"""压缩（P2）测试。

覆盖三件真正会出错的事：

1. 历史未达阈值时**绝不改动**（避免无谓调用与写坏历史）。
2. 达阈值时：近期原文、checkpoint、以及原始档案都不受影响；摘要正确落位。
3. 摘要滚动合并：第二次压缩把上一轮摘要一起重新总结，早期信息不丢失。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))


class FakeProvider:
    def __init__(self, text="这是一段摘要。"):
        self.text = text
        self.calls = []

    async def text_chat(self, prompt=None, system_prompt=None, contexts=None):
        self.calls.append({"prompt": prompt, "system": system_prompt})
        return type("R", (), {"completion_text": self.text})()


class FakeContext:
    def __init__(self, provider):
        self._p = provider

    def get_provider_by_id(self, pid):
        return None

    def get_using_provider(self):
        return self._p


class FakeConversation:
    def __init__(self, cid, history):
        self.cid = cid
        self.history = json.dumps(history)


class FakeConvMgr:
    def __init__(self, conversation):
        self.conversation = conversation

    async def get_conversation(self, umo, cid):
        return self.conversation

    async def update_conversation(self, umo, cid, history=None, **kw):
        self.conversation.history = json.dumps(history)
        self.last_written = history


def _cfg(**over):
    from astrbot_plugin_ai_companion.core import CompanionConfig

    base = {
        "enable_compact": True,
        "compact_trigger_turns": 10,
        "compact_keep_recent": 4,
        "compact_min_dropped": 2,
    }
    base.update(over)
    return CompanionConfig(base)


def _history(n, start=0):
    out = []
    for i in range(start, start + n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append({"role": role, "content": f"第{i}条消息"})
    return out


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _make(conv, provider=None, cfg=None):
    from astrbot_plugin_ai_companion.memory import Compactor

    provider = provider or FakeProvider()
    conv_mgr = FakeConvMgr(conv)
    return (
        Compactor(
            conversation_manager=conv_mgr,
            db=None,
            context=FakeContext(provider),
            config=cfg or _cfg(),
        ),
        conv_mgr,
        provider,
    )


def test_below_threshold_is_noop():
    conv = FakeConversation("c1", _history(6))  # 阈值 10
    compactor, conv_mgr, provider = _make(conv)
    result = _run(compactor.maybe_compact("umo", conv))
    assert result.compacted is False
    assert not provider.calls, "未达阈值不应调用模型"
    assert not hasattr(conv_mgr, "last_written"), "未达阈值不应写历史"


def test_compact_replaces_old_and_keeps_recent():
    conv = FakeConversation("c1", _history(20))
    compactor, conv_mgr, provider = _make(conv)
    result = _run(compactor.maybe_compact("umo", conv))

    assert result.compacted is True
    assert result.dropped == 16, "20 条保留 4 条 -> 压缩 16 条"
    assert result.kept == 4
    assert len(provider.calls) == 1

    written = conv_mgr.last_written
    assert written[0]["content"].startswith("[[ai_companion_summary]]"), "摘要应在最前"
    tail = written[1:]
    assert [m["content"] for m in tail] == [f"第{i}条消息" for i in range(16, 20)], (
        "最近 4 条原文必须原样保留"
    )
    assert provider.calls[0]["system"].startswith("你负责压缩一段聊天记录")


def test_checkpoints_are_preserved():
    hist = _history(12)
    hist.insert(3, {"role": "_checkpoint", "content": {"id": "ck1"}})
    conv = FakeConversation("c1", hist)
    compactor, conv_mgr, _ = _make(conv)
    result = _run(compactor.maybe_compact("umo", conv))

    assert result.compacted is True
    written = conv_mgr.last_written
    checkpoints = [m for m in written if m.get("role") == "_checkpoint"]
    assert checkpoints == [{"role": "_checkpoint", "content": {"id": "ck1"}}], (
        "checkpoint 必须原样保留，否则平台历史与流水会错位"
    )
    assert written[0]["content"].startswith("[[ai_companion_summary]]")


def test_rolling_summary_merges_previous():
    """第二次压缩应把上一轮摘要一起重新总结，早期信息不丢。"""
    conv = FakeConversation("c1", _history(20))
    compactor, conv_mgr, provider = _make(conv)
    _run(compactor.maybe_compact("umo", conv))

    # 再灌入更多消息，触发第二次压缩
    hist = json.loads(conv_mgr.conversation.history)
    hist.extend(_history(30, start=100))
    conv_mgr.conversation.history = json.dumps(hist)

    provider.text = "合并后的新摘要。"
    result = _run(compactor.maybe_compact("umo", conv))
    assert result.compacted is True

    prompt = provider.calls[-1]["prompt"]
    assert "已有的更早摘要" in prompt, "应把旧摘要带入重新总结"
    assert "这是一段摘要" in prompt or "合并后的新摘要" in prompt
    written = conv_mgr.last_written
    summaries = [
        m
        for m in written
        if isinstance(m.get("content"), str) and m["content"].startswith("[[ai_companion_summary]]")
    ]
    assert len(summaries) == 1, "任何时刻只应存在一条摘要"


def test_summary_failure_leaves_history_untouched():
    conv = FakeConversation("c1", _history(20))
    provider = FakeProvider(text="")  # 模型返回空
    compactor, conv_mgr, _ = _make(conv, provider=provider)
    result = _run(compactor.maybe_compact("umo", conv))
    assert result.compacted is False
    assert not hasattr(conv_mgr, "last_written"), "摘要失败必须保持历史原样"


def test_disabled_by_config():
    conv = FakeConversation("c1", _history(50))
    compactor, conv_mgr, provider = _make(conv, cfg=_cfg(enable_compact=False))
    result = _run(compactor.maybe_compact("umo", conv))
    assert result.compacted is False
    assert not provider.calls
