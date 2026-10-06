"""规则层：明确应当回复的场景（真人一定会接话）。"""

from __future__ import annotations

from .base import Decision, TurnContext, ReplyDecider, reply


class RuleDecider(ReplyDecider):
    """被 @ / 被引用 / 私聊 / 唤醒前缀 —— 这些是「明确叫到了我」。

    注意：群聊中未被唤醒的消息根本不会进入决策链（唤醒阶段已 ``stop_event``），
    因此本层的群聊分支主要覆盖「被唤醒但需要区分是否只是点名」的场景。
    """

    name = "rule"

    async def decide(self, ctx: TurnContext) -> Decision | None:
        if ctx.is_private:
            # 私聊等价于「一直和你说话」
            return reply("私聊消息", self.name)

        if ctx.is_mention:
            return reply("被 @ / 被引用 / 唤醒前缀", self.name)

        return None
