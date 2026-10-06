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

    def __init__(
        self,
        rng: random.Random | None = None,
        *,
        defer_on_fail: bool = False,
    ) -> None:
        """
        Args:
            rng: 随机源（可注入以便测试）。
            defer_on_fail: 为 True 时，**未通过**时不自作主张否决，而是弃权
                （返回 ``None``），把决定权交给策略链后续的读空气决策器。
        """
        self._rng = rng or random.Random()
        self._defer_on_fail = defer_on_fail

    async def decide(self, ctx: TurnContext) -> Decision | None:
        p = ctx.config.reply_probability
        if p >= 1.0:
            return reply("概率设为 1.0（总是回复）", self.name)

        if p <= 0.0:
            if self._defer_on_fail:
                return None
            return skip("概率设为 0.0（从不回复）", self.name)

        roll = self._rng.random()
        if roll < p:
            return reply(f"概率通过（{roll:.3f} < {p:.3f}）", self.name)
        if self._defer_on_fail:
            return None  # 交予读空气决策器
        return skip(f"概率未通过（{roll:.3f} >= {p:.3f}）", self.name)
