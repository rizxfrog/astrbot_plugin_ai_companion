"""上下文装配：构造每轮请求的「动态环境信息」。

短期对话历史由平台的 conversation 机制负责（本插件通过
``event.request_llm(conversation=...)`` 复用平台链路）。本模块只负责那些
「不进历史、只对当前轮生效」的内容，并统一标记为临时内容块，避免破坏
服务端提示词缓存。

后续版本将在此接入长期记忆检索结果的注入。
"""

from __future__ import annotations

import time
from typing import Any

try:  # 不同版本位置略有差异
    from astrbot.core.agent.message import TextPart
except ImportError:  # pragma: no cover
    TextPart = None  # type: ignore[assignment]


class ContextAssembler:
    """构造动态上下文块。"""

    def __init__(self, db: Any = None) -> None:
        self._db = db

    def build_extra_parts(
        self, cfg: Any, people_hint: str = "", events_hint: str = ""
    ) -> list[Any]:
        """返回需要追加到本轮请求的临时内容块。"""
        parts: list[Any] = []
        if cfg.inject_time and TextPart is not None:
            now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            part = TextPart(text=f"<当前时间>{now}</当前时间>").mark_as_temp()
            parts.append(part)
        if people_hint and TextPart is not None:
            part = TextPart(
                text=f"<你对这个人的了解>{people_hint}</你对这个人的了解>"
            ).mark_as_temp()
            parts.append(part)
        if events_hint and TextPart is not None:
            part = TextPart(
                text=f"<最近发生的事>{events_hint}</最近发生的事>"
            ).mark_as_temp()
            parts.append(part)
        return parts
