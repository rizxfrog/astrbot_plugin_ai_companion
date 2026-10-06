"""会话运行态注册表。

这是「同时关注 N 个窗口」的核心：常驻内存的只有每会话一小段状态（几 KB），
LLM 调用与 Agent 都是按需创建、用完即弃，绝不 per-window 常驻。

并发模型：

* **同一会话串行**：每个 Actor 持有一把 ``asyncio.Lock``。
* **跨会话并行**：不同 umo 的 Actor 互不阻塞，天然并发。
* **主动任务**：由全局调度循环扫描候选窗口后，为命中的窗口创建临时任务，
  启动前抢锁并复查状态，避免与用户消息互相打断。
"""

from __future__ import annotations

import asyncio
import time
import zlib
from dataclasses import dataclass, field


@dataclass
class SessionActor:
    """单个会话（窗口）的运行态。"""

    umo: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_message_ts: float = 0.0
    """最近一次「用户侧活动」时间（含 AI 回复）。"""
    last_reply_ts: float = 0.0
    last_proactive_ts: float = 0.0
    """最近一次主动发言时间。"""
    unanswered_count: int = 0
    """连续主动发言而未被回应的次数。"""
    cooldown_until: float = 0.0
    state: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    def bump_message(self, when: float | None = None) -> None:
        """用户侧出现活动：刷新沉默计时，清零未回复计数。"""
        self.last_message_ts = time.time() if when is None else when
        self.unanswered_count = 0

    def bump_reply(self, when: float | None = None) -> None:
        """AI 完成一次回复：视为对话活跃。"""
        self.last_reply_ts = time.time() if when is None else when
        self.unanswered_count = 0

    def mark_proactive(self, when: float | None = None) -> None:
        """记录一次主动发言。"""
        self.last_proactive_ts = time.time() if when is None else when
        self.unanswered_count += 1

    # ------------------------------------------------------------------
    @property
    def last_activity_ts(self) -> float:
        """会话内最近一次「有动静」的时间（用户消息 / AI 回复 / 主动发言）。"""
        return max(self.last_message_ts, self.last_reply_ts, self.last_proactive_ts)

    @property
    def in_cooldown(self) -> bool:
        return time.time() < self.cooldown_until

    def silence_seconds(self, now: float | None = None) -> float:
        last = self.last_activity_ts
        if last <= 0:
            return float("inf")
        return (time.time() if now is None else now) - last


class SessionRegistry:
    """umo -> SessionActor 的轻量注册表。"""

    def __init__(self) -> None:
        self._actors: dict[str, SessionActor] = {}

    def get(self, umo: str) -> SessionActor:
        actor = self._actors.get(umo)
        if actor is None:
            actor = SessionActor(umo=umo)
            self._actors[umo] = actor
        return actor

    def peek(self, umo: str) -> SessionActor | None:
        return self._actors.get(umo)

    def all_actors(self) -> list[SessionActor]:
        return list(self._actors.values())

    def proactive_candidates(
        self,
        *,
        threshold_seconds: float,
        max_unanswered: int,
        jitter_seconds: float = 0.0,
        now: float | None = None,
    ) -> list[SessionActor]:
        """列出可以主动发言的会话。

        这是「同时关注多个窗口」的服务端实现：一次扫描即可得到所有该开口的窗口，
        不需要为每个窗口维护常驻协程或定时器。

        每个会话带一个**稳定的**抖动偏移（由 UMO 派生），使各会话不在同一时刻
        齐刷刷开口，更像真人各自的节奏；同一会话每次判定结果一致，不会来回抖动。
        """
        now = time.time() if now is None else now
        result: list[SessionActor] = []
        for actor in self._actors.values():
            if actor.last_activity_ts <= 0:
                continue
            effective = threshold_seconds + self._jitter(actor.umo, jitter_seconds)
            if now - actor.last_activity_ts < effective:
                continue
            if actor.in_cooldown:
                continue
            if max_unanswered > 0 and actor.unanswered_count >= max_unanswered:
                continue
            result.append(actor)
        return result

    @staticmethod
    def _jitter(umo: str, jitter_seconds: float) -> float:
        if jitter_seconds <= 0:
            return 0.0
        crc = zlib.crc32(umo.encode("utf-8"))
        return (crc % 1000) / 1000.0 * jitter_seconds

    def __len__(self) -> int:
        return len(self._actors)
