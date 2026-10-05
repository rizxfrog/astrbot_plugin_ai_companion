"""决策层基础类型。

「要不要回这条消息」被抽象为可插拔的 :class:`ReplyDecider`，编排层只负责按序执行
策略链。未来引入专门决策模型（如 Jev）时，只需实现同一协议并插入链中，
编排层零改动。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class TurnContext:
    """一次「消息 -> 决策」轮次所需的全部信息。"""

    event: Any
    umo: str
    actor: Any  # SessionActor（避免循环导入，运行期鸭子类型）
    config: Any  # CompanionConfig
    is_private: bool
    is_wake: bool
    is_command: bool
    message_text: str
    sender_id: str
    sender_name: str
    now: float = field(default_factory=time.time)

    def log(self, message: str) -> None:
        if getattr(self.config, "debug_mode", False):
            from astrbot.api import logger

            logger.info(f"[ai_companion][决策] {message}")


@dataclass
class Decision:
    """决策结果。"""

    should_reply: bool
    reason: str
    decider: str

    def __bool__(self) -> bool:  # 方便 if decision
        return self.should_reply


@runtime_checkable
class ReplyDecider(Protocol):
    """回复决策器协议。

    返回 ``None`` 表示「弃权」，交由策略链中的下一个决策器判断；
    返回具体的 :class:`Decision` 则立即终止策略链。
    """

    name: str

    async def decide(self, ctx: TurnContext) -> Decision | None: ...


def reply(reason: str, decider: str) -> Decision:
    return Decision(should_reply=True, reason=reason, decider=decider)


def skip(reason: str, decider: str) -> Decision:
    return Decision(should_reply=False, reason=reason, decider=decider)
