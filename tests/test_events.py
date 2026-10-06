"""事件线（P5）测试。

覆盖真正会出错的行为：

1. 事件与其参与者正确落库、按会话隔离。
2. 关键词检索与「某人参与过的事」。
3. 相似事件去重（同一件事不被反复记）。
4. 抽取失败不推进游标、事件不落库。
5. 上下文提示（最近事件摘要）。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))
import os

os.environ.setdefault("ASTRBOT_ROOT", tempfile.mkdtemp(prefix="aic-p5-"))


@pytest.fixture()
def db(tmp_path):
    from astrbot_plugin_ai_companion.storage import MemoryDB

    d = MemoryDB(db_path=tmp_path / "p5.db",
                 schema_path=PLUGIN_DIR / "storage" / "schema.sql")
    return d


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeProvider:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def text_chat(self, prompt=None, system_prompt=None, contexts=None):
        self.calls += 1
        return type("R", (), {"completion_text": self.payload})()


class FakeContext:
    def __init__(self, provider):
        self._p = provider

    def get_provider_by_id(self, pid):
        return None

    def get_using_provider(self):
        return self._p


def _cfg(**over):
    from astrbot_plugin_ai_companion.core import CompanionConfig

    base = {
        "enable_knowledge_extraction": True,
        "extraction_min_messages": 2,
        "extraction_batch_size": 20,
    }
    base.update(over)
    return CompanionConfig(base)


def _extractor(db, payload, cfg=None):
    from astrbot_plugin_ai_companion.memory import KnowledgeExtractor

    return KnowledgeExtractor(
        db=db, context=FakeContext(FakeProvider(payload)), config=cfg or _cfg()
    )


async def _seed(db, umo="p:GroupMessage:1", n=3):
    for i in range(n):
        await db.insert_message(umo=umo, role="user", content=f"消息{i}",
                                sender_id="u1", sender_name="小明")
    await db.insert_message(umo=umo, role="assistant", content="嗯")


# ----------------------------------------------------------------------
# 存储层
# ----------------------------------------------------------------------
def test_insert_and_query_events(db):
    async def scenario():
        await db.connect()
        await db.insert_event(
            umo="p:GroupMessage:1", title="小明和小红约好周末爬山",
            summary="周六一早出发", event_type="约定", importance=0.8,
            participants=[("u1", ""), ("u2", "")],
        )
        await db.insert_event(
            umo="p:GroupMessage:2", title="另一个群的事", event_type="其他",
        )

        # 会话隔离
        ev1 = await db.get_recent_events("p:GroupMessage:1")
        assert len(ev1) == 1 and ev1[0]["title"].startswith("小明和小红")
        assert {p["entity_id"] for p in ev1[0]["participants"]} == {"u1", "u2"}

        assert len(await db.get_recent_events("p:GroupMessage:2")) == 1
        assert len(await db.get_recent_events()) == 2  # 不限会话

        # 检索
        hits = await db.search_events("爬山")
        assert len(hits) == 1
        assert await db.search_events("不存在的词") == []

        # 某人参与过的事
        mine = await db.get_events_for_entity("u1")
        assert len(mine) == 1 and mine[0]["title"].startswith("小明和小红")
        await db.close()

    _run(scenario())


# ----------------------------------------------------------------------
# 抽取
# ----------------------------------------------------------------------
def test_extraction_persists_events_with_participants(db):
    async def scenario():
        await db.connect()
        payload = (
            '{"people":[{"name":"小明","traits":["开朗"]}],'
            '"relations":[],'
            '"events":[{"title":"小明约小红周末去爬山","summary":"周六出发",'
            '"type":"约定","importance":0.8,"participants":["小明","小红"]}]}'
        )
        await _seed(db)
        ex = _extractor(db, payload)
        result = await ex.maybe_extract("p:GroupMessage:1")

        assert result.events == 1
        events = await db.get_recent_events("p:GroupMessage:1")
        assert len(events) == 1
        assert events[0]["event_type"] == "约定"
        assert abs(float(events[0]["importance"]) - 0.8) < 1e-6
        # 小明解析到平台 ID，小红为临时实体
        ids = {p["entity_id"] for p in events[0]["participants"]}
        assert "u1" in ids and "name:小红" in ids, ids
        await db.close()

    _run(scenario())


def test_duplicate_events_are_skipped(db):
    async def scenario():
        await db.connect()
        payload = (
            '{"people":[],"relations":[],'
            '"events":[{"title":"小明约小红周末去爬山","type":"约定",'
            '"importance":0.8,"participants":["小明"]}]}'
        )
        await _seed(db)
        ex = _extractor(db, payload)

        # 第一次抽取写入
        r1 = await ex.maybe_extract("p:GroupMessage:1")
        assert r1.events == 1

        # 再灌消息，模型又给出同一件事（标点/空格略有不同）-> 应去重
        await db.insert_message(umo="p:GroupMessage:1", role="user",
                                content="又说了爬山的事", sender_id="u1",
                                sender_name="小明")
        await db.insert_message(umo="p:GroupMessage:1", role="user",
                                content="嗯嗯", sender_id="u1", sender_name="小明")
        ex2 = _extractor(
            db,
            '{"people":[],"relations":[],"events":['
            '{"title":"小明约小红周末去爬山！","type":"约定","participants":[]}]}',
        )
        r2 = await ex2.maybe_extract("p:GroupMessage:1")
        assert r2.events == 0, "同一件事不应被重复记录"
        assert len(await db.get_recent_events("p:GroupMessage:1")) == 1
        await db.close()

    _run(scenario())


def test_failed_extraction_writes_no_events(db):
    async def scenario():
        await db.connect()
        await _seed(db)
        ex = _extractor(db, "这不是 JSON")
        result = await ex.maybe_extract("p:GroupMessage:1")
        assert result.events == 0
        assert await db.get_recent_events("p:GroupMessage:1") == []
        assert await db.get_extraction_cursor("p:GroupMessage:1") == 0
        await db.close()

    _run(scenario())


def test_events_hint_summarizes_recent(db):
    async def scenario():
        await db.connect()
        payload = (
            '{"people":[],"relations":[],"events":['
            '{"title":"约好周末去爬山","type":"约定","participants":[]},'
            '{"title":"小红找到新工作","type":"变化","participants":[]}]}'
        )
        await _seed(db)
        ex = _extractor(db, payload)
        await ex.maybe_extract("p:GroupMessage:1")

        hint = await ex.recent_events_hint("p:GroupMessage:1")
        assert "爬山" in hint and "新工作" in hint
        # 无事件的会话返回空
        assert await ex.recent_events_hint("p:GroupMessage:999") == ""
        await db.close()

    _run(scenario())


def test_recall_events_by_keyword(db):
    async def scenario():
        await db.connect()
        await db.insert_event(umo="p:GroupMessage:1", title="约好周末去爬山")
        await db.insert_event(umo="p:GroupMessage:1", title="小红找到新工作")
        ex = _extractor(db, '{"people":[],"relations":[],"events":[]}')

        found = await ex.recall_events("爬山", umo="p:GroupMessage:1")
        assert len(found) == 1 and "爬山" in found[0]["title"]

        recent = await ex.recall_events("", umo="p:GroupMessage:1")
        assert len(recent) == 2, "无关键词应返回最近的事件线"
        await db.close()

    _run(scenario())


def test_person_lookup_includes_events(db):
    async def scenario():
        await db.connect()
        payload = (
            '{"people":[{"name":"小明","traits":["开朗"]}],"relations":[],'
            '"events":[{"title":"小明升职了","type":"变化","participants":["小明"]}]}'
        )
        await _seed(db)
        ex = _extractor(db, payload)
        await ex.maybe_extract("p:GroupMessage:1")

        info = await ex.describe_person("小明")
        assert info["known"]
        assert info["events"] and info["events"][0]["title"] == "小明升职了"
        await db.close()

    _run(scenario())
