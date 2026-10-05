"""历史记录检索工具。

「人会翻聊天记录」——把它做成工具而非塞进提示词，模型需要时才调用，
既省 token 又符合精简提示词原则。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from astrbot.api import FunctionTool, logger
from astrbot.api.event import AstrMessageEvent

if TYPE_CHECKING:  # pragma: no cover
    from ..storage import MemoryDB


@dataclass
class SearchHistoryTool(FunctionTool):
    """让 AI 检索历史聊天记录。"""

    db: "MemoryDB | None" = None

    name: str = "search_chat_history"
    description: str = (
        "检索过去的聊天记录。当你需要回忆此前聊过什么、某人说过什么、"
        "或确认某个细节时使用。可按当前会话或全部会话来查。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要查找的关键词或短语",
                },
                "scope": {
                    "type": "string",
                    "description": "检索范围：current=仅当前会话（默认），all=全部会话",
                    "enum": ["current", "all"],
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回条数，默认 10",
                },
            },
            "required": ["query"],
        }
    )

    async def run(
        self,
        event: AstrMessageEvent,
        query: str,
        scope: str = "current",
        limit: int = 10,
    ) -> str:
        if self.db is None or not self.db.connected:
            return json.dumps({"error": "记忆库未就绪"}, ensure_ascii=False)

        umo = event.unified_msg_origin if scope != "all" else None
        try:
            rows = await self.db.search_messages(
                query, umo=umo, limit=max(1, min(int(limit or 10), 50))
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[ai_companion] 历史检索失败: {e}", exc_info=True)
            return json.dumps({"error": "检索失败"}, ensure_ascii=False)

        if not rows:
            return json.dumps({"found": 0, "results": []}, ensure_ascii=False)

        results = [
            {
                "time": _fmt_time(row["created_at"]),
                "speaker": row["sender_name"] or ("bot" if row["role"] == "assistant" else "?"),
                "text": row["content"],
            }
            for row in rows
        ]
        return json.dumps(
            {"found": len(results), "results": results}, ensure_ascii=False
        )


def _fmt_time(ts: float) -> str:
    import time

    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return ""
