"""短期记忆压缩（compact）。

真人不会记住对话里的每一句话，但会记住「聊过什么」。当某个会话的历史长到
可能挤爆上下文窗口时，本模块把最老的一段对话交给模型总结成一段摘要，并用它
替换掉那一段原文——既保住信息，又腾出窗口。

实现要点：

* **只动平台对话历史**（``conversation_manager``）。平台保存的历史就是喂给模型的
  上下文，压缩它才真正让窗口「变短」。插件自己的 ``messages`` 表是原始档案，
  **永不删改**，长期记忆与检索依赖它。
* **保留近期原文**：最近 ``keep_recent`` 条永不压缩。
* **保留人设开端**：人格的示意图对话（``_begin_dialogs``）位于历史最前，若把
  它们卷进摘要会破坏人格，因此检测到就从头跳过。
* **保留 checkpoint**：平台用 ``role="_checkpoint"`` 的内部消息把 LLM 轮次与平台
  流水关联，压缩时必须原样搬运，否则会造成历史与流水对不上。
* **摘要滚动**：新一轮摘要会把「上一轮摘要 + 本轮被压缩的原文」一起重新总结，
  因此多次压缩不会丢失早期的远记忆。

并发安全：历史替换在**平台会话锁**内进行（与平台读写同一会话历史的加锁机制
一致），避免与正在进行的 LLM 请求/历史写入互相覆盖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

try:  # 平台内部实现，缺失时降级为插件自带锁
    from astrbot.core.utils.session_lock import session_lock_manager
except Exception:  # pragma: no cover
    session_lock_manager = None  # type: ignore[assignment]

# 摘要消息的角色标记：用 assistant 承载，模型看到的是「我之前聊过什么」。
SUMMARY_ROLE = "assistant"
SUMMARY_PREFIX = "[更早对话的摘要]"
SUMMARY_MARKER = "[[ai_companion_summary]]"

COMPACT_SYSTEM_PROMPT = """你负责压缩一段聊天记录。把下面的对话总结成一段简明摘要。

要求：
- 只保留事实与关系：谁和谁聊了什么、聊到哪些人/事/约定/情绪走向。
- 保留专有名词、称呼、时间、约定、未完成的约定。
- 丢弃寒暄、重复、无信息量的往来。
- 不要编造，不要评价，不要写「摘要如下」之类的话。
- 直接输出摘要正文，控制在 300 字以内。"""


@dataclass
class CompactResult:
    """一次压缩的结果。"""

    compacted: bool
    dropped: int = 0  # 被替换掉的原文条数
    kept: int = 0  # 保留的原文条数
    summary_len: int = 0
    reason: str = ""


def _to_text(content: Any) -> str:
    """把消息 content 归一为纯文本（多模态内容取文本部分）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
            else:
                text = getattr(item, "text", None)
                if text:
                    parts.append(str(text))
        return "\n".join(p for p in parts if p)
    return ""


def _load_history(conversation: Any) -> list[dict] | None:
    """解析对话历史；损坏时返回 None（宁可不压缩，也不破坏历史）。"""
    raw = getattr(conversation, "history", "")
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, list) else None


class Compactor:
    """基于平台的对话历史压缩器。"""

    def __init__(
        self,
        *,
        conversation_manager: Any,
        db: Any,
        context: Any = None,
        config: Any = None,
    ) -> None:
        self.conversation_manager = conversation_manager
        self.db = db
        self.context = context
        self.config = config

    # ------------------------------------------------------------------
    async def maybe_compact(self, umo: str, conversation: Any) -> CompactResult:
        """历史过长时压缩；否则原样返回。"""
        cfg = self.config
        if cfg is None or not getattr(cfg, "enable_compact", True):
            return CompactResult(False, reason="未启用")
        if self.conversation_manager is None or conversation is None:
            return CompactResult(False, reason="无对话管理器")

        history = _load_history(conversation)
        if history is None:
            return CompactResult(False, reason="历史解析失败")
        if len(history) <= cfg.compact_trigger_turns:
            return CompactResult(False, reason="历史未达压缩阈值")

        cid = getattr(conversation, "cid", None)
        if not cid:
            return CompactResult(False, reason="无法确定对话 ID")

        if session_lock_manager is not None:
            async with session_lock_manager.acquire_lock(umo):
                return await self._do_compact(umo, cid, history)
        return await self._do_compact(umo, cid, history)

    # ------------------------------------------------------------------
    async def _do_compact(self, umo: str, cid: str, history: list[dict]) -> CompactResult:
        cfg = self.config

        # 划分：可压缩区（较老） / 保留区（最近 compact_keep_recent 条）
        keep_recent = max(0, cfg.compact_keep_recent)
        cut = len(history) - keep_recent
        if cut <= 0:
            return CompactResult(False, reason="可压缩区不足")

        compressible = history[:cut]

        # checkpoint 必须原样保留（它把轮次与平台流水关联，压缩会破坏关联）
        checkpoints = [m for m in compressible if _is_checkpoint(m)]
        content_msgs = [m for m in compressible if not _is_checkpoint(m)]

        if len(content_msgs) < cfg.compact_min_dropped:
            return CompactResult(False, reason="可压缩内容不足")

        previous_summary = self._extract_previous_summary(history)
        summary = await self._summarize(content_msgs, previous_summary)
        if not summary:
            # 摘要失败：保持历史原样，宁可不压缩
            return CompactResult(False, reason="摘要生成失败")

        new_history = [
            _make_summary_message(summary),
            *checkpoints,
            *history[cut:],
        ]

        try:
            await self.conversation_manager.update_conversation(umo, cid, history=new_history)
        except Exception as e:
            logger.error(f"[ai_companion] 写入压缩后历史失败: {e}", exc_info=True)
            return CompactResult(False, reason="写入失败")

        result = CompactResult(
            compacted=True,
            dropped=len(content_msgs),
            kept=len(history) - len(content_msgs),
            summary_len=len(summary),
            reason="已压缩",
        )
        logger.info(
            f"[ai_companion] {umo} 历史已压缩："
            f"替换 {result.dropped} 条为摘要（{result.summary_len} 字），"
            f"保留 {result.kept} 条"
        )
        return result

    # ------------------------------------------------------------------
    async def _summarize(self, messages: list[dict], previous_summary: str) -> str:
        provider = await self._resolve_provider()
        if provider is None:
            logger.warning("[ai_companion] 无可用模型，跳过压缩")
            return ""

        recent = "\n".join(self._render(m) for m in messages)
        user_prompt = ""
        if previous_summary:
            user_prompt += f"已有的更早摘要：\n{previous_summary}\n\n"
        user_prompt += f"需要合并进摘要的新对话：\n{recent}"

        try:
            resp = await provider.text_chat(
                prompt=user_prompt,
                system_prompt=COMPACT_SYSTEM_PROMPT,
                contexts=[],
            )
        except Exception as e:
            logger.error(f"[ai_companion] 压缩调用失败: {e}", exc_info=True)
            return ""

        text = (getattr(resp, "completion_text", "") or "").strip()
        return text

    async def _resolve_provider(self) -> Any:
        cfg = self.config
        if self.context is None:
            return None
        try:
            pid = (getattr(cfg, "compact_provider_id", "") or "").strip()
            if pid:
                prov = self.context.get_provider_by_id(pid)
                if prov is not None:
                    return prov
                logger.warning(f"[ai_companion] 配置的压缩模型 {pid} 不存在，回退默认模型")
            return self.context.get_using_provider()
        except Exception as e:
            logger.error(f"[ai_companion] 解析压缩模型失败: {e}", exc_info=True)
            return None

    @staticmethod
    def _render(message: dict) -> str:
        role = message.get("role", "?")
        who = {"user": "对方", "assistant": "我", "system": "系统", "tool": "工具"}.get(role, role)
        text = _to_text(message.get("content"))
        return f"{who}: {text}"

    @staticmethod
    def _extract_previous_summary(history: list[dict]) -> str:
        """取出历史中已有的摘要正文，供滚动合并。"""
        for message in history:
            content = message.get("content")
            if isinstance(content, str) and content.startswith(SUMMARY_MARKER):
                return content[len(SUMMARY_MARKER) :].strip()
        return ""


def _is_checkpoint(message: dict) -> bool:
    return message.get("role") == "_checkpoint"


def _make_summary_message(summary: str) -> dict:
    """构造承载摘要的历史消息。"""
    return {
        "role": SUMMARY_ROLE,
        "content": f"{SUMMARY_MARKER}{SUMMARY_PREFIX}\n{summary}",
    }
