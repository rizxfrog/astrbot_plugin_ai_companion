"""人物查询工具。

「知道身边是什么人」——做成工具而非塞进提示词，模型需要时才查。
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
class LookupPersonTool(FunctionTool):
    """查询某个人的印象与关系。"""

    extractor: KnowledgeExtractor | None = None

    name: str = "lookup_person"
    description: str = (
        "查询你对某个人已知的印象与关系，例如「小明是个什么样的人」"
        "「新新和谁是什么关系」。当话题涉及具体的人、需要回忆对这个人的了解时使用。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "要查询的人的称呼（昵称或群名片）",
                },
            },
            "required": ["name"],
        }
    )

    async def run(self, event: AstrMessageEvent, name: str) -> str:
        if self.extractor is None:
            return json.dumps({"error": "记忆未就绪"}, ensure_ascii=False)
        name = (name or "").strip()
        if not name:
            return json.dumps({"error": "缺少 name 参数"}, ensure_ascii=False)
        try:
            info = await self.extractor.describe_person(name)
        except Exception as e:
            logger.error(f"[ai_companion] 人物查询失败: {e}", exc_info=True)
            return json.dumps({"error": "查询失败"}, ensure_ascii=False)
        return json.dumps(info, ensure_ascii=False)
