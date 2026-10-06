"""策略链：按成本从低到高依次执行，短路即停。

设计意图：低成本的硬过滤/规则/限流先跑，只有全部弃权时才进入有成本的
概率层与（未来的）LLM 读空气层。这样绝大多数消息在微秒级就被处理完，
不会为「明显不该回」的消息浪费一次模型调用。
"""

from __future__ import annotations

from collections.abc import Sequence

from astrbot.api import logger

from .base import Decision, ReplyDecider, TurnContext, skip


class DecisionChain:
    """回复决策策略链。"""

    def __init__(self, deciders: Sequence[ReplyDecider]) -> None:
        if not deciders:
            raise ValueError("决策链至少需要一个决策器")
        self.deciders: list[ReplyDecider] = list(deciders)

    def add(self, decider: ReplyDecider) -> None:
        self.deciders.append(decider)

    def insert(self, index: int, decider: ReplyDecider) -> None:
        """供未来插入专门决策器（如 Jev）使用。"""
        self.deciders.insert(index, decider)

    async def decide(self, ctx: TurnContext) -> Decision:
        """运行策略链，返回最终决策。"""
        for decider in self.deciders:
            try:
                decision = await decider.decide(ctx)
            except Exception as e:  # 单个决策器异常不应中断整条链
                logger.error(
                    f"[ai_companion] 决策器 {getattr(decider, 'name', decider)} 异常: {e}",
                    exc_info=True,
                )
                continue

            if decision is None:
                continue

            ctx.log(f"{decision.decider} -> {decision.should_reply} ({decision.reason})")
            return decision

        # 所有决策器都弃权：默认不回复（保守，避免意外刷屏）
        return skip("所有决策器均弃权", "chain_default")
