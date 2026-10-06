"""事件回忆工具。

「人有时会想起之前发生过的事」——做成工具，需要时才回忆。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from astrbot.api import FunctionTool, logger
from astrbot.api.event import AstrMessageEvent

if TYPE_CHECKING:  # pragma: no cover
    from ..memory import KnowledgeExtractor


@dataclass
class RecallEventsTool(FunctionTool):
    """回忆之前发生过的事情。"""

    extractor: "KnowledgeExtractor | None" = None

    name: str = "recall_events"
    description: str = (
        "回忆与某人或某事相关的事件。当需要想起此前的约定、计划、"
        "共同经历或重要变化时使用。不给关键词则返回当前会话最近发生的事。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要回忆的关键词（人名或事情）。留空则返回最近的事。",
                },
                "scope": {
                    "type": "string",
                    "description": "范围：current=仅当前会话（默认），all=全部会话",
                    "enum": ["current", "all"],
                },
            },
            "required": [],
        }
    )

    async def run(
        self, event: AstrMessageEvent, query: str = "", scope: str = "current"
    ) -> str:
        if self.extractor is None:
            return json.dumps({"error": "记忆未就绪"}, ensure_ascii=False)
        umo = event.unified_msg_origin if scope != "all" else ""
        try:
            events = await self.extractor.recall_events(query, umo=umo, limit=6)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[ai_companion] 事件回忆失败: {e}", exc_info=True)
            return json.dumps({"error": "回忆失败"}, ensure_ascii=False)
        return json.dumps({"found": len(events), "events": events}, ensure_ascii=False)
