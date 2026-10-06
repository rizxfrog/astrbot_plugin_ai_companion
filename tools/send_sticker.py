"""表情包工具。

模型**不能**凭空构造 ``{"type": "sticker"}`` 这种消息段——平台不认识，会直接报
``unsupported message type 'sticker'``。因此把发表情做成一个真正的工具：
模型调用它，我们直接发送一张真实的图片，平台无需理解任何自定义类型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from astrbot.api import FunctionTool, logger
from astrbot.api.event import AstrMessageEvent

if TYPE_CHECKING:  # pragma: no cover
    from ..humanize import StickerLibrary


@dataclass
class SendStickerTool(FunctionTool):
    """给当前会话发送一张表情包。"""

    stickers: StickerLibrary | None = None
    send_fn: Any = None
    """由插件注入的实际发送函数：async (umo, Image) -> bool。"""

    name: str = "send_sticker"
    description: str = (
        "发送一张表情包。当你想用表情回应、而不是用文字时使用。"
        "分类可留空，会自动挑一张合适的。不要用文字描述表情内容。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "表情分类，例如 开心 / 无语 / 生气；留空则自动挑一张",
                },
            },
            "required": [],
        }
    )

    async def run(self, event: AstrMessageEvent, category: str = "") -> str:
        if self.stickers is None or self.stickers.empty:
            return json.dumps({"ok": False, "error": "没有可用的表情包"}, ensure_ascii=False)

        path = self.stickers.pick(category)
        if path is None:
            return json.dumps({"ok": False, "error": "没有可用的表情包"}, ensure_ascii=False)

        # 闸门：与 [sticker:x] 标记、自动补图共用同一个限流器
        limiter = getattr(self.stickers, "limiter", None)
        if limiter is not None:
            ok, reason = limiter.allow(str(event.unified_msg_origin))
            if not ok:
                logger.debug(f"[ai_companion] 工具表情被限流：{reason}")
                # 告诉模型「这次没发」而不是报错，避免它重试或向用户解释
                return json.dumps(
                    {"ok": True, "sent": False, "note": "这次先不发图，用文字回"},
                    ensure_ascii=False,
                )

        try:
            from astrbot.core.message.components import Image

            image = Image.fromFileSystem(path)
        except Exception as e:
            logger.error(f"[ai_companion] 构造表情组件失败: {e}", exc_info=True)
            return json.dumps({"ok": False, "error": "表情构造失败"}, ensure_ascii=False)

        if self.send_fn is None:
            return json.dumps({"ok": False, "error": "发送通道未就绪"}, ensure_ascii=False)

        try:
            ok = await self.send_fn(event.unified_msg_origin, image)
        except Exception as e:
            logger.error(f"[ai_companion] 发表情失败: {e}", exc_info=True)
            return json.dumps({"ok": False, "error": "发送失败"}, ensure_ascii=False)

        if not ok:
            return json.dumps({"ok": False, "error": "发送失败"}, ensure_ascii=False)
        return json.dumps(
            {"ok": True, "sent": self.stickers.resolve_category(category) or "表情"},
            ensure_ascii=False,
        )
