"""专门决策模型插槽（预留）。

未来引入 Jev 等专门决策模型时，在此实现 :class:`ReplyDecider` 协议即可，
无需改动编排层与策略链；只需在构建链时把它插入到概率层附近。
"""

from __future__ import annotations

# 示例（未被 P0 引用）：
#
# from ..base import Decision, ReplyDecider, TurnContext
#
# class JevDecider(ReplyDecider):
#     name = "jev"
#
#     def __init__(self, model_client) -> None:
#         self._client = model_client
#
#     async def decide(self, ctx: TurnContext) -> Decision | None:
#         score = await self._client.score(ctx)   # 0..1 回复倾向
#         if score is None:
#             return None                          # 弃权
#         ...
#
# 然后在 DecisionChain 中 insert 到概率层之前即可。
