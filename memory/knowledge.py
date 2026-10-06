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
import json
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

EXTRACT_SYSTEM_PROMPT = """你负责从聊天记录里提取关于人的信息。

只输出 JSON，不要多余文字：
{"people":[{"name":"小明","traits":["开朗","话痨"],"style":"说话很随意"}],
 "relations":[{"a":"小明","b":"小红","relation":"姐妹","evidence":"小明说小红是我姐"}]}

规则：
- name 用聊天记录里出现的称呼，原样照抄。
- traits 是这个人的性格或特点，最多 3 个，必须有依据；没有就留空数组。
- style 是一句话描述他/她的说话风格，没有就留空。
- relations 只写对话里明确体现的关系（姐妹、恋人、朋友、同事、父子等），没有就留空。
- 不确定的一律不要写。完全没内容就返回 {"people":[],"relations":[]}。"""


@dataclass
class ExtractionResult:
    people: int = 0
    relations: int = 0
    processed: int = 0
    reason: str = ""


class KnowledgeExtractor:
    """从聊天记录中抽取人物画像与关系。"""

    def __init__(
        self, *, db: Any, context: Any, config: Any
    ) -> None:
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
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
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
                logger.error(
                    f"[ai_companion] {umo} 知识抽取失败: {e}", exc_info=True
                )
                continue
            if result.people or result.relations:
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

        people, relations = await self._apply(data, name_map)

        # 推进游标：只消费到本批最后一条
        last_id = max(int(r["id"]) for r in rows)
        await self.db.set_extraction_cursor(umo, last_id)

        logger.info(
            f"[ai_companion] {umo} 知识抽取完成：{people} 人 / {relations} 关系"
            f"（消费 {len(rows)} 条消息）"
        )
        return ExtractionResult(
            people=people, relations=relations, processed=len(rows), reason="已抽取"
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
        self, data: dict, name_map: dict[str, str]
    ) -> tuple[int, int]:
        people = data.get("people") or []
        relations = data.get("relations") or []

        # 先把本批出现的名字解析为实体 ID
        resolved: dict[str, str] = {}
        for person in people + [{"name": r.get("a")} for r in relations] + [
            {"name": r.get("b")} for r in relations
        ]:
            name = str((person or {}).get("name") or "").strip()
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
                subj, predicate, obj,
                evidence=str(rel.get("evidence") or "")[:200],
            )
            saved_relations += 1

        return saved_people, saved_relations

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
        """查一个人：画像 + 关系。供工具与上下文注入使用。"""
        entity_id = name_or_id
        entity = await self.db.get_entity(entity_id)
        if entity is None:
            alias_hit = await self.db.resolve_alias(name_or_id)
            if alias_hit:
                entity_id = alias_hit
                entity = await self.db.get_entity(entity_id)

        profile = await self.db.get_profile(entity_id)
        relations = await self.db.get_relations(entity_id)
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
        }

    async def _name_of(self, entity_id: str) -> str:
        entity = await self.db.get_entity(entity_id)
        if entity and entity.get("last_name"):
            return entity["last_name"]
        if entity_id.startswith("name:"):
            return entity_id[5:]
        return entity_id

    # ------------------------------------------------------------------
    async def note_from_speaker(
        self, entity_id: str, display_name: str
    ) -> None:
        """见到某人时说一声：维护实体与别名（画像内容交给抽取器）。"""
        if not entity_id:
            return
        await self.db.touch_entity(entity_id, display_name)
        if display_name and display_name != entity_id:
            await self.db.bind_alias(display_name, entity_id)


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
