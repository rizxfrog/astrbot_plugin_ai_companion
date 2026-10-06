"""硬过滤层：零成本、必然否决的判定。"""

from __future__ import annotations

from .base import Decision, ReplyDecider, TurnContext, skip

# 平台可能下发的空事件类型（与 GCP 的经验一致）
_EMPTY_COMPONENT_TYPES = {"unknown"}


class HardFilterDecider(ReplyDecider):
    """在进入任何有成本的判断前，先剔除不可能回复的情况。"""

    name = "hard_filter"

    async def decide(self, ctx: TurnContext) -> Decision | None:
        if not ctx.config.enabled_for(is_private=ctx.is_private):
            return skip("插件在该会话类型下未启用", self.name)

        if ctx.config.ignore_command_messages and ctx.is_command:
            return skip("指令消息交给平台指令链路处理", self.name)

        if not (ctx.message_text or "").strip() and not self._has_rich_content(ctx):
            return skip("空消息", self.name)

        # Bot 自己发的消息不回复
        self_id = ""
        try:
            self_id = str(ctx.event.get_self_id() or "")
        except Exception:
            self_id = ""
        if self_id and ctx.sender_id and str(ctx.sender_id) == self_id:
            return skip("Bot 自身消息", self.name)

        return None

    @staticmethod
    def _has_rich_content(ctx: TurnContext) -> bool:
        from ..context.renderer import render_chain

        try:
            components = ctx.event.get_messages()
        except Exception:
            return False
        # 任何能渲染出内容的组件都算有效内容（图片/语音等会渲染为占位符）
        return bool(render_chain(components, self_id=""))
