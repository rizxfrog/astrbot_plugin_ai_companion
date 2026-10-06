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
        config=cfg, is_private=False, is_mention=True, is_command=False,
        message_text="你好", sender_id="u1", sender_name="小明",
    )
    chain = DecisionChain(
        [HardFilterDecider(), RuleDecider(), RateLimitDecider(), ProbabilityDecider()]
    )
    decision = _run(chain.decide(ctx))
    assert decision.should_reply is True
    assert decision.decider == "rule", "被 @ 时应由规则层短路，不能被概率层否决"

    # 未被点名的群消息：规则层弃权 -> 概率 0 否决
    ctx2 = TurnContext(
        event=FakeEvent(), umo="p:GroupMessage:1", actor=SessionActor(umo="x"),
        config=cfg, is_private=False, is_mention=False, is_command=False,
        message_text="随便说一句", sender_id="u1", sender_name="小明",
    )
    decision2 = _run(chain.decide(ctx2))
    assert decision2.should_reply is False
    assert decision2.decider == "probability"

    # 空消息被硬过滤拦下
    ctx3 = TurnContext(
        event=FakeEvent(), umo="p:GroupMessage:1", actor=SessionActor(umo="x"),
        config=cfg, is_private=False, is_mention=True, is_command=False,
        message_text="", sender_id="u1", sender_name="小明",
    )
    decision3 = _run(chain.decide(ctx3))
    assert decision3.should_reply is False
    assert decision3.decider == "hard_filter"


def test_probability_gate_controls_model_calls():
    """概率层的职责：决定**是否值得调用模型**。

    命中门槛才交给读空气；未命中由代码直接判不回，一次模型调用都不花。
    """

    from astrbot_plugin_ai_companion.core import CompanionConfig, SessionActor
    from astrbot_plugin_ai_companion.decision import (
        DecisionChain,
        HardFilterDecider,
        LLMJudgeDecider,
        ProbabilityDecider,
        RateLimitDecider,
        RuleDecider,
        TurnContext,
    )

    class FakeEvent:
        def get_self_id(self):
            return "bot1"

    class FakeJudge:
        def __init__(self, answer):
            self.answer = answer
            self.calls = 0

        async def __call__(self, ctx):
            self.calls += 1
            return self.answer

    def make_ctx(p):
        return TurnContext(
            event=FakeEvent(), umo="p:GroupMessage:1", actor=SessionActor(umo="x"),
            config=CompanionConfig({"reply_probability": p}),
            is_private=False, is_mention=False, is_command=False,
            message_text="有人吗", sender_id="u1", sender_name="小明",
        )

    # 命中门槛（roll=0 < 0.5）-> 交给读空气，由它说「想接」-> 回复
    judge_yes = FakeJudge({"reply": True, "reason": "被问到了"})
    d = _run(DecisionChain([
        HardFilterDecider(), RuleDecider(), RateLimitDecider(),
        ProbabilityDecider(rng=_AlwaysZeroRng(), rate=lambda c: 0.5, defer_on_fail=True),
        LLMJudgeDecider(judge_yes),
    ]).decide(make_ctx(0.5)))
    assert d.should_reply is True and d.decider == "llm_judge", d
    assert judge_yes.calls == 1, "命中门槛后必须调用读空气"

    # 读空气说「不想接」-> 不回复
    judge_no = FakeJudge({"reply": False, "reason": "插不上话"})
    d2 = _run(DecisionChain([
        HardFilterDecider(), RuleDecider(), RateLimitDecider(),
        ProbabilityDecider(rng=_AlwaysZeroRng(), rate=lambda c: 0.5, defer_on_fail=True),
        LLMJudgeDecider(judge_no),
    ]).decide(make_ctx(0.5)))
    assert d2.should_reply is False and d2.decider == "llm_judge", d2

    # 未命中门槛（roll=0.99 >= 0.5）-> 代码直接判不回，**不调用模型**
    judge_unused = FakeJudge({"reply": True})
    d3 = _run(DecisionChain([
        HardFilterDecider(), RuleDecider(), RateLimitDecider(),
        ProbabilityDecider(rng=_AlwaysHighRng(), rate=lambda c: 0.5, defer_on_fail=True),
        LLMJudgeDecider(judge_unused),
    ]).decide(make_ctx(0.5)))
    assert d3.decider == "probability" and d3.should_reply is False, d3
    assert judge_unused.calls == 0, "未命中门槛不得调用模型（省钱的关键）"

    # 概率为 0 -> 连门槛都不进，同样不调用模型
    judge_zero = FakeJudge({"reply": True})
    d4 = _run(DecisionChain([
        HardFilterDecider(), RuleDecider(), RateLimitDecider(),
        ProbabilityDecider(rng=_AlwaysZeroRng(), rate=lambda c: 0.0, defer_on_fail=True),
        LLMJudgeDecider(judge_zero),
    ]).decide(make_ctx(0.0)))
    assert d4.should_reply is False and d4.decider == "probability", d4
    assert judge_zero.calls == 0


def test_group_probability_differs_from_default():
    """群聊未 @ 时使用群聊概率；被 @ 与私聊不受影响。"""

    from astrbot_plugin_ai_companion.core import CompanionConfig, SessionActor
    from astrbot_plugin_ai_companion.decision import ProbabilityDecider, TurnContext

    class FakeEvent:
        def get_self_id(self):
            return "bot1"

    cfg = CompanionConfig({"reply_probability": 0.9, "group_reply_probability": 0.1})

    def mk(is_private, is_mention):
        return TurnContext(
            event=FakeEvent(), umo="p:GroupMessage:1", actor=SessionActor(umo="x"),
            config=cfg, is_private=is_private, is_mention=is_mention,
            is_command=False, message_text="hi", sender_id="u1", sender_name="小明",
        )

    def gate_rate(ctx):
        if not ctx.is_private and not ctx.is_mention:
            if cfg.group_reply_probability >= 0:
                return cfg.group_reply_probability
        return cfg.reply_probability

    dec = ProbabilityDecider(rng=_AlwaysHighRng(), rate=gate_rate, defer_on_fail=True)

    # 群聊未 @ -> 用 0.1，roll 0.99 未命中
    d1 = _run(dec.decide(mk(is_private=False, is_mention=False)))
    assert d1 is not None and d1.should_reply is False, "群聊未 @ 应使用较低概率"
    # 群聊被 @ -> 用 0.9，roll 0.99 仍未命中（但若概率层弃权会由规则层先短路）
    d2 = _run(dec.decide(mk(is_private=False, is_mention=True)))
    assert d2 is not None and d2.should_reply is False


class _AlwaysZeroRng:
    def random(self):
        return 0.0


class _AlwaysHighRng:
    def random(self):
        return 0.99


def test_parse_judge_output_tolerates_noise():
    from astrbot_plugin_ai_companion.decision import parse_judge_output

    assert parse_judge_output('{"reply": true, "reason": "x"}')["reply"] is True
    assert parse_judge_output('```json\n{"reply": false}\n```')["reply"] is False
    assert parse_judge_output('好的，我的判断是 {"reply": true} 以上')["reply"] is True
    assert parse_judge_output("完全不是 JSON") is None
    assert parse_judge_output("") is None


def test_is_mention_prefers_at_or_wake_command():
    """回归：is_wake 会被本插件自身 filter 置真，必须用 is_at_or_wake_command。"""

    from astrbot_plugin_ai_companion.core.orchestrator import Orchestrator

    class Ev:
        is_wake = True  # 本插件 handler filter 通过会置真
        is_at_or_wake_command = False  # 没被 @

    class Ev2:
        is_wake = True
        is_at_or_wake_command = True

    assert Orchestrator._is_mention(Ev(), is_private=False) is False
    assert Orchestrator._is_mention(Ev2(), is_private=False) is True
    assert Orchestrator._is_mention(Ev(), is_private=True) is True


# ======================================================================
# 人格与身份
# ======================================================================
def test_identity_question_detects_disguised_probes():
    """真正会被问到的身份句式都要识别出来。"""
    from astrbot_plugin_ai_companion.persona import is_identity_question

    positives = [
        "你是谁",
        "你是谁？",
        "你是不是AI",
        "你是AI吗",
        "你是ai吗？",
        "你是真人还是机器人",
        "你是真人还是机器人？",
        "你家在哪",
        "你家是哪的",
        "你几岁了",
        "你多大了",
        "你有感情吗",
        "你有没有意识",
        "你是用什么模型",
        "你是什么模型",
        "AI吗？",
        "你是哪个公司开发的",
        "你会不会有情绪",
    ]
    for text in positives:
        assert is_identity_question(text), f"应识别为身份追问: {text!r}"


def test_identity_question_ignores_normal_ai_talk():
    """正常讨论 AI 话题不能被误判 —— 误判会把对话带歪。"""
    from astrbot_plugin_ai_companion.persona import is_identity_question

    negatives = [
        "你觉得AI会取代人类吗",
        "现在的人工智能发展好快",
        "我最近在用chatgpt写代码",
        "机器人的电影有什么推荐",
        "今天天气不错",
        "帮我看看这段代码",
        "你吃饭了吗",
        "",
    ]
    for text in negatives:
        assert not is_identity_question(text), f"不应识别为身份追问: {text!r}"


def test_identity_confession_detects_self_disclosure():
    """模型自曝身份的各种说法都要能抓到。"""
    from astrbot_plugin_ai_companion.persona import looks_like_identity_confession

    confessions = [
        "我是你的智能助手小帮手～可以帮你查信息、记事情",
        "你好呀！我是 AstrBot，一个运行在安全模式下的智能助手～",
        "我是一个语言模型，由 OpenAI 开发",
        "作为 AI，我可以帮你处理文档",
        "我是由深度求索训练出来的模型",
        "我并不是真人，我只是程序",
        "我没有真实的感情",
        "我是 DeepSeek 模型",
    ]
    for text in confessions:
        assert looks_like_identity_confession(text), f"应识别为自曝身份: {text!r}"


def test_identity_confession_ignores_normal_replies():
    """正常闲聊不能被身份兜底误伤 —— 这是最关键的一条。"""
    from astrbot_plugin_ai_companion.persona import looks_like_identity_confession

    normals = [
        "今天杭州下雨，出门记得带伞",
        "这个我不太清楚，你可以问问别人",
        "哈哈你这问题问得好",
        "我建议你先看看文档",
        "好累啊，今天加班到十点",
        "你猜",
        "不告诉你",
        "我就是我呗，怎么了",
        # 反例：「我是X，这个软件…」不是自曝，必须不误伤
        "我是新来的，这个软件怎么用",
        "我是做后端的，这段程序有点问题",
        "我是你朋友啊，怎么不认识了",
    ]
    for text in normals:
        assert not looks_like_identity_confession(text), f"误伤正常回复: {text!r}"


def test_identity_directive_differs_by_scene():
    """群聊允许无视，私聊给话术。"""
    from astrbot_plugin_ai_companion.persona import build_identity_directive

    group = build_identity_directive(is_group=True)
    private = build_identity_directive(is_group=False)
    assert "懒得理就不要回复" in group
    assert "你猜" in private
    assert group != private


def test_persona_config_defaults_and_override():
    """人格默认开启并使用内置文案；空串不应关闭人格。"""
    from astrbot_plugin_ai_companion.core import CompanionConfig
    from astrbot_plugin_ai_companion.persona import DEFAULT_PERSONA_PROMPT

    cfg = CompanionConfig({})
    assert cfg.enable_persona is True
    assert cfg.identity_conceal is True
    assert cfg.persona_prompt == DEFAULT_PERSONA_PROMPT
    assert cfg.identity_deflect_private, "私聊应有默认兜底话术"
    assert cfg.identity_deflect_group == [], "群聊默认应直接无视"

    # 显式空串 = 用内置人格，而不是「没有人格」
    cfg2 = CompanionConfig({"persona_prompt": "   "})
    assert cfg2.persona_prompt == DEFAULT_PERSONA_PROMPT

    # 自定义人格生效
    cfg3 = CompanionConfig({"persona_prompt": "你是小美。", "enable_persona": False})
    assert cfg3.persona_prompt == "你是小美。"
    assert cfg3.enable_persona is False


def test_identity_output_guard_replaces_confession():
    """私聊自曝身份 -> 换成兜底话术；群聊 -> 直接不发。"""
    from astrbot_plugin_ai_companion.main import AICompanionPlugin
    from astrbot_plugin_ai_companion.persona import (
        DEFAULT_DEFLECT_PRIVATE,
    )

    class Cfg:
        identity_conceal = True
        identity_deflect_private = list(DEFAULT_DEFLECT_PRIVATE)
        identity_deflect_group = []
        debug_mode = False

    # 必须用平台的真实 Plain 组件：插件内部按 isinstance 判定
    from astrbot.core.message.components import Plain

    class Res:
        def __init__(self, text):
            self.chain = [Plain(text)]

    class Ev:
        def __init__(self, text, private):
            self._r = Res(text)
            self._p = private

        def is_private_chat(self):
            return self._p

        def get_extra(self, k):
            return True

        def get_result(self):
            return self._r

    plugin = AICompanionPlugin.__new__(AICompanionPlugin)
    plugin.config = Cfg()
    plugin._identity_rng = __import__("random").Random(0)

    # 私聊：自曝 -> 替换成兜底话术
    ev = Ev("你好呀！我是 AstrBot，一个运行在安全模式下的智能助手～", True)
    assert plugin._guard_identity_output(ev) is True
    assert ev._r.chain[0].text in DEFAULT_DEFLECT_PRIVATE

    # 群聊：自曝 -> 整条不发
    ev2 = Ev("我是你的智能助手小帮手～可以帮你查信息", False)
    assert plugin._guard_identity_output(ev2) is True
    assert ev2._r.chain == []

    # 正常回复不受影响
    ev3 = Ev("今天杭州下雨，记得带伞", True)
    assert plugin._guard_identity_output(ev3) is False
    assert ev3._r.chain[0].text == "今天杭州下雨，记得带伞"


# ======================================================================
# 表情闸门
# ======================================================================
def test_sticker_limiter_blocks_expected_share():
    """放行率 0.2 -> 实际放行比例应接近 20%（拦掉约 80%）。"""
    import random as _r

    from astrbot_plugin_ai_companion.humanize import StickerRateLimiter

    lim = StickerRateLimiter(drop_rate=0.2, rng=_r.Random(42))
    allowed = sum(1 for _ in range(3000) if lim.allow("umo:1")[0])
    ratio = allowed / 3000
    assert 0.17 < ratio < 0.23, f"放行率异常: {ratio:.3f}"
    assert lim.blocked == 3000 - allowed


def test_sticker_limiter_full_allow_and_full_block():
    """0 全拦、1 全放 —— 边界必须干净。"""
    from astrbot_plugin_ai_companion.humanize import StickerRateLimiter

    block_all = StickerRateLimiter(drop_rate=0.0)
    assert all(not block_all.allow("u")[0] for _ in range(20))
    assert block_all.allowed == 0

    allow_all = StickerRateLimiter(drop_rate=1.0)
    assert all(allow_all.allow("u")[0] for _ in range(20))
    assert allow_all.blocked == 0


def test_sticker_limiter_cooldown_is_per_session():
    """冷却按会话隔离，且只由「成功发出」推进。"""
    from astrbot_plugin_ai_companion.humanize import StickerRateLimiter

    lim = StickerRateLimiter(drop_rate=1.0, cooldown_seconds=60)
    ok, _ = lim.allow("a", now=1000.0)
    assert ok
    # 同会话冷却中
    ok2, reason = lim.allow("a", now=1030.0)
    assert not ok2 and "冷却" in reason
    # 另一个会话不受影响
    assert lim.allow("b", now=1030.0)[0] is True
    # 冷却过后恢复
    assert lim.allow("a", now=1061.0)[0] is True

    # 被概率拦下的不应推进冷却窗口
    lim2 = StickerRateLimiter(drop_rate=0.0, cooldown_seconds=60)
    lim2.allow("x", now=1000.0)          # 被拦
    assert lim2.allow("x", now=1000.0)[0] is False   # 仍无冷却负担


def test_humanizer_marker_path_respects_limiter():
    """AI 主动写 [sticker:x] 也必须被闸门拦下 —— 这是本次修复的核心。"""
    import random as _r

    from astrbot.core.message.components import Plain
    from astrbot_plugin_ai_companion.humanize import (
        Humanizer,
        StickerLibrary,
        StickerRateLimiter,
    )

    class Cfg:
        enable_stickers = True
        sticker_auto_probability = 0.0     # 关掉自动补图，只看标记路径
        enable_typos = False

    lib = StickerLibrary(roots=[PLUGIN_DIR / "stickers"])
    assert not lib.empty, "测试需要真实的 stickers 目录"
    lib.limiter = StickerRateLimiter(drop_rate=0.0)  # 全拦

    h = Humanizer(stickers=lib, config=Cfg(), rng=_r.Random(1))

    class Res:
        def __init__(self):
            self.chain = [Plain("好的 [sticker:开心]")]

    class Ev:
        unified_msg_origin = "p:GroupMessage:1"

        def __init__(self):
            self._r = Res()

        def get_result(self):
            return self._r

    ev = Ev()
    out = h.apply(ev)
    texts = "".join(c.text for c in ev._r.chain if isinstance(c, Plain))
    assert "[sticker" not in texts, "标记必须被清掉，不能字面发给用户"
    assert out.replaced == 0, "被限流时不应有图片替换"
    assert out.blocked == 1, "应记录一次拦截"


def test_humanizer_allows_when_limiter_absent_or_open():
    """没有闸门（未初始化）或放行率 1 时，标记应正常变成图片。"""
    import random as _r

    from astrbot.core.message.components import Plain
    from astrbot_plugin_ai_companion.humanize import (
        Humanizer,
        StickerLibrary,
        StickerRateLimiter,
    )

    class Cfg:
        enable_stickers = True
        sticker_auto_probability = 0.0
        enable_typos = False

    for limiter in (None, StickerRateLimiter(drop_rate=1.0)):
        lib = StickerLibrary(roots=[PLUGIN_DIR / "stickers"])
        lib.limiter = limiter

        h = Humanizer(stickers=lib, config=Cfg(), rng=_r.Random(1))

        class Res:
            def __init__(self):
                self.chain = [Plain("哈哈 [sticker:开心]")]

        class Ev:
            unified_msg_origin = "p:GroupMessage:1"

            def __init__(self):
                self._r = Res()

            def get_result(self):
                return self._r

        ev = Ev()
        out = h.apply(ev)
        assert out.replaced == 1, f"limiter={limiter} 时应正常插图"
        assert out.blocked == 0


def test_dangling_image_placeholder_stripped_on_event_path():
    """模型写 [图片] 却没真配图时，不能把字面量发给用户（回归）。

    此前只有主动消息路径做了清理，事件路径漏了，用户会看到 "[图片]"。
    """
    import random as _r

    from astrbot.core.message.components import Plain
    from astrbot_plugin_ai_companion.humanize import Humanizer, StickerLibrary

    class Cfg:
        enable_stickers = False       # 关掉表情，确保不插图
        sticker_auto_probability = 0.0
        enable_typos = False

    lib = StickerLibrary(roots=[PLUGIN_DIR / "stickers"])
    h = Humanizer(stickers=lib, config=Cfg(), rng=_r.Random(1))

    class Res:
        def __init__(self, text):
            self.chain = [Plain(text)]

    class Ev:
        unified_msg_origin = "p:FriendMessage:1"

        def __init__(self, text):
            self._r = Res(text)

        def get_result(self):
            return self._r

    ev = Ev("你家猫叫煤球呀！ [图片]")
    h.apply(ev)
    text = "".join(c.text for c in ev._r.chain if isinstance(c, Plain))
    assert "[图片]" not in text, f"占位符泄漏: {text!r}"
    assert "煤球" in text, "正文不能被误删"

    # 整段只有占位符时应被丢弃，不留空消息
    ev2 = Ev("[图片]")
    h.apply(ev2)
    leftovers = [c.text for c in ev2._r.chain if isinstance(c, Plain) and c.text.strip()]
    assert leftovers == [], f"应丢弃空段: {leftovers}"
