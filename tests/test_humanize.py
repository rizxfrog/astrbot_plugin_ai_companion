"""拟人增强（P6）测试。

覆盖真正会出错的行为：

1. 表情标记 `[sticker:分类]` 被替换成真实图片组件，且分类正确。
2. 未知分类回退到通用池；库为空时不影响文本。
3. 自动补表情只在该轮没发过时触发，且受概率控制（0 = 永不）。
4. 标记不会残留到文本里。
5. 错别字：只改高频同音字，绝不碰标点/数字/英文/表情标记；概率 0 时零改动。
"""

from __future__ import annotations

import random
import sys
import tempfile
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))
import os

os.environ.setdefault("ASTRBOT_ROOT", tempfile.mkdtemp(prefix="aic-p6-"))

from astrbot.core.message.components import Image, Plain  # noqa: E402


class FakeResult:
    def __init__(self, chain):
        self.chain = chain


class FakeEvent:
    def __init__(self, chain):
        self._result = FakeResult(chain)

    def get_result(self):
        return self._result

    def is_private_chat(self):
        return False


class FakeRng:
    """可控随机数：random() 返回值固定，choice/shuffle 走真实逻辑。"""

    def __init__(self, value=0.0):
        self.value = value
        self._real = random.Random(42)

    def random(self):
        return self.value

    def choice(self, seq):
        return self._real.choice(list(seq))

    def shuffle(self, seq):
        self._real.shuffle(seq)


def _sticker_tree(tmp_path: Path) -> Path:
    root = tmp_path / "stickers"
    for cat, names in {"开心": ["a.jpg", "b.png"], "无语": ["c.gif"], "通用": ["d.jpg"]}.items():
        d = root / cat
        d.mkdir(parents=True)
        for n in names:
            (d / n).write_bytes(b"fake-image")
    # 根目录下的图片 -> 通用
    (root / "root.png").write_bytes(b"fake-image")
    return root


def _cfg(**over):
    from astrbot_plugin_ai_companion.core import CompanionConfig

    base = {
        "enable_stickers": True,
        "sticker_auto_probability": 0.0,
        "enable_typos": False,
        "typo_probability": 0.0,
    }
    base.update(over)
    return CompanionConfig(base)


def _lib(tmp_path):
    from astrbot_plugin_ai_companion.humanize import StickerLibrary

    return StickerLibrary(roots=[_sticker_tree(tmp_path)])


def _humanizer(tmp_path, cfg, rng=None):
    from astrbot_plugin_ai_companion.humanize import Humanizer

    return Humanizer(stickers=_lib(tmp_path), config=cfg, rng=rng or FakeRng())


# ----------------------------------------------------------------------
# 表情包库
# ----------------------------------------------------------------------
def test_library_scans_categories(tmp_path):
    lib = _lib(tmp_path)
    assert set(lib.categories()) == {"开心", "无语", "通用"}
    # 通用池含根目录图片
    assert len(lib._index["通用"]) == 2
    assert lib.pick("开心") is not None
    assert lib.pick("不存在") is not None, "未知分类应回退到通用池"


def test_library_resolves_category(tmp_path):
    lib = _lib(tmp_path)
    assert lib.resolve_category("开心") == "开心"
    assert lib.resolve_category("") == "通用"
    assert lib.resolve_category("没这个") == "通用"


# ----------------------------------------------------------------------
# 表情替换
# ----------------------------------------------------------------------
def test_sticker_marker_replaced_with_image(tmp_path):
    h = _humanizer(tmp_path, _cfg())
    event = FakeEvent([Plain("好耶！[sticker:开心]")])
    result = h.apply(event)

    assert result.replaced == 1
    chain = event.get_result().chain
    texts = [c.text for c in chain if isinstance(c, Plain)]
    images = [c for c in chain if isinstance(c, Image)]
    assert len(images) == 1
    assert images[0].path and images[0].path.startswith(str(tmp_path))
    assert "[sticker" not in "".join(texts), "标记不应留在文本里"
    assert "好耶！" in texts


def test_unknown_category_falls_back(tmp_path):
    h = _humanizer(tmp_path, _cfg())
    event = FakeEvent([Plain("呃[sticker:根本没有这个分类]")])
    result = h.apply(event)
    assert result.replaced == 1, "未知分类应回退通用池，仍要发一张"


def test_sticker_only_reply_keeps_image(tmp_path):
    h = _humanizer(tmp_path, _cfg())
    event = FakeEvent([Plain("[sticker:无语]")])
    result = h.apply(event)
    assert result.replaced == 1
    images = [c for c in event.get_result().chain if isinstance(c, Image)]
    assert len(images) == 1


def test_no_library_still_strips_markers(tmp_path):
    """表情库为空时，标记也绝不能原样发给用户。"""
    from astrbot_plugin_ai_companion.humanize import Humanizer, StickerLibrary

    empty = StickerLibrary(roots=[tmp_path / "nope"])
    assert empty.empty
    h = Humanizer(stickers=empty, config=_cfg(), rng=FakeRng())
    event = FakeEvent([Plain("文字[sticker:开心]")])
    result = h.apply(event)
    assert result.replaced == 0
    text = event.get_result().chain[0].text
    assert "文字" in text
    assert "sticker" not in text, "标记必须被消掉，不能字面发给用户"

    # 纯标记的消息：清理后为空，应整段移除
    event2 = FakeEvent([Plain("[sticker:开心]")])
    h.apply(event2)
    assert event2.get_result().chain == []


def test_markers_stripped_when_feature_disabled(tmp_path):
    from astrbot_plugin_ai_companion.humanize import Humanizer

    h = Humanizer(stickers=_lib(tmp_path), config=_cfg(enable_stickers=False), rng=FakeRng())
    event = FakeEvent([Plain("你好[sticker:开心]")])
    h.apply(event)
    chain = event.get_result().chain
    assert len(chain) == 1, "清理后不应重复插入原文"
    text = chain[0].text
    assert text == "你好", text
    assert "sticker" not in text, "关闭表情功能也要清掉标记"


# ----------------------------------------------------------------------
# 自动补表情
# ----------------------------------------------------------------------
def test_auto_sticker_appended_when_enabled(tmp_path):
    h = _humanizer(
        tmp_path, _cfg(sticker_auto_probability=1.0), rng=FakeRng(value=0.0)
    )
    event = FakeEvent([Plain("随便说点什么")])
    result = h.apply(event)
    assert result.appended is True
    assert any(isinstance(c, Image) for c in event.get_result().chain)


def test_auto_sticker_not_added_when_already_sent(tmp_path):
    h = _humanizer(
        tmp_path, _cfg(sticker_auto_probability=1.0), rng=FakeRng(value=0.0)
    )
    event = FakeEvent([Plain("哈哈[sticker:开心]")])
    result = h.apply(event)
    assert result.replaced == 1
    assert result.appended is False, "本轮已发表情，不应再补一张"
    images = [c for c in event.get_result().chain if isinstance(c, Image)]
    assert len(images) == 1


def test_auto_sticker_probability_zero(tmp_path):
    h = _humanizer(
        tmp_path, _cfg(sticker_auto_probability=0.0), rng=FakeRng(value=0.0)
    )
    event = FakeEvent([Plain("没有表情")])
    result = h.apply(event)
    assert result.appended is False


# ----------------------------------------------------------------------
# 错别字
# ----------------------------------------------------------------------
def test_typo_never_corrupts_punct_numbers_markers():
    from astrbot_plugin_ai_companion.humanize import apply_typos

    rng = random.Random(1)
    text = "我1234觉得很好，see you！[sticker:开心]"
    original_marker = "[sticker:开心]"
    for _ in range(200):
        out = apply_typos(text, probability=1.0, rng=rng, max_typos=1)
        assert original_marker in out, "表情标记不能被改坏"
        assert "1234" in out, "数字不能被改"
        assert "see you" in out, "英文不能被改"
        assert "，" in out and "！" in out, "标点不能被改"


def test_typo_only_replaces_confusable_chars():
    from astrbot_plugin_ai_companion.humanize.typo import CONFUSIONS, apply_typos

    rng = random.Random(7)
    text = "我觉得这个很好的"
    out = apply_typos(text, probability=1.0, rng=rng, max_typos=1)
    assert len(out) == len(text), "只替换字符，不增删"
    diffs = [(a, b) for a, b in zip(text, out) if a != b]
    assert len(diffs) == 1, "max_typos=1 只改一个字"
    src, dst = diffs[0]
    assert dst in CONFUSIONS[src], f"{src}->{dst} 不在混淆表中"


def test_typo_probability_zero():
    from astrbot_plugin_ai_companion.humanize import apply_typos

    text = "我觉得这个很好"
    assert apply_typos(text, probability=0.0, rng=random.Random(0)) == text


def test_typo_applies_through_humanizer(tmp_path):
    h = _humanizer(
        tmp_path, _cfg(enable_typos=True, typo_probability=1.0), rng=FakeRng(value=0.0)
    )
    event = FakeEvent([Plain("我觉得挺好的")])
    result = h.apply(event)
    assert result.typo is True


def test_marker_stripping_helper():
    from astrbot_plugin_ai_companion.humanize import Humanizer

    assert Humanizer.strip_sticker_markers("哈[sticker:开心]哈") == "哈哈"
    assert Humanizer.strip_sticker_markers("你好[sticker:开心]呀") == "你好呀"
    assert Humanizer.strip_sticker_markers("[表情]") == ""
    assert Humanizer.strip_sticker_markers("纯文本") == "纯文本"


# ----------------------------------------------------------------------
# 主动消息路径（没有事件对象，走 apply_to_text）
# ----------------------------------------------------------------------
def test_apply_to_text_replaces_marker_with_image(tmp_path):
    """回归：主动消息不经过 on_decorating_result，必须能自己处理标记。"""
    h = _humanizer(tmp_path, _cfg())
    text, images = h.apply_to_text("好耶！[sticker:开心]")
    assert text == "好耶！"
    assert len(images) == 1
    assert Path(images[0].path).parent.name == "开心"


def test_apply_to_text_drops_dangling_placeholder(tmp_path):
    """回归：模型常写 [图片] 却拿不到图片，不能把字面量发给用户。"""
    h = _humanizer(tmp_path, _cfg(sticker_auto_probability=0.0))
    text, images = h.apply_to_text("给你发个开心小表情包～ [图片]")
    assert "[图片]" not in text
    assert text.strip() == "给你发个开心小表情包～"
    assert images == []


def test_apply_to_text_keeps_placeholder_removed_variants(tmp_path):
    h = _humanizer(tmp_path, _cfg(sticker_auto_probability=1.0), rng=FakeRng(0.0))
    # 有图时不摘占位（因为确实附了图，由图片本身承载）
    text, images = h.apply_to_text("看这个 [图片]")
    assert len(images) == 1


def test_apply_to_text_without_library_strips_markers(tmp_path):
    from astrbot_plugin_ai_companion.humanize import Humanizer, StickerLibrary

    h = Humanizer(
        stickers=StickerLibrary(roots=[tmp_path / "none"]),
        config=_cfg(), rng=FakeRng(),
    )
    text, images = h.apply_to_text("你好[sticker:开心]")
    assert text == "你好"
    assert images == []


# ----------------------------------------------------------------------
# 工具参数净化（回归：模型会构造平台不认识的段的类型）
# ----------------------------------------------------------------------
def _sanitize(messages):
    """复现 main.on_using_llm_tool 的净化逻辑。"""
    for item in messages:
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "")).lower() == "sticker":
            item["type"] = "plain"
            item["text"] = ""
        for key in ("text", "content"):
            v = item.get(key)
            if isinstance(v, str) and v:
                from astrbot_plugin_ai_companion.humanize.humanizer import (
                    DANGLING_PLACEHOLDER_PATTERN,
                    STICKER_PATTERN,
                )

                v = STICKER_PATTERN.sub("", v)
                v = DANGLING_PLACEHOLDER_PATTERN.sub("", v).strip()
                item[key] = v
    out = [
        m for m in messages
        if not (isinstance(m, dict) and str(m.get("type", "plain")).lower() == "plain"
                and not str(m.get("text", "")).strip())
    ]
    return out or [{"type": "plain", "text": "（表情）"}]


def test_tool_args_unknown_sticker_segment_downgraded():
    """模型写的 {'type':'sticker'} 必须降级为合法文本，否则平台会报错。"""
    msgs = [
        {"type": "plain", "text": "早安呀～"},
        {"type": "sticker", "text": "开心"},
    ]
    out = _sanitize(msgs)
    assert all(m["type"] == "plain" for m in out), out
    assert len(out) == 1 and out[0]["text"] == "早安呀～"


def test_tool_args_strips_sticker_marker_and_placeholder():
    msgs = [{"type": "plain", "text": "开心哦 [sticker:开心] [图片]"}]
    out = _sanitize(msgs)
    assert out[0]["text"] == "开心哦"


def test_tool_args_all_empty_becomes_placeholder():
    msgs = [{"type": "plain", "text": "[sticker:开心]"}]
    out = _sanitize(msgs)
    assert out == [{"type": "plain", "text": "（表情）"}]
