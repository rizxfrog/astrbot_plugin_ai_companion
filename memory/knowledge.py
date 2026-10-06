"""人物与关系的抽取与查询。

把「谁是什么样的人、谁和谁是什么关系」从聊天记录里沉淀成长期记忆。

设计要点：

* **全局实体**：``entity_id`` 用平台用户 ID（稳定且跨会话），因此同一个人在不同
  群里仍是同一个人——这正是「对人的记忆」该有的样子。
* **名字归一**：抽取结果只给得出「称呼」，需要把称呼映射回实体。映射优先用
  本批消息的「发送者名 -> 发送者 ID」，其次查历史别名；都查不到时建立一个
  临时实体（``name:<称呼>``），保证「被提及但没说过话的人」也能进入关系图。
* **增量抽取**：按会话维护消息主键游标，只总结新消息，不重复烧 token。
* **失败无害**：解析失败或模型没给出内容时，不写库、不推进游标，下次重试。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

EXTRACT_SYSTEM_PROMPT = """你负责从聊天记录里提取关于人和事的长期记忆。

只输出 JSON，不要多余文字：
{"people":[{"name":"小明","traits":["开朗","话痨"],"style":"说话很随意"}],
 "relations":[{"a":"小明","b":"小红","relation":"姐妹","evidence":"小明说小红是我姐"}],
 "events":[{"title":"小明和小红约好周末去爬山","summary":"两人约定周六一早出发，小红担心体力","type":"约定","importance":0.7,"participants":["小明","小红"]}]}

规则：
- people：name 原样照抄聊天里出现的称呼；traits 最多 3 个且必须有依据；style 一句话。
- relations：只写明确体现的关系（姐妹、恋人、朋友、同事、父子等），没有就留空。
- events：只记**值得记住的事**——约定、计划、重要变化、共同经历、情绪转折、冲突。
  不要记寒暄和日常闲聊。title 一句话说清发生了什么；type 用「约定/计划/经历/变化/冲突/其他」；
  importance 0~1（越重要越高）；participants 写涉及的称呼。
- 不确定的一律不要写。完全没内容就返回 {"people":[],"relations":[],"events":[]}。"""


@dataclass
class ExtractionResult:
    people: int = 0
    relations: int = 0
    events: int = 0
    processed: int = 0
    reason: str = ""


class KnowledgeExtractor:
    """从聊天记录中抽取人物画像与关系。"""

    def __init__(self, *, db: Any, context: Any, config: Any) -> None:
        self.db = db
        self.context = context
        self.config = config
        self._task: Any = None
        self._running = False

    # ------------------------------------------------------------------
    # 后台增量抽取
    # ------------------------------------------------------------------
    async def start(self) -> None:
        cfg = self.config
        if not getattr(cfg, "enable_knowledge_extraction", True):
            logger.info("[ai_companion] 人物与关系抽取未启用")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"[ai_companion] 人物与关系抽取已启动：每 "
            f"{getattr(cfg, 'extraction_interval_minutes', 30)} 分钟增量总结一次"
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        interval = max(60, int(getattr(self.config, "extraction_interval_minutes", 30)) * 60)
        while self._running:
            try:
                await asyncio.sleep(interval)
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ai_companion] 知识抽取循环异常: {e}", exc_info=True)

    async def run_once(self) -> int:
        """对所有已知会话跑一轮增量抽取，返回成功抽取的会话数。"""
        try:
            sessions = await self.db.distinct_sessions()
        except Exception:
            return 0
        done = 0
        for umo in sessions:
            try:
                result = await self.maybe_extract(umo)
            except Exception as e:
                logger.error(f"[ai_companion] {umo} 知识抽取失败: {e}", exc_info=True)
                continue
            if result.people or result.relations or result.events:
                done += 1
        return done

    # ------------------------------------------------------------------
    async def maybe_extract(self, umo: str) -> ExtractionResult:
        cfg = self.config
        if not getattr(cfg, "enable_knowledge_extraction", True):
            return ExtractionResult(reason="未启用")
        if self.context is None:
            return ExtractionResult(reason="无上下文")

        cursor = await self.db.get_extraction_cursor(umo)
        rows = await self.db.messages_since(umo, cursor, limit=cfg.extraction_batch_size)
        if len(rows) < cfg.extraction_min_messages:
            return ExtractionResult(reason="新消息不足")

        transcript, name_map = self._build_transcript(rows)
        data = await self._call_model(transcript)
        if data is None:
            return ExtractionResult(reason="抽取失败（不推进游标）")

        latest_ts = max(float(r["created_at"]) for r in rows)
        people, relations, events = await self._apply(umo, data, name_map, occurred_at=latest_ts)

        # 推进游标：只消费到本批最后一条
        last_id = max(int(r["id"]) for r in rows)
        await self.db.set_extraction_cursor(umo, last_id)

        logger.info(
            f"[ai_companion] {umo} 知识抽取完成：{people} 人 / {relations} 关系 / "
            f"{events} 事件（消费 {len(rows)} 条消息）"
        )
        return ExtractionResult(
            people=people,
            relations=relations,
            events=events,
            processed=len(rows),
            reason="已抽取",
        )

    # ------------------------------------------------------------------
    def _build_transcript(self, rows: list[Any]) -> tuple[str, dict[str, str]]:
        """拼出聊天记录，并返回「称呼 -> 实体 ID」映射。"""
        name_map: dict[str, str] = {}
        lines = []
        for row in rows:
            content = (row["content"] or "").strip()
            if not content:
                continue
            sender_id = str(row["sender_id"] or "")
            name = str(row["sender_name"] or "")
            if sender_id and name:
                name_map.setdefault(name, sender_id)
            who = "我" if row["role"] == "assistant" else (name or "某人")
            lines.append(f"{who}: {content}")
        return "\n".join(lines), name_map

    async def _call_model(self, transcript: str) -> dict | None:
        provider = self._resolve_provider()
        if provider is None:
            return None
        try:
            resp = await provider.text_chat(
                prompt=f"聊天记录：\n{transcript}",
                system_prompt=EXTRACT_SYSTEM_PROMPT,
                contexts=[],
            )
        except Exception as e:
            logger.error(f"[ai_companion] 知识抽取调用失败: {e}", exc_info=True)
            return None
        return _parse_json(getattr(resp, "completion_text", "") or "")

    def _resolve_provider(self) -> Any:
        try:
            pid = (getattr(self.config, "extraction_provider_id", "") or "").strip()
            if pid:
                prov = self.context.get_provider_by_id(pid)
                if prov is not None:
                    return prov
            return self.context.get_using_provider()
        except Exception as e:
            logger.error(f"[ai_companion] 解析抽取模型失败: {e}", exc_info=True)
            return None

    # ------------------------------------------------------------------
    async def _apply(
        self, umo: str, data: dict, name_map: dict[str, str], *, occurred_at: float
    ) -> tuple[int, int, int]:
        people = data.get("people") or []
        relations = data.get("relations") or []
        events = data.get("events") or []

        # 先把本批出现的名字解析为实体 ID
        names: list[str] = []
        for person in people:
            names.append(str(person.get("name") or ""))
        for rel in relations:
            names.extend([str(rel.get("a") or ""), str(rel.get("b") or "")])
        for event in events:
            for p in event.get("participants") or []:
                names.append(str(p or ""))

        resolved: dict[str, str] = {}
        for raw_name in names:
            name = raw_name.strip()
            if not name or name in resolved:
                continue
            resolved[name] = await self._resolve_name(name, name_map)

        saved_people = 0
        for person in people:
            name = str(person.get("name") or "").strip()
            if not name:
                continue
            entity_id = resolved.get(name) or await self._resolve_name(name, name_map)
            await self.db.touch_entity(entity_id, name)
            await self.db.bind_alias(name, entity_id)
            await self.db.upsert_profile(
                entity_id,
                traits=[str(t) for t in (person.get("traits") or [])][:5] or None,
                style=str(person.get("style") or "") or None,
            )
            saved_people += 1

        saved_relations = 0
        for rel in relations:
            a = str(rel.get("a") or "").strip()
            b = str(rel.get("b") or "").strip()
            predicate = str(rel.get("relation") or "").strip()
            if not (a and b and predicate):
                continue
            subj = resolved.get(a) or await self._resolve_name(a, name_map)
            obj = resolved.get(b) or await self._resolve_name(b, name_map)
            await self.db.touch_entity(subj, a)
            await self.db.touch_entity(obj, b)
            await self.db.upsert_relation(
                subj,
                predicate,
                obj,
                evidence=str(rel.get("evidence") or "")[:200],
            )
            saved_relations += 1

        saved_events = 0
        for event in events:
            title = str(event.get("title") or "").strip()
            if not title:
                continue
            participants: list[tuple[str, str]] = []
            for raw in event.get("participants") or []:
                pname = str(raw or "").strip()
                if not pname:
                    continue
                eid = resolved.get(pname) or await self._resolve_name(pname, name_map)
                await self.db.touch_entity(eid, pname)
                await self.db.bind_alias(pname, eid)
                participants.append((eid, ""))

            # 去重：同会话已有高度相似的标题则跳过，避免同一件事被反复记
            if await self._is_duplicate_event(umo, title):
                continue

            try:
                importance = float(event.get("importance", 0.5))
            except (TypeError, ValueError):
                importance = 0.5

            await self.db.insert_event(
                umo=umo,
                title=title,
                summary=str(event.get("summary") or ""),
                event_type=str(event.get("type") or ""),
                importance=importance,
                occurred_at=occurred_at,
                participants=participants,
            )
            saved_events += 1

        return saved_people, saved_relations, saved_events

    async def _is_duplicate_event(self, umo: str, title: str) -> bool:
        """粗略去重：同会话近期是否已有相似标题的事件。"""
        try:
            recent = await self.db.get_recent_events(umo, limit=20)
        except Exception:
            return False
        normalized = _normalize_title(title)
        for event in recent:
            existing = _normalize_title(str(event.get("title") or ""))
            if not existing:
                continue
            if normalized == existing:
                return True
            # 字符级相似度：中文事件标题短，用集合重叠比即可
            overlap = len(set(normalized) & set(existing)) / max(
                len(set(normalized) | set(existing)), 1
            )
            if overlap >= 0.85:
                return True
        return False

    async def _resolve_name(self, name: str, name_map: dict[str, str]) -> str:
        """称呼 -> 实体 ID：本批发送者优先，其次历史别名，最后建临时实体。"""
        if name in name_map:
            return name_map[name]
        existing = await self.db.resolve_alias(name)
        if existing:
            return existing
        return f"name:{name}"

    # ------------------------------------------------------------------
    async def describe_person(self, name_or_id: str) -> dict:
        """查一个人：画像 + 关系 + 经历过的事。供工具与上下文注入使用。"""
        entity_id = name_or_id
        entity = await self.db.get_entity(entity_id)
        if entity is None:
            alias_hit = await self.db.resolve_alias(name_or_id)
            if alias_hit:
                entity_id = alias_hit
                entity = await self.db.get_entity(entity_id)

        profile = await self.db.get_profile(entity_id)
        relations = await self.db.get_relations(entity_id)
        events = await self.db.get_events_for_entity(entity_id, limit=5)
        return {
            "entity_id": entity_id,
            "name": (entity or {}).get("last_name") or name_or_id,
            "known": entity is not None,
            "profile": profile,
            "relations": [
                {
                    "with": await self._name_of(
                        r["object_id"] if r["subject_id"] == entity_id else r["subject_id"]
                    ),
                    "relation": r["predicate"],
                    "strength": r["strength"],
                }
                for r in relations
            ],
            "events": [
                {
                    "title": e.get("title"),
                    "summary": e.get("summary"),
                    "type": e.get("event_type"),
                }
                for e in events
            ],
        }

    async def recall_events(self, query: str = "", *, umo: str = "", limit: int = 5) -> list[dict]:
        """回忆事件：有关键词则检索，否则取最近的事件线。"""
        scope = umo or None
        if query.strip():
            events = await self.db.search_events(query, umo=scope, limit=limit)
        else:
            events = await self.db.get_recent_events(scope, limit=limit)
        return [
            {
                "title": e.get("title"),
                "summary": e.get("summary"),
                "type": e.get("event_type"),
                "when": _fmt_ts(e.get("occurred_at")),
                "participants": [
                    await self._name_of(p["entity_id"]) for p in e.get("participants", [])
                ],
            }
            for e in events
        ]

    async def recent_events_hint(self, umo: str, limit: int = 3) -> str:
        """把最近的关键事件摘成一行，用于本轮上下文。"""
        try:
            events = await self.db.get_recent_events(umo, limit=limit)
        except Exception:
            return ""
        titles = [str(e.get("title") or "").strip() for e in events]
        titles = [t for t in titles if t]
        if not titles:
            return ""
        return "；".join(titles)

    async def _name_of(self, entity_id: str) -> str:
        entity = await self.db.get_entity(entity_id)
        if entity and entity.get("last_name"):
            return entity["last_name"]
        if entity_id.startswith("name:"):
            return entity_id[5:]
        return entity_id

    # ------------------------------------------------------------------
    async def note_from_speaker(self, entity_id: str, display_name: str) -> None:
        """见到某人时说一声：维护实体与别名（画像内容交给抽取器）。"""
        if not entity_id:
            return
        await self.db.touch_entity(entity_id, display_name)
        if display_name and display_name != entity_id:
            await self.db.bind_alias(display_name, entity_id)


def _fmt_ts(ts: Any) -> str:
    try:
        import time

        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return ""


def _normalize_title(text: str) -> str:
    """归一事件标题用于去重：去空白与标点，统一大小写。"""
    stripped = "".join(ch for ch in text if not ch.isspace())
    return stripped.strip("，。！？、,.!?;；:：").lower()


def _parse_json(text: str) -> dict | None:
    """稳健解析模型输出（容忍 ```json 包裹与前后噪声）。"""
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
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
