"""主动消息（P3）测试。

覆盖真正会出错的行为：

1. 会话类型判定（平台名含 group 不得误判）。
2. 候选筛选：沉默阈值、冷却、连续未回复上限、抖动稳定。
3. 完整发言流程：生成 → 发送 → 计数 +1 → 落库。
4. 抢锁后复查：等待期间用户已说话则放弃。
5. 连续无人回应后进入冷却。
6. 运行态持久化与恢复。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))
import os

os.environ.setdefault("ASTRBOT_ROOT", tempfile.mkdtemp(prefix="aic-p3-"))


# ----------------------------------------------------------------------
# 会话类型
# ----------------------------------------------------------------------
def test_is_group_umo_avoids_platform_name_false_positive():
    from astrbot_plugin_ai_companion.core.proactive import is_group_umo

    assert is_group_umo("default:GroupMessage:123") is True
    assert is_group_umo("default:FriendMessage:123") is False
    # 平台名里含 group，但消息类型是私聊 -> 不能误判成群
    assert is_group_umo("qq_group_bot:FriendMessage:123") is False
    assert is_group_umo("myguild:FriendMessage:9") is False
    assert is_group_umo("default:PrivateMessage:9") is False


# ----------------------------------------------------------------------
# 候选筛选
# ----------------------------------------------------------------------
def _cfg(**over):
    from astrbot_plugin_ai_companion.core import CompanionConfig

    base = {
        "enable_proactive": True,
        "proactive_threshold_minutes": 60,
        "proactive_max_unanswered": 2,
        "proactive_group": True,
        "proactive_private": True,
        "record_all_messages": True,
    }
    base.update(over)
    return CompanionConfig(base)


def test_candidates_respect_threshold_and_limits():
    from astrbot_plugin_ai_companion.core import SessionRegistry

    reg = SessionRegistry()
    now = time.time()

    fresh = reg.get("p:GroupMessage:fresh")
    fresh.last_message_ts = now - 10  # 未达阈值

    silent = reg.get("p:GroupMessage:silent")
    silent.last_message_ts = now - 3600 * 2  # 沉默 2 小时

    cooling = reg.get("p:GroupMessage:cooling")
    cooling.last_message_ts = now - 3600 * 2
    cooling.cooldown_until = now + 600

    capped = reg.get("p:GroupMessage:capped")
    capped.last_message_ts = now - 3600 * 2
    capped.unanswered_count = 2  # 已达上限

    cands = reg.proactive_candidates(
        threshold_seconds=3600, max_unanswered=2, now=now
    )
    names = {a.umo for a in cands}
    assert names == {"p:GroupMessage:silent"}, names


def test_jitter_is_stable_per_session():
    from astrbot_plugin_ai_companion.core import SessionRegistry

    reg = SessionRegistry()
    a = reg.get("p:GroupMessage:aaa")
    a.last_message_ts = time.time() - 3600
    # 同一 umo 多次调用结果一致（不会忽快忽慢地抖动）
    first = SessionRegistry._jitter("p:GroupMessage:aaa", 1000)
    assert first == SessionRegistry._jitter("p:GroupMessage:aaa", 1000)
    assert 0 <= first <= 1000


# ----------------------------------------------------------------------
# 发言流程
# ----------------------------------------------------------------------
class FakeProvider:
    def __init__(self, text="在干嘛呢？"):
        self.text = text
        self.calls = []

    async def text_chat(self, prompt=None, system_prompt=None, contexts=None):
        self.calls.append({"prompt": prompt, "system": system_prompt, "contexts": contexts})
        return type("R", (), {"completion_text": self.text})()


class FakePersonaMgr:
    async def get_default_persona_v3(self, umo):
        return {"prompt": "你是小助手，说话活泼。"}


class FakeContext:
    def __init__(self, provider):
        self._p = provider
        self.persona_manager = FakePersonaMgr()
        self.sent = []

    def get_provider_by_id(self, pid):
        return None

    def get_using_provider(self, umo=None):
        return self._p

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return True


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture()
def db(tmp_path):
    from astrbot_plugin_ai_companion.storage import MemoryDB

    d = MemoryDB(db_path=tmp_path / "p3.db",
                 schema_path=PLUGIN_DIR / "storage" / "schema.sql")
    return d


def _make(cfg, db, provider=None):
    from astrbot_plugin_ai_companion.core import ProactiveScheduler, SessionRegistry

    provider = provider or FakeProvider()
    ctx = FakeContext(provider)
    reg = SessionRegistry()
    sched = ProactiveScheduler(
        config=cfg, registry=reg, db=db, context=ctx, conversation_manager=None,
    )
    return sched, reg, ctx, provider


def test_proactive_speaks_and_counts(db):
    async def scenario():
        await db.connect()
        cfg = _cfg(proactive_sessions=["p:GroupMessage:g1"])
        sched, reg, ctx, provider = _make(cfg, db)

        actor = reg.get("p:GroupMessage:g1")
        actor.last_message_ts = time.time() - 7200
        await db.insert_message(umo=actor.umo, role="user",
                                content="我先去忙了", sender_name="小明")

        await sched._run_for(actor)

        assert provider.calls, "应调用模型生成主动消息"
        assert ctx.sent, "应发送主动消息"
        assert actor.unanswered_count == 1, "未回复计数应 +1"
        assert actor.last_proactive_ts > 0
        # 提示词应包含沉默时长
        assert "120" in provider.calls[0]["prompt"]

        # 已落库并标记为主动
        rows = await db.recent_messages(actor.umo, limit=5)
        assert any(r["is_proactive"] for r in rows), "主动消息应落库并标记"
        await db.close()

    _run(scenario())


def test_proactive_aborts_if_user_spoke_while_waiting(db):
    async def scenario():
        await db.connect()
        cfg = _cfg()
        sched, reg, ctx, provider = _make(cfg, db)
        actor = reg.get("p:GroupMessage:g2")
        actor.last_message_ts = time.time() - 7200

        # 模拟：刚抢到锁，用户说话了
        actor.last_message_ts = time.time()
        await sched._run_for(actor)

        assert not provider.calls, "抢锁后复查发现已有新消息，必须放弃"
        assert not ctx.sent
        await db.close()

    _run(scenario())


def test_cooldown_after_repeated_silence(db):
    """连续两轮「主动后仍无人回应」应进入冷却。

    注意：AI 每次主动发言本身会刷新会话活跃时间，因此下一轮必须**再沉默一个
    完整阈值**才会再次开口——这正是不刷屏的关键。测试中显式推进时间。
    """

    async def scenario():
        await db.connect()
        cfg = _cfg(proactive_max_unanswered=2, proactive_cooldown_minutes=30)
        sched, reg, ctx, provider = _make(cfg, db)
        actor = reg.get("p:GroupMessage:g3")

        for _ in range(2):
            # 模拟又过去了一个完整沉默周期（用户始终没有回应）
            actor.last_message_ts = time.time() - 7200
            actor.last_proactive_ts = time.time() - 7200
            await sched._run_for(actor)

        assert actor.unanswered_count == 2
        assert actor.cooldown_until > time.time(), "连续被无视应进入冷却"
        await db.close()

    _run(scenario())


def test_no_repeat_within_one_silence_window(db):
    """主动发言后若未再沉默一个周期，不应立刻重复开口。"""

    async def scenario():
        await db.connect()
        cfg = _cfg()
        sched, reg, ctx, provider = _make(cfg, db)
        actor = reg.get("p:GroupMessage:g4")
        actor.last_message_ts = time.time() - 7200

        await sched._run_for(actor)
        assert len(ctx.sent) == 1

        # 紧接着再跑一次：AI 自己刚说过话，活跃时间被刷新 -> 必须放弃
        await sched._run_for(actor)
        assert len(ctx.sent) == 1, "不得在同一沉默窗口内重复主动发言"
        await db.close()

    _run(scenario())


def test_whitelist_gating():
    from astrbot_plugin_ai_companion.core import ProactiveScheduler, SessionRegistry

    cfg = _cfg(proactive_sessions=["p:GroupMessage:allowed"])
    reg = SessionRegistry()
    sched = ProactiveScheduler(config=cfg, registry=reg, db=None,
                               context=FakeContext(FakeProvider()),
                               conversation_manager=None)
    assert sched._is_allowed("p:GroupMessage:allowed", cfg) is True
    assert sched._is_allowed("p:GroupMessage:other", cfg) is False, "不在白名单应拒绝"


def test_whitelist_is_the_real_gate():
    """准入依据是会话白名单；类型开关只是粗过滤。

    这样「把群加进白名单」即可生效，不会因为还差一个开关而静默无效。
    """
    from astrbot_plugin_ai_companion.core import ProactiveScheduler, SessionRegistry

    cfg = _cfg(proactive_sessions=[])
    sched = ProactiveScheduler(config=cfg, registry=SessionRegistry(), db=None,
                               context=FakeContext(FakeProvider()),
                               conversation_manager=None)
    # 白名单为空 -> 谁都不主动（安全默认）
    assert sched._is_allowed("p:GroupMessage:x", cfg) is False
    assert sched._is_allowed("p:FriendMessage:x", cfg) is False

    # 显式关闭某类型时，即使白名单命中也不主动
    cfg2 = _cfg(proactive_sessions=["p:GroupMessage:x"], proactive_group=False)
    sched2 = ProactiveScheduler(config=cfg2, registry=SessionRegistry(), db=None,
                                context=FakeContext(FakeProvider()),
                                conversation_manager=None)
    assert sched2._is_allowed("p:GroupMessage:x", cfg2) is False

    # "*" 表示主动全部会话
    cfg3 = _cfg(proactive_sessions=["*"])
    sched3 = ProactiveScheduler(config=cfg3, registry=SessionRegistry(), db=None,
                                context=FakeContext(FakeProvider()),
                                conversation_manager=None)
    assert sched3._is_allowed("p:GroupMessage:anything", cfg3) is True


def test_state_persistence_roundtrip(db):
    async def scenario():
        await db.connect()
        await db.upsert_session_state(
            "p:GroupMessage:g9",
            last_message_ts=111.0, unanswered_count=2, last_proactive_ts=222.0,
        )
        rows = await db.load_session_states()
        match = [r for r in rows if r["umo"] == "p:GroupMessage:g9"]
        assert match and int(match[0]["unanswered_count"]) == 2
        assert float(match[0]["last_proactive_ts"]) == 222.0
        await db.close()

    _run(scenario())


def test_scheduler_disabled_by_default():
    from astrbot_plugin_ai_companion.core import CompanionConfig

    assert CompanionConfig({}).enable_proactive is False, (
        "主动消息必须默认关闭，避免意外打扰"
    )
