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

        ctx = TurnContext(
            event=event,
            umo=umo,
            actor=actor,
            config=cfg,
            is_private=is_private,
            is_mention=self._is_mention(event, is_private),
            is_command=self._is_command(event),
            message_text=text,
            sender_id=sender_id,
            sender_name=sender_name,
            self_id=self_id,
            recent_lines=await self._recent_lines(umo),
        )
        decision = await self.chain.decide(ctx)

        if not decision.should_reply:
            self._block_default_llm(event)
            if cfg.debug_mode:
                logger.info(f"[ai_companion] {umo} 不回复（{decision.reason}）")
            return TurnResult(handled=True, should_reply=False, reason=decision.reason)

        if cfg.debug_mode:
            logger.info(f"[ai_companion] {umo} 回复（{decision.reason}）")

        event.set_extra(MANAGED_KEY, True)
        conversation = await self._ensure_conversation(umo)
        conversation = await self._maybe_compact(umo, conversation)
        prompt = format_for_model(
            sender_name=sender_name, sender_id=sender_id, content=text,
        ) or (text or "[空消息]")

        return TurnResult(
            handled=True,
            should_reply=True,
            reason=decision.reason,
            prompt=prompt,
            conversation=conversation,
            extra_parts=self.assembler.build_extra_parts(cfg),
            people_hint=(
                await self._people_hint(sender_id)
                if cfg.inject_people_context
                else ""
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
