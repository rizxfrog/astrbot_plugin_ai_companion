"""每会话运行态注册表。

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
from dataclasses import dataclass, field


@dataclass
class SessionActor:
    """单个会话（窗口）的运行态。"""

    umo: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_message_ts: float = 0.0
    last_reply_ts: float = 0.0
    unanswered_count: int = 0
    cooldown_until: float = 0.0
    # 主动发言调度（P3 使用；P0 仅记录）
    next_proactive_ts: float = 0.0
    # 预留：人格 / 好感度等会话级覆盖
    state: dict = field(default_factory=dict)

    def bump_message(self, when: float | None = None) -> None:
        self.last_message_ts = time.time() if when is None else when

    def bump_reply(self, when: float | None = None) -> None:
        now = time.time() if when is None else when
        self.last_reply_ts = now
        self.unanswered_count = 0

    @property
    def in_cooldown(self) -> bool:
        return time.time() < self.cooldown_until

    def silence_seconds(self, now: float | None = None) -> float:
        if self.last_message_ts <= 0:
            return float("inf")
        return (time.time() if now is None else now) - self.last_message_ts


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

    def silence_candidates(self, threshold_seconds: float, now: float | None = None) -> list[SessionActor]:
        """列出沉默超过阈值的会话，供主动消息调度使用。

        这是「同时关注多个窗口」的服务端实现：一次扫描即可得到所有该开口的窗口，
        不需要为每个窗口维护常驻协程或定时器。
        """
        now = time.time() if now is None else now
        result: list[SessionActor] = []
        for actor in self._actors.values():
            if actor.last_message_ts <= 0:
                continue
            if now - actor.last_message_ts >= threshold_seconds:
                result.append(actor)
        return result

    def __len__(self) -> int:
        return len(self._actors)
