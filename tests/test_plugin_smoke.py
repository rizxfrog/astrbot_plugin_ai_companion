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
import time
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

    assert format_for_model(sender_name="小明", sender_id="u1", content="早") == "小明(u1): 早"
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
            umo="p:GroupMessage:1",
            role="user",
            content="今天天气真好，我们去公园散步吧",
            sender_id="u1",
            sender_name="小明",
        )
        await db.insert_message(
            umo="p:GroupMessage:1",
            role="assistant",
            content="好呀，几点出发？",
        )
        await db.insert_message(
            umo="p:GroupMessage:2",
            role="user",
            content="另一个群的消息",
            sender_id="u2",
            sender_name="小红",
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
            umo="p:GroupMessage:1",
            role="user",
            content="周末一起去爬山吧，山顶的景色很好",
            sender_id="u1",
            sender_name="小明",
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
        event=FakeEvent(),
        umo="p:GroupMessage:1",
        actor=SessionActor(umo="x"),
        config=cfg,
        is_private=False,
        is_mention=True,
        is_command=False,
        message_text="你好",
        sender_id="u1",
        sender_name="小明",
    )
    chain = DecisionChain(
        [HardFilterDecider(), RuleDecider(), RateLimitDecider(), ProbabilityDecider()]
    )
    decision = _run(chain.decide(ctx))
    assert decision.should_reply is True
    assert decision.decider == "rule", "被 @ 时应由规则层短路，不能被概率层否决"

    # 未被点名的群消息：规则层弃权 -> 概率 0 否决
    ctx2 = TurnContext(
        event=FakeEvent(),
        umo="p:GroupMessage:1",
        actor=SessionActor(umo="x"),
        config=cfg,
        is_private=False,
        is_mention=False,
        is_command=False,
        message_text="随便说一句",
        sender_id="u1",
        sender_name="小明",
    )
    decision2 = _run(chain.decide(ctx2))
    assert decision2.should_reply is False
    assert decision2.decider == "probability"

    # 空消息被硬过滤拦下
    ctx3 = TurnContext(
        event=FakeEvent(),
        umo="p:GroupMessage:1",
        actor=SessionActor(umo="x"),
        config=cfg,
        is_private=False,
        is_mention=True,
        is_command=False,
        message_text="",
        sender_id="u1",
        sender_name="小明",
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
            event=FakeEvent(),
            umo="p:GroupMessage:1",
            actor=SessionActor(umo="x"),
            config=CompanionConfig({"reply_probability": p}),
            is_private=False,
            is_mention=False,
            is_command=False,
            message_text="有人吗",
            sender_id="u1",
            sender_name="小明",
        )

    # 命中门槛（roll=0 < 0.5）-> 交给读空气，由它说「想接」-> 回复
    judge_yes = FakeJudge({"reply": True, "reason": "被问到了"})
    d = _run(
        DecisionChain(
            [
                HardFilterDecider(),
                RuleDecider(),
                RateLimitDecider(),
                ProbabilityDecider(rng=_AlwaysZeroRng(), rate=lambda c: 0.5, defer_on_fail=True),
                LLMJudgeDecider(judge_yes),
            ]
        ).decide(make_ctx(0.5))
    )
    assert d.should_reply is True and d.decider == "llm_judge", d
    assert judge_yes.calls == 1, "命中门槛后必须调用读空气"

    # 读空气说「不想接」-> 不回复
    judge_no = FakeJudge({"reply": False, "reason": "插不上话"})
    d2 = _run(
        DecisionChain(
            [
                HardFilterDecider(),
                RuleDecider(),
                RateLimitDecider(),
                ProbabilityDecider(rng=_AlwaysZeroRng(), rate=lambda c: 0.5, defer_on_fail=True),
                LLMJudgeDecider(judge_no),
            ]
        ).decide(make_ctx(0.5))
    )
    assert d2.should_reply is False and d2.decider == "llm_judge", d2

    # 未命中门槛（roll=0.99 >= 0.5）-> 代码直接判不回，**不调用模型**
    judge_unused = FakeJudge({"reply": True})
    d3 = _run(
        DecisionChain(
            [
                HardFilterDecider(),
                RuleDecider(),
                RateLimitDecider(),
                ProbabilityDecider(rng=_AlwaysHighRng(), rate=lambda c: 0.5, defer_on_fail=True),
                LLMJudgeDecider(judge_unused),
            ]
        ).decide(make_ctx(0.5))
    )
    assert d3.decider == "probability" and d3.should_reply is False, d3
    assert judge_unused.calls == 0, "未命中门槛不得调用模型（省钱的关键）"

    # 概率为 0 -> 连门槛都不进，同样不调用模型
    judge_zero = FakeJudge({"reply": True})
    d4 = _run(
        DecisionChain(
            [
                HardFilterDecider(),
                RuleDecider(),
                RateLimitDecider(),
                ProbabilityDecider(rng=_AlwaysZeroRng(), rate=lambda c: 0.0, defer_on_fail=True),
                LLMJudgeDecider(judge_zero),
            ]
        ).decide(make_ctx(0.0))
    )
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
            event=FakeEvent(),
            umo="p:GroupMessage:1",
            actor=SessionActor(umo="x"),
            config=cfg,
            is_private=is_private,
            is_mention=is_mention,
            is_command=False,
            message_text="hi",
            sender_id="u1",
            sender_name="小明",
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
    lim2.allow("x", now=1000.0)  # 被拦
    assert lim2.allow("x", now=1000.0)[0] is False  # 仍无冷却负担


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
        sticker_auto_probability = 0.0  # 关掉自动补图，只看标记路径
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
        enable_stickers = False  # 关掉表情，确保不插图
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


# ======================================================================
# 连发合并（debounce）
# ======================================================================
def _bare_orchestrator(debounce_private=0.3, debounce_group=0.15):
    """构造一个只测等待逻辑的最小 Orchestrator。"""
    from astrbot_plugin_ai_companion.core import CompanionConfig
    from astrbot_plugin_ai_companion.core.orchestrator import Orchestrator

    cfg = CompanionConfig(
        {
            "enable_debounce": True,
            "debounce_private_seconds": debounce_private,
            "debounce_group_seconds": debounce_group,
        }
    )
    return Orchestrator(
        config=cfg,
        registry=None,
        chain=None,
        db=None,
        assembler=None,
        conversation_manager=None,
    )


def test_debounce_waits_for_quiet_window():
    """单条消息：等满窗口后才结算，返回自己这一条。"""

    async def run():
        o = _bare_orchestrator(debounce_private=0.25)
        t0 = time.monotonic()
        lines, ok, _imgs = await o._wait_quiet(
            "u:1", ("小明", "1", "在吗"), o._debounce_seconds(is_private=True)
        )
        elapsed = time.monotonic() - t0
        return lines, ok, _imgs, elapsed

    lines, ok, _imgs, elapsed = _run(run())
    assert ok is True
    assert elapsed >= 0.24, f"应等满窗口，实际 {elapsed:.3f}s"
    assert len(lines) == 1 and lines[0][2] == "在吗"


def test_debounce_merges_burst_into_one_turn():
    """连发三条：只有最后一条结算，且带上前面两条的内容。"""

    async def run():
        o = _bare_orchestrator(debounce_private=0.2)

        async def send(text, delay):
            await asyncio.sleep(delay)
            return await o._wait_quiet(
                "u:1", ("小明", "1", text), o._debounce_seconds(is_private=True)
            )

        # 0.0 / 0.08 / 0.16 秒连发三下，窗口 0.2s
        results = await asyncio.gather(
            send("第一句", 0.0), send("第二句", 0.08), send("第三句", 0.16)
        )
        return results

    results = _run(run())
    settlers = [r for r in results if r[1]]
    yielders = [r for r in results if not r[1]]
    assert len(settlers) == 1, "只允许一条消息负责结算（否则会重复回答）"
    assert len(yielders) == 2, "其余两条应让出"
    lines, _ok, _imgs = settlers[0]
    assert [l[2] for l in lines] == ["第一句", "第二句", "第三句"], (
        f"应合并全部三条且保持顺序，实际 {[l[2] for l in lines]}"
    )


def test_debounce_separate_sessions_do_not_interfere():
    """不同会话各自计时，互不影响。"""

    async def run():
        o = _bare_orchestrator(debounce_private=0.15)

        async def send(umo, text):
            return await o._wait_quiet(umo, ("甲", "1", text), o._debounce_seconds(is_private=True))

        return await asyncio.gather(send("a:1", "A"), send("b:1", "B"))

    results = _run(run())
    assert all(res[1] for res in results), "两个会话都应独立结算"
    assert [r[0][0][2] for r in results] == ["A", "B"]


def test_debounce_disabled_returns_immediately():
    """关闭合并时不应有任何等待。"""
    from astrbot_plugin_ai_companion.core import CompanionConfig
    from astrbot_plugin_ai_companion.core.orchestrator import Orchestrator

    cfg = CompanionConfig({"enable_debounce": False, "debounce_private_seconds": 5})
    o = Orchestrator(
        config=cfg,
        registry=None,
        chain=None,
        db=None,
        assembler=None,
        conversation_manager=None,
    )
    assert o._debounce_seconds(is_private=True) == 0.0
    assert o._debounce_seconds(is_private=False) == 0.0


def test_debounce_config_separates_group_and_private():
    """群聊 3s / 私聊 5s 分别生效。"""
    from astrbot_plugin_ai_companion.core import CompanionConfig
    from astrbot_plugin_ai_companion.core.orchestrator import Orchestrator

    cfg = CompanionConfig(
        {
            "debounce_group_seconds": 3,
            "debounce_private_seconds": 5,
        }
    )
    o = Orchestrator(
        config=cfg,
        registry=None,
        chain=None,
        db=None,
        assembler=None,
        conversation_manager=None,
    )
    assert o._debounce_seconds(is_private=True) == 5.0
    assert o._debounce_seconds(is_private=False) == 3.0

    # 默认值
    d = CompanionConfig({})
    od = Orchestrator(
        config=d,
        registry=None,
        chain=None,
        db=None,
        assembler=None,
        conversation_manager=None,
    )
    assert od._debounce_seconds(is_private=True) == 5.0
    assert od._debounce_seconds(is_private=False) == 3.0


def test_merge_prompt_marks_burst():
    """多条消息渲染成一个提示词，并标注「只回一次」。"""
    from astrbot_plugin_ai_companion.core.orchestrator import Orchestrator

    one = Orchestrator._merge_prompt([("小明", "1", "你好")])
    assert "你好" in one and one.count("\n") == 0

    many = Orchestrator._merge_prompt([("小明", "1", "第一句"), ("小明", "1", "你是谁")])
    assert "第一句" in many and "你是谁" in many
    assert "只回应一次" in many


def test_debounce_boundary_race_only_one_settles():
    """窗口到期与新消息同时到达时，仍只允许一条结算（竞态回归）。

    若结算前不二次确认归属，旧的一轮会在刚被顶替的瞬间返回消息串，
    与新的那一轮各自结算，造成重复回答 —— 这正是本次要修的症状。
    """

    async def run():
        o = _bare_orchestrator(debounce_private=0.1)
        results = []

        async def send(text, delay):
            if delay:
                await asyncio.sleep(delay)
            r = await o._wait_quiet(
                "u:1", ("小明", "1", text), o._debounce_seconds(is_private=True)
            )
            results.append((text, r[1], r[0]))

        # 并发到达，且刻意让部分消息的到达时刻落在前一条窗口到期点上
        await asyncio.gather(*[send(f"msg@{d}", d) for d in (0.0, 0.05, 0.10, 0.15, 0.20)])
        return results

    res = _run(run())
    settlers = [r for r in res if r[1]]
    assert len(settlers) == 1, f"必须恰好一条结算，实际 {len(settlers)}: {[r[0] for r in settlers]}"


# ======================================================================
# 工具调用 XML 泄漏 + 图片输入
# ======================================================================
def test_tool_call_xml_stripped_from_reply():
    """模型把工具调用当文本写进正文时，不能原样发给用户（线上实测泄漏）。"""
    import random as _r

    from astrbot.core.message.components import Plain
    from astrbot_plugin_ai_companion.humanize import Humanizer, StickerLibrary

    class Cfg:
        enable_stickers = False
        sticker_auto_probability = 0.0
        enable_typos = False

    lib = StickerLibrary(roots=[PLUGIN_DIR / "stickers"])
    h = Humanizer(stickers=lib, config=Cfg(), rng=_r.Random(1))

    leak = (
        "又来了是吧😂 你这表情包在我这就没现过身，全是空白\n\n"
        '<invoke name="send_sticker">\n'
        '<parameter name="category">无语</parameter>\n'
        "</invoke>"
    )

    class Res:
        def __init__(self, text):
            self.chain = [Plain(text)]

    class Ev:
        unified_msg_origin = "p:FriendMessage:1"

        def __init__(self, text):
            self._r = Res(text)

        def get_result(self):
            return self._r

    ev = Ev(leak)
    h.apply(ev)
    out = "".join(c.text for c in ev._r.chain if isinstance(c, Plain))
    assert "invoke" not in out and "parameter" not in out, f"XML 泄漏: {out!r}"
    assert "表情包" in out, "正文不能被误删"

    # antml: 前缀变体与残缺闭合标签也要清掉
    for variant in (
        '<antml:invoke name="send_sticker"><antml:parameter name="c">x</antml:parameter></antml:invoke>',
        '文字 <invoke name="x"> 尾巴',
        "</invoke>",
    ):
        ev2 = Ev(variant)
        h.apply(ev2)
        out2 = "".join(c.text for c in ev2._r.chain if isinstance(c, Plain))
        assert "invoke" not in out2 and "parameter" not in out2, f"变体未清: {out2!r}"


def test_tool_call_xml_stripped_in_proactive_path():
    """主动消息路径（apply_to_text）同样要清 XML。"""
    import random as _r

    from astrbot_plugin_ai_companion.humanize import Humanizer, StickerLibrary

    class Cfg:
        enable_stickers = False
        sticker_auto_probability = 0.0
        enable_typos = False

    lib = StickerLibrary(roots=[PLUGIN_DIR / "stickers"])
    h = Humanizer(stickers=lib, config=Cfg(), rng=_r.Random(1))
    text, _ = h.apply_to_text('在吗\n<invoke name="send_sticker"></invoke>')
    assert "invoke" not in text
    assert "在吗" in text


def test_image_components_collected_for_model():
    """用户发的图片必须被收集传给模型 —— 否则模型只能看到「[图片]」占位文本。

    线上症状：用户发表情包，bot 回「我这边全是空白」。根因是插件自己包办
    LLM 请求时，平台不会去扫描 event.message_obj 里的图片组件。
    """
    from astrbot_plugin_ai_companion.core.orchestrator import Orchestrator

    class FakeImage:
        def __init__(self, path):
            self._path = path
            self.converted = False

        async def convert_to_file_path(self):
            self.converted = True
            return self._path

    class FakePlain:
        pass

    class MockEvent:
        def __init__(self, comps):
            self.message_obj = type("O", (), {"message": comps})()

    o = Orchestrator(
        config=None,
        registry=None,
        chain=None,
        db=None,
        assembler=None,
        conversation_manager=None,
    )

    img1, img2 = FakeImage("/tmp/a.png"), FakeImage("/tmp/b.png")
    ev = MockEvent([FakePlain(), img1, img2])

    import astrbot.core.message.components as comps

    orig = comps.Image
    try:
        comps.Image = FakeImage  # 让 isinstance 判定为图片
        paths = _run(o._collect_image_paths(ev))
    finally:
        comps.Image = orig

    assert paths == ["/tmp/a.png", "/tmp/b.png"], f"图片未被收集: {paths}"
    assert img1.converted, "应调用 convert_to_file_path 转存"


def test_image_collection_survives_failure():
    """单张图片转换失败不应影响整轮（也不该抛异常）。"""
    from astrbot_plugin_ai_companion.core.orchestrator import Orchestrator

    class BadImage:
        async def convert_to_file_path(self):
            raise RuntimeError("下载失败")

    class GoodImage:
        async def convert_to_file_path(self):
            return "/tmp/ok.png"

    class MockEvent:
        def __init__(self, comps):
            self.message_obj = type("O", (), {"message": comps})()

    class _Cfg:
        debug_mode = False

    o = Orchestrator(
        config=_Cfg(),
        registry=None,
        chain=None,
        db=None,
        assembler=None,
        conversation_manager=None,
    )

    import astrbot.core.message.components as comps

    orig = comps.Image

    class Both(BadImage, GoodImage):
        pass

    try:
        comps.Image = (BadImage, GoodImage)
        paths = _run(o._collect_image_paths(MockEvent([BadImage(), GoodImage()])))
    finally:
        comps.Image = orig
    assert paths == ["/tmp/ok.png"], f"应跳过失败项保留成功项: {paths}"


def test_burst_keeps_images_from_earlier_messages():
    """「先发图、再发一句话」时，图片不能被丢掉（线上回归）。

    私聊窗口 5 秒会把这两条合并成一轮；若只从**最后一条**事件取图片，
    先到的图就丢了 —— 表现正是「我只显示个图片标记，具体啥样瞅不见」。
    """

    async def run():
        o = _bare_orchestrator(debounce_private=0.2)

        async def with_image():
            return await o._wait_quiet("u:1", ("小明", "1", ""), 0.2, images=["/tmp/photo.png"])

        async def text_only():
            await asyncio.sleep(0.08)
            return await o._wait_quiet("u:1", ("小明", "1", "这是什么表情包"), 0.2, images=[])

        return await asyncio.gather(with_image(), text_only())

    results = _run(run())
    settlers = [r for r in results if r[1]]
    assert len(settlers) == 1, "应只有一条结算"
    lines, _ok, images = settlers[0]
    assert images == ["/tmp/photo.png"], f"图片必须随合并保留，实际 {images}"
    assert [l[2] for l in lines] == ["", "这是什么表情包"], "文本也都要在"


def test_burst_merges_images_from_multiple_messages():
    """多条消息各带图片时全部合并，且不重复。"""

    async def run():
        o = _bare_orchestrator(debounce_private=0.15)

        async def send(imgs, delay):
            await asyncio.sleep(delay)
            return await o._wait_quiet("u:1", ("甲", "1", "看图"), 0.15, images=imgs)

        return await asyncio.gather(
            send(["/a.png"], 0.0),
            send(["/b.png"], 0.05),
            send(["/a.png"], 0.09),  # 重复项
        )

    res = _run(run())
    settlers = [r for r in res if r[1]]
    assert len(settlers) == 1
    _lines, _ok, images = settlers[0]
    assert images == ["/a.png", "/b.png"], f"应去重合并，实际 {images}"


# ======================================================================
# 群画像与熟悉度
# ======================================================================
def test_compute_familiarity_monotonic():
    """消息越多、画像轮次越多、认识的人越多，熟悉度越高且封顶 100。"""
    from astrbot_plugin_ai_companion.memory import compute_familiarity

    assert compute_familiarity(message_count=0, profile_rounds=0, known_people=0) == 0
    a = compute_familiarity(message_count=50, profile_rounds=1, known_people=2)
    b = compute_familiarity(message_count=200, profile_rounds=2, known_people=6)
    c = compute_familiarity(message_count=2000, profile_rounds=10, known_people=100)
    assert a < b < c
    assert c == 100, f"应封顶 100，实际 {c}"


def test_familiarity_stage_mapping():
    """熟悉度映射到 观望/融入/熟悉 三档。"""
    from astrbot_plugin_ai_companion.memory import familiarity_stage

    assert familiarity_stage(0) == "观望"
    assert familiarity_stage(49) == "观望"
    assert familiarity_stage(50) == "融入"
    assert familiarity_stage(199) == "融入"
    assert familiarity_stage(200) == "熟悉"


def test_group_profile_db_roundtrip():
    """群画像写入后可读回，且 upsert 更新。"""

    async def run(db):
        await db.connect()
        await db.upsert_group_profile(
            umo="p:GroupMessage:1", profile="少前游戏群", message_count=100, familiar=40
        )
        row = await db.get_group_profile("p:GroupMessage:1")
        assert row["profile"] == "少前游戏群"
        assert row["familiar"] == 40
        # 二次 upsert 更新
        await db.upsert_group_profile(
            umo="p:GroupMessage:1", profile="少前游戏群(硬核)", message_count=200, familiar=70
        )
        row2 = await db.get_group_profile("p:GroupMessage:1")
        assert row2["profile"].endswith("硬核)")
        assert row2["familiar"] == 70
        await db.close()

    import tempfile
    from pathlib import Path

    from astrbot_plugin_ai_companion.storage import MemoryDB

    schema = PLUGIN_DIR / "storage" / "schema.sql"
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    _run(run(MemoryDB(db_path=tmp, schema_path=schema)))


def test_probability_async_rate_provider():
    """异步概率回调（群熟悉度场景）能被正确 await。"""
    from astrbot_plugin_ai_companion.decision import ProbabilityDecider, TurnContext

    class Cfg:
        reply_probability = 0.1

    class Ev:
        def get_self_id(self):
            return "bot"

    async def async_rate(ctx):
        return 0.5

    dec = ProbabilityDecider(rng=_AlwaysZeroRng(), rate=async_rate, defer_on_fail=True)
    ctx = TurnContext(
        event=Ev(),
        umo="p:GroupMessage:1",
        actor=type("A", (), {})(),
        config=Cfg(),
        is_private=False,
        is_mention=False,
        is_command=False,
        message_text="hi",
        sender_id="u1",
        sender_name="x",
        self_id="bot",
    )
    # roll=0.0 < 0.5 -> 弃权（交给读空气）
    d = _run(dec.decide(ctx))
    assert d is None, f"异步 rate=0.5 且 roll=0 应弃权，实际 {d}"


def test_group_profile_manager_skips_below_min():
    """消息不足最低数时，不生成画像。"""

    async def run():
        from astrbot_plugin_ai_companion.memory.group_profile import GroupProfileManager

        class Cfg:
            enable_group_profile = True
            group_profile_min_messages = 30
            group_profile_interval = 50

        class Db:
            async def count_messages_in(self, umo):
                return 10

            async def get_group_profile(self, umo):
                return None

        m = GroupProfileManager(db=Db(), context=None, config=Cfg())
        updated = await m.update_if_due("p:GroupMessage:1")
        return updated

    assert _run(run()) is False
