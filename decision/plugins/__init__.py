"""专门决策模型插槽。

:class:`ProbabilityDecider` 目前扮演「成本闸门」：它决定**是否值得调用模型**。
当引入专门决策模型（如 Jev）后，这个插槽负责**最终判断要不要回复**。

两者的关系：

* **现在**：闸门（概率）-> 通用 LLM 读空气 -> 决定。
* **接入 Jev 后**：闸门保留 —— 它依然是省钱的关键（不必对每条消息都跑模型）；
  把 ``LLMJudgeDecider`` 换成 ``JevDecider`` 即可，策略链与编排层**零改动**。

之所以把闸门和决策器分开：

* 闸门是**近乎免费**的，且与「用哪个模型判断」无关，任何决策模型都需要它；
* 决策器是**有成本**的，未来可能换成更小/更快/更专业的模型。

因此本文件给出一个可直接使用的 Jev 适配器骨架，以及如何接入的说明。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from astrbot.api import logger

from ..base import Decision, ReplyDecider, TurnContext, reply, skip

# 接入 Jev 时替换成真实的打分函数：返回 0~1 的「回复倾向」
ScoreFn = Callable[[TurnContext], Awaitable[float | None]]


class JevDecider(ReplyDecider):
    """专门决策模型适配器（骨架）。

    实现步骤：

    1. 把 ``score`` 换成真实模型调用，返回 0~1 的回复倾向；返回 ``None`` 表示弃权。
    2. 在 ``main.initialize`` 里用它替换 ``LLMJudgeDecider``：
       ``deciders.append(JevDecider(score=..., threshold=0.5))``
    3. 概率闸门保持原样 —— 它继续承担「值不值得调模型」的职责。
    """

    name = "jev"

    def __init__(self, score: ScoreFn, *, threshold: float = 0.5) -> None:
        self._score = score
        self._threshold = threshold

    async def decide(self, ctx: TurnContext) -> Decision | None:
        try:
            value = await self._score(ctx)
        except Exception as e:
            logger.error(f"[ai_companion] Jev 打分失败: {e}", exc_info=True)
            return None

        if value is None:
            return None  # 弃权

        value = max(0.0, min(1.0, float(value)))
        if value >= self._threshold:
            return reply(f"决策模型倾向回复（{value:.2f}）", self.name)
        return skip(f"决策模型倾向不回复（{value:.2f}）", self.name)


# 保留旧名，避免既有引用失效
__all__ = ["JevDecider", "ScoreFn"]
