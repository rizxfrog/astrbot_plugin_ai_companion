"""主动消息调度。

真人不只是被动应答，也会在冷场时主动开口。本模块用一个**全局调度循环**
扫描所有关注中的会话，对沉默够久的窗口发起一次主动消息。

为什么不是「每窗口一个 Agent / 定时器」：

* 常驻的只有每会话一小段状态（见 :mod:`core.registry`）；
* 循环每 tick 扫一遍候选，命中的窗口才创建临时任务，发完即销毁；
* 因此关注 1000 个窗口也只跑一个循环，内存与协程数量都不随窗口数膨胀。

生成方式：直接调用 Provider 并**显式传入人格**，上下文由本插件的记忆库提供。
相比把伪消息塞回平台管线，这条路径不依赖平台内部实现，也不会往对话历史里写入
一条并不存在的「用户消息」。

「未回复即闭嘴」是真人本能：连续主动几次没人接，就进入冷却，安静下来。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain

DEFAULT_PROACTIVE_PROMPT = (
    "已经安静了 {{silence_minutes}} 分钟，没有人说话。"
    "请你主动开口说点什么，自然地活跃一下气氛——可以延续刚才的话题，"
    "也可以开启一个轻松的新话题。请完全按照你的人设说话，"
    "不要提及你收到了任何提示、提醒或指令。"
)


def is_group_umo(umo: str) -> bool:
    """按 UMO 的消息类型段判断是否群聊（避免平台名含 group 造成误判）。"""
    for segment in (umo or "").split(":")[1:]:
        low = segment.lower()
        if low in {"groupmessage", "guildmessage", "group", "guild"}:
            return True
        if low in {"friendmessage", "privatemessage", "friend", "private"}:
            return False
    low = (umo or "").lower()
    return "group" in low or "guild" in low


class ProactiveScheduler:
    """全局主动消息调度器。"""

    def __init__(
        self,
        *,
        config: Any,
        registry: Any,
        db: Any,
        context: Any,
        conversation_manager: Any,
        humanizer: Any = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.db = db
        self.context = context
        self.conversation_manager = conversation_manager
        self.humanizer = humanizer

        self._task: asyncio.Task | None = None
        self._running = False
        self._inflight: set[str] = set()
        self._ticks_since_persist = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if not self.config.enable_proactive:
            logger.info("[ai_companion] 主动消息未启用")
            return
        await self._restore_state()
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"[ai_companion] 主动消息已启动：沉默阈值 "
            f"{self.config.proactive_threshold_minutes} 分钟，"
            f"扫描间隔 {self.config.proactive_check_interval_seconds} 秒"
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._persist_all()

    # ------------------------------------------------------------------
    async def _loop(self) -> None:
        interval = max(5, self.config.proactive_check_interval_seconds)
        while self._running:
            try:
                await asyncio.sleep(interval)
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                # 单次扫描失败不应终止整个调度循环
                logger.error(f"[ai_companion] 主动消息扫描异常: {e}", exc_info=True)

    async def _tick(self) -> None:
        cfg = self.config
        candidates = self.registry.proactive_candidates(
            threshold_seconds=cfg.proactive_threshold_minutes * 60,
            max_unanswered=cfg.proactive_max_unanswered,
            jitter_seconds=cfg.proactive_threshold_minutes * 60 * 0.35,
        )
        for actor in candidates:
            if actor.umo in self._inflight:
                continue
            if not self._is_allowed(actor.umo, cfg):
                continue
            self._inflight.add(actor.umo)
            asyncio.create_task(self._guarded_run(actor))

        # 全量落盘有成本，按扫描次数节流；关键时刻（发言后）另有即时落盘。
        self._ticks_since_persist += 1
        if self._ticks_since_persist >= 10:
            self._ticks_since_persist = 0
            await self._persist_all()

    async def _guarded_run(self, actor: Any) -> None:
        try:
            async with actor.lock:
                await self._run_for(actor)
        except Exception as e:
            logger.error(
                f"[ai_companion] {actor.umo} 主动消息失败: {e}", exc_info=True
            )
        finally:
            self._inflight.discard(actor.umo)

    # ------------------------------------------------------------------
    def _is_allowed(self, umo: str, cfg: Any) -> bool:
        """会话类型与白名单过滤。

        白名单为空表示「不主动任何会话」——这是刻意的安全默认，避免一旦打开总开关
        就在所有群/私聊里开口。需要主动全部会话时，显式填写 ``*``。
        """
        is_group = is_group_umo(umo)
        if is_group and not cfg.proactive_group:
            return False
        if not is_group and not cfg.proactive_private:
            return False

        allowed = [s.strip() for s in (cfg.proactive_sessions or []) if s.strip()]
        if not allowed:
            return False
        if "*" in allowed:
            return True
        return umo in allowed

    # ------------------------------------------------------------------
    async def _run_for(self, actor: Any) -> None:
        cfg = self.config
        umo = actor.umo

        # 抢到锁后再复查一次：等待期间用户可能已经说话
        if actor.silence_seconds() < cfg.proactive_threshold_minutes * 60:
            return

        provider = self._resolve_provider(umo)
        if provider is None:
            logger.warning(f"[ai_companion] {umo} 无可用模型，跳过主动消息")
            return

        system_prompt = await self._persona_prompt(umo)
        contexts = await self._history_context(umo, cfg)
        prompt = self._render_prompt(actor, cfg)

        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt=system_prompt,
                contexts=contexts,
            )
        except Exception as e:
            logger.error(f"[ai_companion] {umo} 主动消息生成失败: {e}", exc_info=True)
            return

        text = (getattr(resp, "completion_text", "") or "").strip()
        if not text:
            logger.info(f"[ai_companion] {umo} 主动消息内容为空，放弃")
            return

        sent = await self._send(umo, text)
        if not sent:
            logger.warning(f"[ai_companion] {umo} 主动消息发送失败")
            return

        actor.mark_proactive()
        if cfg.proactive_max_unanswered > 0 and (
            actor.unanswered_count >= cfg.proactive_max_unanswered
        ):
            actor.cooldown_until = time.time() + cfg.proactive_cooldown_minutes * 60
            logger.info(
                f"[ai_companion] {umo} 连续 {actor.unanswered_count} 次主动无人回应，"
                f"进入 {cfg.proactive_cooldown_minutes} 分钟冷却"
            )

        if cfg.record_all_messages:
            try:
                await self.db.insert_message(
                    umo=umo, role="assistant", content=text,
                    sender_id=str(self._self_id()), sender_name="bot",
                    is_proactive=True,
                )
            except Exception as e:
                logger.error(f"[ai_companion] 记录主动消息失败: {e}", exc_info=True)

        await self._persist(actor)
        logger.info(f"[ai_companion] {umo} 已主动发言：{text[:60]}")

    # ------------------------------------------------------------------
    async def _persona_prompt(self, umo: str) -> str:
        mgr = getattr(self.context, "persona_manager", None)
        if mgr is None:
            return ""
        try:
            persona = await mgr.get_default_persona_v3(umo)
        except Exception:
            return ""
        if isinstance(persona, dict):
            return str(persona.get("prompt") or "")
        return ""

    async def _history_context(self, umo: str, cfg: Any) -> list[dict]:
        if cfg.proactive_history_turns <= 0:
            return []
        try:
            rows = await self.db.recent_messages(
                umo, limit=cfg.proactive_history_turns
            )
        except Exception:
            return []
        return [
            {"role": r["role"], "content": r["content"]}
            for r in rows
            if r["role"] in ("user", "assistant") and (r["content"] or "").strip()
        ]

    def _render_prompt(self, actor: Any, cfg: Any) -> str:
        template = (cfg.proactive_prompt or "").strip() or DEFAULT_PROACTIVE_PROMPT
        silence_minutes = int(actor.silence_seconds() / 60)
        return (
            template.replace("{{silence_minutes}}", str(silence_minutes))
            .replace("{{unanswered_count}}", str(actor.unanswered_count))
            .replace("{{current_time}}", time.strftime("%Y-%m-%d %H:%M"))
        )

    async def _send(self, umo: str, text: str) -> bool:
        """发送主动消息。

        主动消息不经过平台的 ``on_decorating_result``，所以表情标记与悬空占位符
        必须在这里处理，否则会字面发给用户。
        """
        chain = MessageChain()
        try:
            if self.humanizer is not None:
                cleaned, images = self.humanizer.apply_to_text(text)
                if cleaned:
                    chain.message(cleaned)
                for image in images:
                    chain.chain.append(image)
            else:
                chain.message(text)
        except Exception as e:
            logger.error(f"[ai_companion] 主动消息拟人化失败: {e}", exc_info=True)
            chain = MessageChain().message(text)

        if not chain.chain:
            # 文本被清理后为空、又没有图片：没有可发的内容
            return False

        try:
            ok = await self.context.send_message(umo, chain)
            return bool(ok)
        except Exception as e:
            logger.error(f"[ai_companion] 主动消息发送异常: {e}", exc_info=True)
            return False

    def _resolve_provider(self, umo: str) -> Any:
        try:
            pid = (getattr(self.config, "proactive_provider_id", "") or "").strip()
            if pid:
                prov = self.context.get_provider_by_id(pid)
                if prov is not None:
                    return prov
            return self.context.get_using_provider(umo)
        except Exception as e:
            logger.error(f"[ai_companion] 解析主动消息模型失败: {e}", exc_info=True)
            return None

    def _self_id(self) -> str:
        return "bot"

    # ------------------------------------------------------------------
    # 持久化：跨重启恢复
    # ------------------------------------------------------------------
    async def _persist(self, actor: Any) -> None:
        try:
            await self.db.upsert_session_state(
                actor.umo,
                last_message_ts=actor.last_message_ts,
                unanswered_count=actor.unanswered_count,
                last_proactive_ts=actor.last_proactive_ts,
            )
        except Exception as e:
            logger.error(f"[ai_companion] 持久化会话状态失败: {e}", exc_info=True)

    async def _persist_all(self) -> None:
        for actor in self.registry.all_actors():
            await self._persist(actor)

    async def _restore_state(self) -> None:
        try:
            rows = await self.db.load_session_states()
        except Exception as e:
            logger.error(f"[ai_companion] 恢复会话状态失败: {e}", exc_info=True)
            return
        restored = 0
        for row in rows:
            actor = self.registry.get(row["umo"])
            actor.last_message_ts = float(row["last_message_ts"] or 0)
            actor.unanswered_count = int(row["unanswered_count"] or 0)
            actor.last_proactive_ts = float(row["last_proactive_ts"] or 0)
            restored += 1
        if restored:
            logger.info(f"[ai_companion] 已恢复 {restored} 个会话的运行态")
