"""概率层：模拟「有时候想接、有时候懒得接」。

这一层同时承担两个角色，取决于链上后面有没有「需要模型才能判断」的决策器：

* **后面没有 AI 决策器** —— 它就是最终回复概率。
* **后面有 AI 决策器**（读空气 / 未来的 Jev）—— 它是**进入 AI 决策的概率**：
  未命中就直接由代码判定「不回复」，命中才花钱调用模型。

第二种用法是成本闸门：群聊里绝大多数消息本该石沉大海，让它们先过一道
几乎免费的概率筛，只有少数被抽中才需要模型动脑。

进入 AI 决策的概率可以是固定的，也可以由外部按会话动态提供
（例如群聊与私聊取不同值），因此 :class:`ProbabilityDecider` 接受一个
``rate`` 回调而不是一个死值。
"""

from __future__ import annotations

import inspect
import random
from collections.abc import Awaitable, Callable

from .base import Decision, ReplyDecider, TurnContext, reply, skip

# 由编排层注入：根据本轮上下文决定「进入 AI 决策的概率」。
# 支持同步与异步两种回调：需要查库（如群熟悉度）时用异步。
RateProvider = Callable[[TurnContext], float | Awaitable[float]]


class ProbabilityDecider(ReplyDecider):
    """概率闸门。"""

    name = "probability"

    def __init__(
        self,
        rng: random.Random | None = None,
        *,
        defer_on_fail: bool = False,
        rate: RateProvider | None = None,
    ) -> None:
        """
        Args:
            rng: 随机源（可注入以便测试）。
            defer_on_fail: 为 True 时，**未通过**时不自作主张否决，而是弃权
                （返回 ``None``），把决定权交给策略链后续的 AI 决策器。
            rate: 动态概率来源；为空则用 ``config.reply_probability``。
        """
        self._rng = rng or random.Random()
        self._defer_on_fail = defer_on_fail
        self._rate = rate

    # ------------------------------------------------------------------
    async def _resolve_rate(self, ctx: TurnContext) -> float:
        if self._rate is not None:
            try:
                result = self._rate(ctx)
                if inspect.isawaitable(result):
                    result = await result
                return max(0.0, min(1.0, float(result)))
            except Exception:
                pass
        return max(0.0, min(1.0, float(ctx.config.reply_probability)))

    def pass_label(self) -> str:
        """「通过」在日志里的含义，取决于后面有没有 AI 决策器。"""
        return "进入 AI 决策" if self._defer_on_fail else "回复"

    # ------------------------------------------------------------------
    async def decide(self, ctx: TurnContext) -> Decision | None:
        p = await self._resolve_rate(ctx)
        label = self.pass_label()

        if p >= 1.0:
            if self._defer_on_fail:
                return None  # 全部交给 AI 决策器，本层不表态
            return reply("概率设为 1.0（总是回复）", self.name)

        if p <= 0.0:
            if self._defer_on_fail:
                # 概率为 0：代码直接判不回，连 AI 都不问
                return skip("未命中门槛（概率 0）", self.name)
            return skip("概率设为 0.0（从不回复）", self.name)

        roll = self._rng.random()
        if roll < p:
            if self._defer_on_fail:
                return None  # 命中 -> 交给 AI 决策器
            return reply(f"概率通过（{roll:.3f} < {p:.3f}）", self.name)

        if self._defer_on_fail:
            return skip(f"未命中{label}门槛（{roll:.3f} >= {p:.3f}）", self.name)
        return skip(f"概率未通过（{roll:.3f} >= {p:.3f}）", self.name)
