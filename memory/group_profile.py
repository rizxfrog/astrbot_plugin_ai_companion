"""群画像与熟悉度。

像真人一样融入一个群：先观望、逐步形成对「这个群在聊什么、氛围如何、都有谁」
的长期印象，熟悉度随之上升，进而决定「此刻该不该搭话、怎么搭」。

设计：

* **群画像**是一段滚动更新的自然语言摘要，存在 ``group_profiles`` 表里，
  跨重启保留。内容覆盖：群的类型（游戏/动漫/闲聊）、常聊话题、氛围
  （硬核讨论/熟人互怼/礼貌疏离）、活跃成员的大致风格。
* **滚动合并**与短期记忆压缩同思路：新一批消息总结出「增量印象」，再与旧画像
  合并成新画像，避免无限增长也避免丢失早期信息。
* **熟悉度**（0~100）由两个信号驱动：
  - 消息量：看过越多消息越熟；
  - 画像质量：画像更新过几轮、覆盖了多少人。
  映射成三档（观望 / 融入 / 熟悉），供概率层做渐进式接话。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

# 只提取「这个群整体像什么样」，不重复人物/事件抽取的活。
GROUP_PROFILE_SYSTEM_PROMPT = """你是群聊观察者，负责对「这个群整体」形成印象。

只输出 JSON，不要多余文字：
{"profile":"一段话描述这个群：群类型、常聊话题、氛围、活跃成员的说话风格",
 "topics":["话题1","话题2"],
 "vibe":"一句话描述氛围"}

规则：
- profile 控制在 150 字以内，客观描述，不编造。
- topics 是群里反复出现的话题（游戏名/作品名/共同兴趣），最多 5 个。
- vibe 一句话，如「硬核战力讨论，熟人互怼」「礼貌疏离的闲聊群」。
- 信息不足就写得笼统，不要硬凑。"""

# 把「增量印象」与「旧画像」合并成新画像的提示词
GROUP_PROFILE_MERGE_PROMPT = """把对同一个群的旧印象和新观察到的情况，合并成一段最新的群画像。

旧画像：
{old}

新观察：
{new}

只输出合并后的画像正文（一段话，≤200 字），不要 JSON、不要前缀。"""


@dataclass
class Familiarity:
    """一个群的熟悉度快照。"""

    level: int  # 0~100
    stage: str  # 观望 / 融入 / 熟悉
    profile: str
    message_count: int


# 熟悉度分档阈值（可被配置覆盖）
STAGE_THRESHOLDS = {"融入": 50, "熟悉": 200}


def familiarity_stage(familiar: int, *, thresholds: dict[str, int] | None = None) -> str:
    """把 0~100 的熟悉度映射到阶段名。"""
    t = thresholds or STAGE_THRESHOLDS
    if familiar < t["融入"]:
        return "观望"
    if familiar < t["熟悉"]:
        return "融入"
    return "熟悉"


def _is_group_umo(umo: str) -> bool:
    """按 UMO 的消息类型段判断是否群聊。"""
    for segment in (umo or "").split(":")[1:]:
        if segment.lower() in {"groupmessage", "guildmessage", "group", "guild"}:
            return True
    return False


def compute_familiarity(
    *,
    message_count: int,
    profile_rounds: int,
    known_people: int,
) -> int:
    """由信号计算 0~100 的熟悉度。

    三个信号加权：
    - 消息量：0~200 条线性爬到 60 分；
    - 画像轮次：每轮 +10，封顶 25；
    - 认识的人：每 2 人 +1，封顶 15。

    合计封顶 100。
    """
    msg_score = min(60, int(message_count / 200 * 60))
    round_score = min(25, profile_rounds * 10)
    people_score = min(15, known_people // 2)
    return max(0, min(100, msg_score + round_score + people_score))


class GroupProfileManager:
    """群画像与熟悉度的读写与更新。"""

    def __init__(self, *, db: Any, context: Any, config: Any) -> None:
        self.db = db
        self.context = context
        self.config = config
        self._task: Any = None
        self._running = False

    # ------------------------------------------------------------------
    # 后台更新循环
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if not getattr(self.config, "enable_group_profile", True):
            logger.info("[ai_companion] 群画像未启用")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[ai_companion] 群画像后台更新已启动")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        interval = 60
        while self._running:
            try:
                await asyncio.sleep(interval)
                await self.refresh_all()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ai_companion] 群画像循环异常: {e}", exc_info=True)

    async def refresh_all(self) -> int:
        """对所有已知群跑一轮画像更新，返回更新的群数。"""
        try:
            sessions = await self.db.distinct_sessions()
        except Exception:
            return 0
        updated = 0
        for umo in sessions:
            if not _is_group_umo(umo):
                continue
            try:
                if await self.update_if_due(umo):
                    updated += 1
            except Exception as e:
                logger.error(f"[ai_companion] {umo} 群画像更新失败: {e}", exc_info=True)
                continue
        return updated

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    async def get(self, umo: str) -> Familiarity | None:
        row = await self.db.get_group_profile(umo)
        if row is None or not row["profile"]:
            return None
        return Familiarity(
            level=int(row["familiar"] or 0),
            stage=familiarity_stage(int(row["familiar"] or 0)),
            profile=str(row["profile"] or ""),
            message_count=int(row["message_count"] or 0),
        )

    async def profile_text(self, umo: str) -> str:
        fam = await self.get(umo)
        return fam.profile if fam else ""

    # ------------------------------------------------------------------
    # 更新
    # ------------------------------------------------------------------
    async def update_if_due(self, umo: str) -> bool:
        """若该群画像该更新了（消息增量够多），就更新；返回是否更新。"""
        cfg = self.config
        if not getattr(cfg, "enable_group_profile", True):
            return False

        total = await self.db.count_messages_in(umo)
        if total < getattr(cfg, "group_profile_min_messages", 30):
            return False

        current = await self.db.get_group_profile(umo)
        last_count = int(current["message_count"]) if current else 0
        # 新消息不足一档就不更新（节流模型调用）
        step = getattr(cfg, "group_profile_interval", 50)
        if total - last_count < step:
            return False

        # 只取本窗口的消息做「增量印象」
        window = await self._recent_group_text(umo, limit=max(step, 30))
        if not window.strip():
            return False

        new_obs = await self._observe(window)
        if not new_obs:
            return False

        old_profile = str(current["profile"]) if current else ""
        merged = await self._merge(old_profile, new_obs)

        familiar = compute_familiarity(
            message_count=total,
            profile_rounds=1 if not current else self._estimate_rounds(current),
            known_people=await self._known_people(umo),
        )
        await self.db.upsert_group_profile(
            umo=umo,
            profile=merged,
            message_count=total,
            familiar=familiar,
        )
        logger.info(f"[ai_companion] {umo} 群画像已更新（熟悉度 {familiar}）")
        return True

    # ------------------------------------------------------------------
    async def _recent_group_text(self, umo: str, *, limit: int) -> str:
        rows = await self.db.recent_messages(umo, limit=limit)
        lines = []
        for r in rows:
            content = (r["content"] or "").strip()
            if not content:
                continue
            who = "我" if r["role"] == "assistant" else (r["sender_name"] or "某人")
            lines.append(f"{who}: {content}")
        return "\n".join(lines)

    async def _observe(self, window: str) -> str:
        provider = self._resolve_provider()
        if provider is None:
            return ""
        try:
            resp = await provider.text_chat(
                prompt=f"最近聊天记录：\n{window}",
                system_prompt=GROUP_PROFILE_SYSTEM_PROMPT,
                contexts=[],
            )
            data = _parse_json(getattr(resp, "completion_text", "") or "")
            if not data:
                return ""
            profile = str(data.get("profile") or "").strip()
            topics = data.get("topics") or []
            vibe = str(data.get("vibe") or "").strip()
            if not profile and not topics and not vibe:
                return ""
            parts = [profile] if profile else []
            if topics:
                parts.append("常聊话题：" + "、".join(str(t) for t in topics[:5]))
            if vibe:
                parts.append("氛围：" + vibe)
            return "；".join(p for p in parts if p)
        except Exception as e:
            logger.error(f"[ai_companion] 群画像观察失败: {e}", exc_info=True)
            return ""

    async def _merge(self, old_profile: str, new_obs: str) -> str:
        if not old_profile:
            return new_obs
        provider = self._resolve_provider()
        if provider is None:
            return new_obs
        try:
            resp = await provider.text_chat(
                prompt=GROUP_PROFILE_MERGE_PROMPT.format(old=old_profile, new=new_obs),
                system_prompt="你负责维护对群聊的长期画像，输出客观简洁的正文。",
                contexts=[],
            )
            merged = (getattr(resp, "completion_text", "") or "").strip()
            return merged or new_obs
        except Exception as e:
            logger.error(f"[ai_companion] 群画像合并失败: {e}", exc_info=True)
            return new_obs

    # ------------------------------------------------------------------
    def _resolve_provider(self) -> Any:
        try:
            pid = (getattr(self.config, "group_profile_provider_id", "") or "").strip()
            if pid:
                prov = self.context.get_provider_by_id(pid)
                if prov is not None:
                    return prov
            return self.context.get_using_provider()
        except Exception as e:
            logger.error(f"[ai_companion] 解析群画像模型失败: {e}", exc_info=True)
            return None

    @staticmethod
    def _estimate_rounds(row: Any) -> int:
        # 已有画像的话，按「看过多少消息」粗略估算更新过几轮
        try:
            total = int(row["message_count"] or 0)
            return max(1, total // 50)
        except Exception:
            return 1

    async def _known_people(self, umo: str) -> int:
        try:
            rows = await self.db.recent_messages(umo, limit=200)
            return len({str(r["sender_id"]) for r in rows if r["sender_id"]})
        except Exception:
            return 0


def _parse_json(text: str) -> dict | None:
    if not text:
        return None
    import json

    cleaned = text.strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
