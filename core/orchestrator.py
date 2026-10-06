"""编排层：把「一条消息」串成 记录 → 决策 → 生成 的完整流程。

与平台链路的衔接方式（本设计的核心，也是与自建上下文方案最大的区别）：

* 注册一个消息 handler，既让本插件能拿到群聊中的**非唤醒消息**用于记录，
  又能在决策为「回复」时通过 ``yield event.request_llm(...)`` 复用平台整条 Agent 链路
  ——人格、Skills、工具、其他插件的 ``on_llm_request`` 注入全部免费获得。
* 决策为「不回复」时，通过 ``event.should_call_llm(True)`` 阻止平台默认 LLM。
  该标记**不会**打断其他插件的 handler（区别于 ``stop_event``）。
* 群聊中未被 @ 的消息，平台默认链路本来就不会调用 LLM
  （``is_at_or_wake_command`` 为假），因此「决定回复」时才需要本插件主动发起请求。

短期对话历史由平台 conversation 提供，长期记忆由本插件的数据库提供。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from ..context import format_for_model, render_chain
from ..decision import DecisionChain, TurnContext
from ..decision.llm_judge import JUDGE_SYSTEM_PROMPT, parse_judge_output

MANAGED_KEY = "_ai_companion_managed"
RECORDED_KEY = "_ai_companion_recorded"


@dataclass
class TurnResult:
    """一次消息处理的结论，供 main.py 决定如何生成回复。"""

    handled: bool = False
    should_reply: bool = False
    reason: str = ""
    prompt: str = ""
    conversation: Any = None
    extra_parts: list = field(default_factory=list)
    people_hint: str = ""
    events_hint: str = ""
    image_urls: list[str] = field(default_factory=list)


@dataclass
class _Burst:
    """一次「连发」的累积状态。"""

    token: object          # 最新一条消息的身份标记
    lines: list[tuple[str, str, str]] = field(default_factory=list)


class Orchestrator:
    """聊天主流程编排。"""

    def __init__(
        self,
        *,
        config: Any,
        registry: Any,
        chain: DecisionChain,
        db: Any,
        assembler: Any,
        conversation_manager: Any,
        context: Any = None,
        compactor: Any = None,
        extractor: Any = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.chain = chain
        self.db = db
        self.assembler = assembler
        self.conversation_manager = conversation_manager
        self.context = context
        self.compactor = compactor
        self.extractor = extractor
        self._bg_tasks: set[Any] = set()
        self._seen: dict[str, float] = {}
        # 「连发合并」状态：umo -> 该会话正在累积的消息串
        self._bursts: dict[str, _Burst] = {}

    # ------------------------------------------------------------------
    def _debounce_seconds(self, *, is_private: bool) -> float:
        """按会话类型取等待时长。0 表示关闭合并（收到即决策）。"""
        cfg = self.config
        if not getattr(cfg, "enable_debounce", True):
            return 0.0
        raw = (
            cfg.debounce_private_seconds
            if is_private
            else cfg.debounce_group_seconds
        )
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 0.0

    async def _wait_quiet(
        self,
        umo: str,
        line: "tuple[str, str, str]",
        wait_seconds: float,
    ) -> "tuple[list[tuple[str, str, str]], bool]":
        """等这个会话安静下来，再返回累积的消息串。

        语义是「用户停手若干秒后才决策」：收到消息起等 ``wait_seconds``，期间
        又来一条就重新计时（滚动窗口），直到窗口内没有新消息为止。

        滚动窗口不需要在这里手动延长：每来一条新消息都会重新进入本方法并从自己
        的到达时刻起算，因此「最新那条消息等满窗口」自然等价于「用户停手了」。
        谁是最新那条，由 ``token`` 判定 —— 被更新的消息取代时，旧的这一轮直接
        放弃，把内容交给最新的那一轮，从而只回答一次。

        Returns:
            ``(本轮该处理的消息串, 是否继续处理)``。

            消息串可能包含**当前这条之外**的更早消息 —— 它们原本会被平台作为
            「运行中 Agent 的补充消息」注入，诱发重复回答；合并成一次请求即可根治。

            第二项为 False 表示本轮已被后续消息合并，调用方应立即返回（并且
            必须照常阻止平台默认链路，避免漏出未经决策的回复）。
        """
        cfg = self.config
        token = object()
        # 继承尚未结算的上一串：先到的那几轮会让出，内容由最新这轮统一处理
        existing = self._bursts.get(umo)
        lines: list[tuple[str, str, str]] = (
            list(existing.lines) if existing is not None else []
        )
        lines.append(line)
        self._bursts[umo] = _Burst(token=token, lines=lines)

        deadline = time.monotonic() + wait_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # 阻塞等待；等待期间新消息可并发进入（平台按会话并发派发事件）
            await asyncio.sleep(min(remaining, 0.25))
            burst = self._bursts.get(umo)
            if burst is None or burst.token is not token:
                if cfg.debug_mode:
                    logger.info(f"[ai_companion] {umo} 本轮消息已被后续消息合并")
                return [], False

        # 结算前再确认一次归属：窗口到期与新消息到达可能恰好同时发生，
        # 若此时已被顶替却仍返回消息串，就会和新的那一轮各自结算 -> 重复回答。
        burst = self._bursts.get(umo)
        if burst is None or burst.token is not token:
            if cfg.debug_mode:
                logger.info(f"[ai_companion] {umo} 本轮消息已被后续消息合并")
            return [], False

        self._bursts.pop(umo, None)
        return lines, True

    async def _collect_image_paths(self, event: Any) -> list[str]:
        """把当前消息里的图片落成可传给模型的路径。

        为什么需要这一步：本插件通过 ``yield event.request_llm(...)`` 自己包办
        LLM 请求，而平台只在**它自己创建请求**时才扫描消息里的图片组件
        （``_build_main_agent`` 的 else 分支）。走「已有 provider_request」分支时
        只沿用 ``req.image_urls``，不会去读 ``event.message_obj.message``。
        结果是用户发的图片**根本进不了模型**，模型只能看到渲染后的占位文本
        （如 ``[图片]``），于是回「我这边全是空白」。

        这里补上同样的收集动作，保持与平台行为一致。
        """
        out: list[str] = []
        try:
            from astrbot.core.message.components import Image
        except Exception:  # pragma: no cover - 平台导入失败时静默跳过
            return out
        try:
            components = list(event.message_obj.message)
        except Exception:
            return out
        for comp in components:
            if not isinstance(comp, Image):
                continue
            try:
                path = await comp.convert_to_file_path()
            except Exception as e:
                if getattr(self.config, "debug_mode", False):
                    logger.info(f"[ai_companion] 图片转存失败，跳过: {e}")
                continue
            if path:
                out.append(path)
        if out and getattr(self.config, "debug_mode", False):
            logger.info(f"[ai_companion] 本轮附带 {len(out)} 张图片给模型")
        return out

    @staticmethod
    def _merge_prompt(lines: list[tuple[str, str, str]]) -> str:
        """把一组消息渲染成给模型的一条提示词。"""
        if not lines:
            return "[空消息]"
        if len(lines) == 1:
            name, sid, content = lines[0]
            return format_for_model(
                sender_name=name, sender_id=sid, content=content
            ) or "[空消息]"
        # 多条：逐行渲染，让模型明确「这是同一个人连发的几条」
        rendered = []
        for name, sid, content in lines:
            rendered.append(
                format_for_model(sender_name=name, sender_id=sid, content=content)
            )
        return (
            "(对方刚才连着发了几条消息，请合并理解为一次发言、只回应一次)\n"
            + "\n".join(rendered)
        )

    # ------------------------------------------------------------------
    async def handle_message(self, event: Any) -> TurnResult:
        """处理一条入站消息。"""
        cfg = self.config
        is_private = bool(event.is_private_chat())
        if not cfg.enabled_for(is_private=is_private):
            return TurnResult()

        if self._is_duplicate(event):
            return TurnResult(handled=True, should_reply=False, reason="重复消息")

        umo = event.unified_msg_origin
        actor = self.registry.get(umo)
        actor.bump_message()

        sender_id = str(event.get_sender_id() or "")
        sender_name = str(event.get_sender_name() or "")
        self_id = str(event.get_self_id() or "")
        text = render_chain(event.get_messages(), self_id=self_id)

        if cfg.record_all_messages:
            await self._record_user(
                umo=umo, event=event, sender_id=sender_id,
                sender_name=sender_name, text=text,
            )

        # 见到这个人就登记一下（画像内容由抽取器定期总结）
        if sender_id:
            self._spawn(self._remember_person(sender_id, sender_name))

        # 其他插件已经回复过：不再插手，也不拦截
        if getattr(event, "_has_send_oper", False):
            return TurnResult(handled=True, should_reply=False, reason="已有其他发送行为")

        # ---- 连发合并：等用户停手再决策 ----
        # 用户常把一句话拆成几条发。每条都独立决策会导致模型一口气把几条都答了
        # （表现为「同样的话回答两遍」）。这里等窗口内没有新消息后再统一决策。
        # 指令消息不等待：用户敲 /help 就期望立刻有反应。
        owner_event = event
        is_command = self._is_command(event)
        wait_seconds = (
            0.0 if is_command else self._debounce_seconds(is_private=is_private)
        )
        if wait_seconds > 0:
            lines, is_settler = await self._wait_quiet(
                umo, (sender_name, sender_id, text), wait_seconds
            )
            if not is_settler:
                # 已被后续消息合并：本轮不作决策，但**必须**阻止平台默认链路，
                # 否则未被决策的消息会由平台自己的 Agent 直接回掉。
                self._block_default_llm(event)
                return TurnResult(
                    handled=True, should_reply=False, reason="已合并到后续消息"
                )
            # 用整串消息作为本轮输入
            text = self._merge_prompt(lines)

        ctx = TurnContext(
            event=owner_event,
            umo=umo,
            actor=actor,
            config=cfg,
            is_private=is_private,
            is_mention=self._is_mention(owner_event, is_private),
            is_command=is_command,
            message_text=text,
            sender_id=sender_id,
            sender_name=sender_name,
            self_id=self_id,
            recent_lines=await self._recent_lines(umo),
        )
        decision = await self.chain.decide(ctx)

        if not decision.should_reply:
            self._block_default_llm(owner_event)
            if cfg.debug_mode:
                logger.info(f"[ai_companion] {umo} 不回复（{decision.reason}）")
            return TurnResult(handled=True, should_reply=False, reason=decision.reason)

        if cfg.debug_mode:
            logger.info(f"[ai_companion] {umo} 回复（{decision.reason}）")

        owner_event.set_extra(MANAGED_KEY, True)
        conversation = await self._ensure_conversation(umo)
        conversation = await self._maybe_compact(umo, conversation)
        image_urls = await self._collect_image_paths(owner_event)

        return TurnResult(
            handled=True,
            should_reply=True,
            reason=decision.reason,
            prompt=text,
            conversation=conversation,
            image_urls=image_urls,
            extra_parts=self.assembler.build_extra_parts(cfg),
            people_hint=(
                await self._people_hint(sender_id)
                if cfg.inject_people_context
                else ""
            ),
            events_hint=(
                await self._events_hint(umo) if cfg.inject_events_context else ""
            ),
        )

    async def _maybe_compact(self, umo: str, conversation: Any) -> Any:
        """历史过长时压缩，并返回压缩后的对话对象。"""
        if self.compactor is None or conversation is None:
            return conversation
        try:
            result = await self.compactor.maybe_compact(umo, conversation)
        except Exception as e:
            # 压缩失败绝不能影响正常回复
            logger.error(f"[ai_companion] 记忆压缩异常: {e}", exc_info=True)
            return conversation

        if not result.compacted:
            return conversation

        # 重新读取，确保后续请求使用的是压缩后的历史
        try:
            cid = getattr(conversation, "cid", None)
            refreshed = await self.conversation_manager.get_conversation(umo, cid)
            return refreshed or conversation
        except Exception:
            return conversation

    def _spawn(self, coro: Any) -> None:
        """派发一个后台协程，不阻塞消息处理。"""
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:
            return
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _remember_person(self, entity_id: str, display_name: str) -> None:
        if self.extractor is None:
            return
        try:
            await self.extractor.note_from_speaker(entity_id, display_name)
        except Exception as e:
            logger.debug(f"[ai_companion] 登记人物失败: {e}")

    async def _people_hint(self, sender_id: str) -> str:
        """摘一句「对当前发言者的了解」，用于本轮动态上下文。"""
        if self.extractor is None or not sender_id:
            return ""
        try:
            info = await self.extractor.describe_person(sender_id)
        except Exception:
            return ""
        if not info.get("known"):
            return ""

        parts = []
        profile = info.get("profile") or {}
        traits = profile.get("traits") or []
        if traits:
            parts.append("、".join(str(t) for t in traits[:5]))
        if profile.get("style"):
            parts.append(str(profile["style"]))
        rels = [
            f"{r['relation']}{r['with']}" for r in (info.get("relations") or [])[:5]
        ]
        line = f"{info.get('name') or '此人'}"
        if parts:
            line += "：" + "；".join(parts)
        if rels:
            line += "（关系：" + "，".join(rels) + "）"
        return line if (parts or rels) else ""

    async def _events_hint(self, umo: str) -> str:
        """摘一句「最近发生的事」，用于本轮动态上下文。"""
        if self.extractor is None:
            return ""
        try:
            return await self.extractor.recent_events_hint(umo, limit=3)
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # 读空气实现
    # ------------------------------------------------------------------
    async def _judge(self, ctx: TurnContext) -> dict | None:
        """一次轻量 LLM 调用，判断「此刻想不想接这句话」。

        刻意不走平台 Agent 链路：没有工具、没有人格、上下文极小，
        因此又快又便宜，也不会污染主对话历史。
        """
        provider = await self._resolve_judge_provider(ctx.umo)
        if provider is None:
            if ctx.config.debug_mode:
                logger.info("[ai_companion] 未找到可用模型，读空气弃权")
            return None

        recent = "\n".join(ctx.recent_lines) if ctx.recent_lines else "（暂无）"
        user_prompt = (
            f"最近的消息：\n{recent}\n\n"
            f"即将判断的这条：{ctx.sender_name or '某人'}: {ctx.message_text}"
        )

        if ctx.config.debug_mode:
            logger.info(f"[ai_companion] 读空气请求:\n{user_prompt[:400]}")

        resp = await provider.text_chat(
            prompt=user_prompt,
            system_prompt=JUDGE_SYSTEM_PROMPT,
            contexts=[],
        )
        raw = getattr(resp, "completion_text", "") or ""
        parsed = parse_judge_output(raw)

        if ctx.config.debug_mode:
            logger.info(f"[ai_companion] 读空气原始输出: {raw[:200]!r}")
            logger.info(f"[ai_companion] 读空气解析结果: {parsed}")
        return parsed

    async def _resolve_judge_provider(self, umo: str) -> Any:
        """解析读空气使用的 Provider：优先配置指定，否则用会话默认。"""
        context = self.context
        if context is None:
            return None
        try:
            pid = (self.config.judge_provider_id or "").strip()
            if pid:
                prov = context.get_provider_by_id(pid)
                if prov is not None:
                    return prov
                logger.warning(
                    f"[ai_companion] 配置的读空气模型 {pid} 不存在，回退默认模型"
                )
            return await context.get_using_provider_async(umo)
        except Exception as e:
            logger.error(f"[ai_companion] 解析读空气模型失败: {e}", exc_info=True)
            return None

    async def _recent_lines(self, umo: str, limit: int = 10) -> list[str]:
        """取本会话最近的可读消息，供读空气判断氛围。"""
        try:
            rows = await self.db.recent_messages(umo, limit=limit)
        except Exception:
            return []
        lines: list[str] = []
        for row in rows:
            content = (row["content"] or "").strip()
            if not content:
                continue
            if row["role"] == "assistant":
                lines.append(f"我: {content}")
            else:
                name = row["sender_name"] or "某人"
                lines.append(f"{name}: {content}")
        return lines

    # ------------------------------------------------------------------
    async def handle_after_sent(self, event: Any) -> None:
        """消息发送后归档 AI 回复并刷新运行态。"""
        if not self.config.enabled_for(is_private=bool(event.is_private_chat())):
            return
        if event.get_extra(RECORDED_KEY):
            return
        text = self._extract_result_text(event)
        if not text:
            return
        event.set_extra(RECORDED_KEY, True)

        umo = event.unified_msg_origin
        self.registry.get(umo).bump_reply()
        if not self.config.record_all_messages:
            return
        try:
            await self.db.insert_message(
                umo=umo,
                role="assistant",
                content=text,
                sender_id=str(event.get_self_id() or ""),
                sender_name="bot",
            )
        except Exception as e:
            logger.error(f"[ai_companion] 归档 AI 回复失败: {e}", exc_info=True)

    # ------------------------------------------------------------------
    async def inject(self, event: Any, req: Any) -> None:
        """在 LLM 请求上追加系统提示词补充（当前时间等动态块由 main.py 注入）。"""
        if not event.get_extra(MANAGED_KEY):
            return
        extra = (self.config.system_prompt_extra or "").strip()
        if extra and extra not in (req.system_prompt or ""):
            req.system_prompt = (req.system_prompt or "") + "\n" + extra

    # ------------------------------------------------------------------
    async def _ensure_conversation(self, umo: str) -> Any:
        """取得（必要时创建）该会话当前对话，以便复用平台人格与历史。"""
        cm = self.conversation_manager
        if cm is None:
            return None
        try:
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                cid = await cm.new_conversation(umo)
            return await cm.get_conversation(umo, cid)
        except Exception as e:
            logger.warning(f"[ai_companion] 获取对话失败，退化为无对话请求: {e}")
            return None

    async def _record_user(self, *, umo: str, event: Any, sender_id: str,
                           sender_name: str, text: str) -> None:
        try:
            await self.db.insert_message(
                umo=umo, role="user", content=text,
                sender_id=sender_id, sender_name=sender_name, raw=_safe_raw(event),
            )
        except Exception as e:
            logger.error(f"[ai_companion] 记录用户消息失败: {e}", exc_info=True)

    def _is_duplicate(self, event: Any) -> bool:
        mid = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        if not mid:
            return False
        now = time.time()
        if len(self._seen) > 512:
            self._seen = {k: v for k, v in self._seen.items() if now - v < 60}
        if mid in self._seen:
            return True
        self._seen[mid] = now
        return False

    @staticmethod
    def _is_mention(event: Any, is_private: bool) -> bool:
        """是否真的被叫到（被 @ / 被引用 / 唤醒前缀 / 私聊）。

        只能信任 ``is_at_or_wake_command``：它在唤醒阶段仅对「被 @ / 被引用 /
        唤醒前缀」置位；而 ``is_wake`` 会被本插件自身 handler 的 filter 通过置位，
        对本插件恒为真，不能作为判断依据。
        """
        if is_private:
            return True
        return bool(getattr(event, "is_at_or_wake_command", False))

    @staticmethod
    def _is_command(event: Any) -> bool:
        try:
            from astrbot.core.star.filter.command import CommandFilter

            for handler in event.get_extra("activated_handlers") or []:
                for flt in getattr(handler, "event_filters", []) or []:
                    if isinstance(flt, CommandFilter):
                        return True
        except Exception:
            pass
        return False

    @staticmethod
    def _block_default_llm(event: Any) -> None:
        try:
            event.should_call_llm(True)
        except Exception:
            event.call_llm = True

    @staticmethod
    def _extract_result_text(event: Any) -> str:
        try:
            result = event.get_result()
        except Exception:
            return ""
        chain = getattr(result, "chain", None) if result is not None else None
        if not chain:
            return ""
        return render_chain(chain, self_id=str(event.get_self_id() or ""))


def _safe_raw(event: Any) -> Any:
    try:
        chain = event.get_messages() or []
        return [
            c.toDict() if hasattr(c, "toDict") else str(getattr(c, "type", ""))
            for c in chain
        ]
    except Exception:
        return None
