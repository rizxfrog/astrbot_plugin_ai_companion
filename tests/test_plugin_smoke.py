"""AI Companion P0 冒烟测试。

覆盖两件真正会出错的事：
1. 插件模块能在真实 AstrBot 环境下导入并组装（捕捉 API 漂移）。
2. 记忆库的写入 / 近期读取 / 中文检索（含 trigram 短词回退）与决策链短路行为。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

# 让 StarTools.get_data_dir 落在临时目录
os.environ.setdefault("ASTRBOT_ROOT", tempfile.mkdtemp(prefix="ai-companion-test-"))


@pytest.fixture()
def db(tmp_path):
    from astrbot_plugin_ai_companion.storage import MemoryDB

    schema = PLUGIN_DIR / "storage" / "schema.sql"
    instance = MemoryDB(db_path=tmp_path / "test.db", schema_path=schema)
    yield instance


def test_plugin_module_imports():
    """模块能导入，且决策链与配置可构建。"""
    import importlib

    module = importlib.import_module("astrbot_plugin_ai_companion.main")
    assert hasattr(module, "AICompanionPlugin")

    from astrbot_plugin_ai_companion.core import CompanionConfig
    from astrbot_plugin_ai_companion.decision import (
        DecisionChain,
        HardFilterDecider,
        ProbabilityDecider,
        RateLimitDecider,
        RuleDecider,
    )

    cfg = CompanionConfig({"reply_probability": 1.0, "min_reply_interval_seconds": 0})
    assert cfg.enabled_for(is_private=True)
    assert cfg.reply_probability == 1.0

    chain = DecisionChain(
        [HardFilterDecider(), RuleDecider(), RateLimitDecider(), ProbabilityDecider()]
    )
    assert len(chain.deciders) == 4


def test_render_chain_handles_component_type_enum():
    """回归：ComponentType 是 (str, Enum)，必须取 .value 才能正确渲染。"""
    from astrbot.core.message.components import At, Plain, Reply

    from astrbot_plugin_ai_companion.context import render_chain

    chain = [
        At(qq="bot1", name="Bot"),
        Plain("你好呀"),
        Reply(id="9", sender_nickname="小明", message_str="在吗"),
    ]
    rendered = render_chain(chain, self_id="bot1")
    assert rendered == "[@我]你好呀[引用 小明: 在吗]", rendered
    assert "ComponentType" not in rendered


def test_render_formats_sender_prefix():
    from astrbot_plugin_ai_companion.context import format_for_model

    assert (
        format_for_model(sender_name="小明", sender_id="u1", content="早")
        == "小明(u1): 早"
    )
    assert (
        format_for_model(sender_name="Bot", sender_id="bot1", content="早", is_bot=True)
        == "Bot(你)(bot1): 早"
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_memory_write_and_recent(db):
    async def scenario():
        await db.connect()
        await db.insert_message(
            umo="p:GroupMessage:1", role="user", content="今天天气真好，我们去公园散步吧",
            sender_id="u1", sender_name="小明",
        )
        await db.insert_message(
            umo="p:GroupMessage:1", role="assistant", content="好呀，几点出发？",
        )
        await db.insert_message(
            umo="p:GroupMessage:2", role="user", content="另一个群的消息",
            sender_id="u2", sender_name="小红",
        )

        recent = await db.recent_messages("p:GroupMessage:1", limit=10)
        assert [r["content"] for r in recent] == [
            "今天天气真好，我们去公园散步吧",
            "好呀，几点出发？",
        ], "近期消息应按时间正序返回且只含本会话"

        assert await db.count_messages() == 3
        assert await db.count_messages("p:GroupMessage:2") == 1
        await db.close()

    _run(scenario())


def test_memory_search_cjk_and_fallback(db):
    """中文检索：≥3 字符走 FTS5 trigram，2 字符回退 LIKE。"""

    async def scenario():
        await db.connect()
        await db.insert_message(
            umo="p:GroupMessage:1", role="user",
            content="周末一起去爬山吧，山顶的景色很好",
            sender_id="u1", sender_name="小明",
        )

        # 2 字查询：trigram 无法命中，必须回退 LIKE
        short = await db.search_messages("爬山", umo="p:GroupMessage:1")
        assert len(short) == 1, "两字中文查询应通过 LIKE 回退命中"

        # 3 字查询：FTS5 trigram 命中
        long = await db.search_messages("景色", umo="p:GroupMessage:1")
        assert len(long) == 1, "三字查询应命中"

        # 会话隔离
        other = await db.search_messages("爬山", umo="p:GroupMessage:999")
        assert other == [], "检索不应跨会话泄漏"

        # 无结果
        assert await db.search_messages("不存在的关键词") == []
        await db.close()

    _run(scenario())


def test_decision_chain_short_circuits():
    """策略链短路：被 @ 的群消息由规则层直接放行，且不进入概率层。"""

    from astrbot_plugin_ai_companion.core import CompanionConfig, SessionActor
    from astrbot_plugin_ai_companion.decision import (
        DecisionChain,
        HardFilterDecider,
        ProbabilityDecider,
        RateLimitDecider,
        RuleDecider,
        TurnContext,
    )

    class FakeEvent:
        def get_self_id(self):
            return "bot1"

    cfg = CompanionConfig({"reply_probability": 0.0})  # 概率层会否决

    ctx = TurnContext(
        event=FakeEvent(), umo="p:GroupMessage:1", actor=SessionActor(umo="x"),
        config=cfg, is_private=False, is_wake=True, is_command=False,
        message_text="你好", sender_id="u1", sender_name="小明",
    )
    chain = DecisionChain(
        [HardFilterDecider(), RuleDecider(), RateLimitDecider(), ProbabilityDecider()]
    )
    decision = _run(chain.decide(ctx))
    assert decision.should_reply is True
    assert decision.decider == "rule", "被 @ 时应由规则层短路，不能被概率层否决"

    # 未被唤醒的群消息：规则层弃权 -> 概率 0 否决
    ctx2 = TurnContext(
        event=FakeEvent(), umo="p:GroupMessage:1", actor=SessionActor(umo="x"),
        config=cfg, is_private=False, is_wake=False, is_command=False,
        message_text="随便说一句", sender_id="u1", sender_name="小明",
    )
    decision2 = _run(chain.decide(ctx2))
    assert decision2.should_reply is False
    assert decision2.decider == "probability"

    # 空消息被硬过滤拦下
    ctx3 = TurnContext(
        event=FakeEvent(), umo="p:GroupMessage:1", actor=SessionActor(umo="x"),
        config=cfg, is_private=False, is_wake=True, is_command=False,
        message_text="", sender_id="u1", sender_name="小明",
    )
    decision3 = _run(chain.decide(ctx3))
    assert decision3.should_reply is False
    assert decision3.decider == "hard_filter"
