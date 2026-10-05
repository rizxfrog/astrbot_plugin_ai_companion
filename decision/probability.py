"""概率层：模拟「有时候想接、有时候懒得接」。

这一层是未来接入专门决策模型（如 Jev）最自然的挂载点：把 ``random.random()``
替换为模型打分，即可让「回复倾向」由模型而非固定概率决定。
"""

from __future__ import annotations

import random

from .base import Decision, TurnContext, ReplyDecider, reply, skip


class ProbabilityDecider(ReplyDecider):
    """基础概率筛选。"""

    name = "probability"

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()

    async def decide(self, ctx: TurnContext) -> Decision | None:
        p = ctx.config.reply_probability
        if p >= 1.0:
            return reply("概率设为 1.0（总是回复）", self.name)
        if p <= 0.0:
            return skip("概率设为 0.0（从不回复）", self.name)

        roll = self._rng.random()
        if roll < p:
            return reply(f"概率通过（{roll:.3f} < {p:.3f}）", self.name)
        return skip(f"概率未通过（{roll:.3f} >= {p:.3f}）", self.name)
