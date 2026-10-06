"""限流层：真人不会秒回，也不会刷屏。"""

from __future__ import annotations

from .base import Decision, ReplyDecider, TurnContext, skip


class RateLimitDecider(ReplyDecider):
    """最小回复间隔与冷却。"""

    name = "rate_limit"

    async def decide(self, ctx: TurnContext) -> Decision | None:
        actor = ctx.actor

        if getattr(actor, "in_cooldown", False):
            remain = max(0.0, actor.cooldown_until - ctx.now)
            return skip(f"处于冷却中（剩余 {remain:.0f}s）", self.name)

        interval = ctx.config.min_reply_interval_seconds
        if interval > 0 and actor.last_reply_ts > 0:
            elapsed = ctx.now - actor.last_reply_ts
            if elapsed < interval:
                return skip(f"距上次回复仅 {elapsed:.1f}s（最小间隔 {interval}s）", self.name)

        return None
