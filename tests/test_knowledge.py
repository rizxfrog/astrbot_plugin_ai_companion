"""人物与关系（P4）测试。

覆盖真正会出错的行为：

1. 全局实体：同一个人在不同会话里是同一个实体。
2. 别名归一：昵称/群名片指向同一实体。
3. 画像合并：重复抽取是**累积**而非覆盖；同一特点不重复。
4. 关系去重与强化：同一关系重复出现只留一条，强度上升。
5. 增量抽取游标：未达阈值不动、成功才推进、失败不推进（不丢不重）。
6. 发送者名 -> 实体 ID 的解析优先级。
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

os.environ.setdefault("ASTRBOT_ROOT", tempfile.mkdtemp(prefix="aic-p4-"))


@pytest.fixture()
def db(tmp_path):
    from astrbot_plugin_ai_companion.storage import MemoryDB

    d = MemoryDB(db_path=tmp_path / "p4.db",
                 schema_path=PLUGIN_DIR / "storage" / "schema.sql")
    return d


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ----------------------------------------------------------------------
# 存储层
# ----------------------------------------------------------------------
def test_entity_and_alias_are_global(db):
    """同一个人在不同会话是同一实体；别名可反查。"""

    async def scenario():
        await db.connect()
        await db.touch_entity("u1", "小明")
        await db.bind_alias("小明", "u1")
        await db.bind_alias("小明明", "u1")

        # 另一个会话写入同一实体
        await db.touch_entity("u1", "小明")

        assert await db.resolve_alias("小明") == "u1"
        assert await db.resolve_alias("小明明") == "u1"
        entity = await db.get_entity("u1")
        assert entity["last_name"] == "小明"
        assert entity["interaction_count"] == 2, "同一实体应累计互动次数"
        await db.close()

    _run(scenario())


def test_profile_merge_accumulates(db):
    async def scenario():
        await db.connect()
        await db.upsert_profile("u1", traits=["开朗"], style="说话随意")
        await db.upsert_profile("u1", traits=["开朗", "话痨"])  # 重复项应去重
        profile = await db.get_profile("u1")
        assert profile["traits"] == ["开朗", "话痨"], profile["traits"]
        assert profile["style"] == "说话随意", "未传入的字段不应被清空"

        # 再传 style 应更新
        await db.upsert_profile("u1", style="很内向")
        profile = await db.get_profile("u1")
        assert profile["style"] == "很内向"
        assert profile["traits"] == ["开朗", "话痨"], "traits 不应因只传 style 而丢"
        await db.close()

    _run(scenario())


def test_relation_dedup_and_strengthen(db):
    async def scenario():
        await db.connect()
        await db.upsert_relation("u1", "姐妹", "u2", strength=0.5, evidence="我是她姐")
        await db.upsert_relation("u1", "姐妹", "u2", strength=0.5, evidence="再次确认")

        rels = await db.get_relations("u1")
        assert len(rels) == 1, "同一关系只应有一条"
        assert rels[0]["strength"] > 0.5, "重复观察应强化关系"
        assert "我是她姐" in rels[0]["evidence"]

        # 反向也能查到（他/她作为宾语）
        rels2 = await db.get_relations("u2")
        assert len(rels2) == 1
        await db.close()

    _run(scenario())


# ----------------------------------------------------------------------
# 抽取器
# ----------------------------------------------------------------------
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
        "record_all_messages": True,
    }
    base.update(over)
    return CompanionConfig(base)


def _extractor(db, provider, cfg=None):
    from astrbot_plugin_ai_companion.memory import KnowledgeExtractor

    return KnowledgeExtractor(
        db=db, context=FakeContext(provider), config=cfg or _cfg()
    )


def test_extraction_below_threshold_is_noop(db):
    async def scenario():
        await db.connect()
        provider = FakeProvider('{"people":[],"relations":[]}')
        ex = _extractor(db, provider)
        await db.insert_message(umo="p:GroupMessage:1", role="user",
                                content="只有一条", sender_id="u1", sender_name="小明")
        result = await ex.maybe_extract("p:GroupMessage:1")
        assert result.people == 0 and result.relations == 0
        assert provider.calls == 0, "消息不足不应调用模型"
        await db.close()

    _run(scenario())


def test_extraction_saves_people_and_relations(db):
    async def scenario():
        await db.connect()
        payload = (
            '{"people":[{"name":"小明","traits":["开朗"],"style":"话多"},'
            '{"name":"小红","traits":["理性"]}],'
            '"relations":[{"a":"小明","b":"小红","relation":"姐妹",'
            '"evidence":"小明说小红是我姐"}]}'
        )
        provider = FakeProvider(payload)
        ex = _extractor(db, provider)

        for i in range(3):
            await db.insert_message(umo="p:GroupMessage:1", role="user",
                                    content=f"第{i}条", sender_id="u1",
                                    sender_name="小明")
            await db.insert_message(umo="p:GroupMessage:1", role="assistant",
                                    content=f"回复{i}")

        result = await ex.maybe_extract("p:GroupMessage:1")
        assert result.people == 2 and result.relations == 1

        # 小明用发送者 ID 解析；小红只被提及 -> 临时实体
        info = await ex.describe_person("小明")
        assert info["known"] and "开朗" in info["profile"]["traits"]
        assert info["relations"][0]["relation"] == "姐妹"
        assert info["relations"][0]["with"] == "小红"

        # 小红作为独立称呼也能查到
        info2 = await ex.describe_person("小红")
        assert info2["known"], "被提及的人也应进入人物库"
        await db.close()

    _run(scenario())


def test_cursor_advances_only_on_success(db):
    async def scenario():
        await db.connect()
        for i in range(4):
            await db.insert_message(umo="p:GroupMessage:1", role="user",
                                    content=f"消息{i}", sender_id="u1",
                                    sender_name="小明")

        # 第一次：模型返回垃圾 -> 不推进游标
        bad = _extractor(db, FakeProvider("这不是 JSON"))
        r1 = await bad.maybe_extract("p:GroupMessage:1")
        assert r1.people == 0 and r1.relations == 0
        assert await db.get_extraction_cursor("p:GroupMessage:1") == 0, (
            "失败不应推进游标，否则消息会被丢掉"
        )

        # 第二次：正常返回 -> 推进到本批最后一条
        good = _extractor(
            db, FakeProvider('{"people":[{"name":"小明","traits":["开朗"]}],"relations":[]}')
        )
        r2 = await good.maybe_extract("p:GroupMessage:1")
        assert r2.people == 1
        cursor = await db.get_extraction_cursor("p:GroupMessage:1")
        assert cursor > 0

        # 没有新消息 -> 不再调用模型（增量，不重复烧 token）
        provider3 = FakeProvider('{"people":[],"relations":[]}')
        ex3 = _extractor(db, provider3)
        await ex3.maybe_extract("p:GroupMessage:1")
        assert provider3.calls == 0, "无新消息不应重复抽取"
        await db.close()

    _run(scenario())


def test_name_resolution_priority(db):
    """称呼解析优先级：本批发送者 > 历史别名 > 临时实体。"""

    async def scenario():
        await db.connect()
        await db.bind_alias("老明", "u1")  # 历史别名
        ex = _extractor(db, FakeProvider("{}"))

        # 发送者映射优先
        assert await ex._resolve_name("小明", {"小明": "u9"}) == "u9"
        # 其次历史别名
        assert await ex._resolve_name("老明", {}) == "u1"
        # 都没有 -> 临时实体
        assert await ex._resolve_name("路人甲", {}) == "name:路人甲"
        await db.close()

    _run(scenario())


def test_schema_migration_drops_empty_legacy_tables(tmp_path):
    """v0.1~v0.3 的占位旧表（含 umo_scope）若为空应被重建为新结构。"""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE profiles (entity_id TEXT, umo_scope TEXT, display_name TEXT,"
        " PRIMARY KEY(entity_id, umo_scope));"
        "CREATE TABLE entity_aliases (umo_scope TEXT, alias TEXT, entity_id TEXT,"
        " PRIMARY KEY(umo_scope, alias));"
    )
    conn.commit()
    conn.close()

    from astrbot_plugin_ai_companion.storage import MemoryDB

    mdb = MemoryDB(db_path=db_path, schema_path=PLUGIN_DIR / "storage" / "schema.sql")

    async def scenario():
        await mdb.connect()
        # 新结构应可用（无 umo_scope）
        await mdb.upsert_profile("u1", traits=["开朗"])
        profile = await mdb.get_profile("u1")
        assert profile["traits"] == ["开朗"]
        await mdb.close()

    _run(scenario())
