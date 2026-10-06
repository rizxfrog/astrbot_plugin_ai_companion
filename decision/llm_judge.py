"""LLM 读空气决策器。

与其他决策器的关键区别：它需要一次独立的、**不进入平台 Agent 链路**的 LLM 调用，
因此由编排层注入 ``judge`` 回调（``Orchestrator._judge``），避免决策层反向依赖
平台上下文。

它在策略链中的位置：概率层**之后**。当概率层弃权（defer）时，读空气负责最终
拍板；调用失败时同样弃权，由链尾默认保守否决。
"""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable

from astrbot.api import logger

from .base import Decision, ReplyDecider, TurnContext, reply, skip

JudgeFn = Callable[[TurnContext], Awaitable[dict | None]]

# 精简提示词：只描述「像人一样判断」，不堆砌规则，充分发挥模型自身能力。
JUDGE_SYSTEM_PROMPT = """你是群聊中的普通成员，不是助手，也不是客服。
请判断此刻你是否想接这句话。

倾向接话：被点名/被提问/话题你感兴趣/气氛需要人接。
倾向不接：闲聊与你无关/别人已经在回/你插不上话/单纯刷屏。

只输出 JSON，不要多余文字：
{"reply": true 或 false, "reason": "一句话理由", "mood": "此刻心情词"}"""


class LLMJudgeDecider(ReplyDecider):
    """用一次轻量 LLM 调用做「读空气」。"""

    name = "llm_judge"

    def __init__(self, judge: JudgeFn, timeout: float = 15.0) -> None:
        self._judge = judge
        self._timeout = timeout

    async def decide(self, ctx: TurnContext) -> Decision | None:
        try:
            result = await asyncio.wait_for(self._judge(ctx), timeout=self._timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[ai_companion] 读空气超时（>{self._timeout}s），弃权")
            return None
        except Exception as e:
            logger.error(f"[ai_companion] 读空气调用失败: {e}", exc_info=True)
            return None  # 弃权，交由链上后续决策器

        if not isinstance(result, dict) or "reply" not in result:
            return None

        if result.get("reply"):
            reason = str(result.get("reason") or "读空气判定为想接")
            return reply(reason, self.name)
        reason = str(result.get("reason") or "读空气判定为不想接")
        return skip(reason, self.name)


def parse_judge_output(text: str) -> dict | None:
    """从模型输出中稳健地解析 JSON（容忍 ```json 包裹与前后噪声）。"""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
