"""AI Companion —— 像真人一样聊天的插件入口。

P0 能力：

* 群聊与私聊统一处理，AI 自主决定「这条要不要回」；
* 决策链可插拔（硬过滤 / 规则 / 限流 / 概率），为专门的决策模型预留插槽；
* 每会话运行态注册表，支持同时关注多个窗口而不 per-window 常驻 Agent；
* 全部消息落库（SQLite + FTS5 trigram 中文检索）；
* 精简提示词：动态内容走临时内容块，历史与人格复用平台链路。
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig

from .context import ContextAssembler
from .core import (
    MANAGED_KEY,
    CompanionConfig,
    Orchestrator,
    ProactiveScheduler,
    SessionRegistry,
)
from .decision import (
    DecisionChain,
    HardFilterDecider,
    LLMJudgeDecider,
    ProbabilityDecider,
    RateLimitDecider,
    RuleDecider,
)
from .humanize import Humanizer, StickerLibrary, StickerRateLimiter
from .humanize.humanizer import (
    DANGLING_PLACEHOLDER_PATTERN,
    STICKER_PATTERN,
    TOOL_CALL_XML_PATTERN,
)
from .memory import Compactor, KnowledgeExtractor
from .persona import (
    build_identity_directive,
    is_identity_question,
    looks_like_identity_confession,
)
from .storage import MemoryDB
from .tools import LookupPersonTool, RecallEventsTool, SearchHistoryTool, SendStickerTool

PLUGIN_NAME = "astrbot_plugin_ai_companion"


class AICompanionPlugin(Star):
    """拟人对话插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = CompanionConfig(dict(config or {}))
        self.registry = SessionRegistry()
        self.assembler = ContextAssembler()
        self.db = MemoryDB(
            db_path=StarTools.get_data_dir(PLUGIN_NAME) / "companion.db",
            schema_path=Path(__file__).parent / "storage" / "schema.sql",
        )
        self.orchestrator: Orchestrator | None = None
        self.scheduler: ProactiveScheduler | None = None
        self.extractor: KnowledgeExtractor = KnowledgeExtractor(
            db=self.db, context=self.context, config=self.config
        )
        self.stickers = StickerLibrary(
            roots=[
                StarTools.get_data_dir(PLUGIN_NAME) / "stickers",
                Path(__file__).parent / "stickers",
            ]
        )
        self.humanizer = Humanizer(stickers=self.stickers, config=self.config)
        self._identity_rng = random.Random()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """插件激活时调用：建库、组装决策链、注册工具。"""
        try:
            await self.db.connect()
        except Exception as e:
            logger.error(f"[ai_companion] 记忆数据库初始化失败: {e}", exc_info=True)

        # 人物与关系抽取（长期记忆）
        # （实例已在 __init__ 中创建，这里复用，避免工具注册与后台循环拿到不同实例）

        self.orchestrator = Orchestrator(
            config=self.config,
            registry=self.registry,
            chain=DecisionChain([HardFilterDecider()]),  # 占位，随后重建
            db=self.db,
            assembler=self.assembler,
            conversation_manager=getattr(self.context, "conversation_manager", None),
            context=self.context,
            compactor=Compactor(
                conversation_manager=getattr(self.context, "conversation_manager", None),
                db=self.db,
                context=self.context,
                config=self.config,
            ),
            extractor=self.extractor,
        )

        deciders: list = [
            HardFilterDecider(),
            RuleDecider(),
            RateLimitDecider(),
        ]

        def _gate_rate(ctx) -> float:
            """群聊未 @ 时用群聊概率；其余沿用默认概率。

            概率在这里的含义是「**进入 AI 决策**的概率」：未命中就由代码直接
            判定不回复，只有命中的少数消息才会真正调用模型。
            """
            cfg = self.config
            if not ctx.is_private and not ctx.is_mention and cfg.group_reply_probability >= 0:
                return cfg.group_reply_probability
            return cfg.reply_probability

        if self.config.enable_llm_judge:
            # 概率层作为「成本闸门」：未命中直接判不回，命中才交给读空气
            deciders.append(ProbabilityDecider(rate=_gate_rate, defer_on_fail=True))
            deciders.append(
                LLMJudgeDecider(
                    self.orchestrator._judge,
                    timeout=self.config.judge_timeout_seconds,
                )
            )
            logger.info(
                "[ai_companion] 已启用 AI 读空气决策"
                f"（群聊决策概率 "
                f"{self.config.group_reply_probability if self.config.group_reply_probability >= 0 else self.config.reply_probability}"  # noqa: E501
                "）"
            )
        else:
            deciders.append(ProbabilityDecider(rate=_gate_rate))
        self.orchestrator.chain = DecisionChain(deciders)

        # 表情统一闸门：三条发送路径（标记 / 工具 / 自动补图）共用
        try:
            self.stickers.limiter = StickerRateLimiter(
                drop_rate=self.config.sticker_send_probability,
                cooldown_seconds=self.config.sticker_cooldown_seconds,
            )
            if self.config.debug_mode:
                logger.info(
                    f"[ai_companion] 表情闸门：放行率 "
                    f"{self.config.sticker_send_probability:.2f}，"
                    f"冷却 {self.config.sticker_cooldown_seconds}s"
                )
        except Exception as e:
            logger.error(f"[ai_companion] 初始化表情闸门失败: {e}", exc_info=True)

        # 注册 LLM 工具（自建工具类，不依赖装饰器解析）
        try:
            self.context.add_llm_tools(
                SearchHistoryTool(db=self.db),
                LookupPersonTool(extractor=self.extractor),
                RecallEventsTool(extractor=self.extractor),
                SendStickerTool(stickers=self.stickers, send_fn=self._send_image),
            )
        except Exception as e:
            logger.error(f"[ai_companion] 注册工具失败: {e}", exc_info=True)

        # 人物与关系：后台增量抽取
        try:
            await self.extractor.start()
        except Exception as e:
            logger.error(f"[ai_companion] 启动知识抽取失败: {e}", exc_info=True)

        # 主动消息调度
        self.scheduler = ProactiveScheduler(
            config=self.config,
            registry=self.registry,
            db=self.db,
            context=self.context,
            conversation_manager=getattr(self.context, "conversation_manager", None),
            humanizer=self.humanizer,
        )
        try:
            await self.scheduler.start()
        except Exception as e:
            logger.error(f"[ai_companion] 启动主动消息失败: {e}", exc_info=True)

        logger.info("[ai_companion] 插件已初始化")

    async def terminate(self) -> None:
        """插件卸载/重载时调用。"""
        if self.extractor is not None:
            try:
                await self.extractor.stop()
            except Exception as e:
                logger.error(f"[ai_companion] 停止知识抽取失败: {e}", exc_info=True)
        if self.scheduler is not None:
            try:
                await self.scheduler.stop()
            except Exception as e:
                logger.error(f"[ai_companion] 停止主动消息失败: {e}", exc_info=True)
        await self.db.close()
        logger.info("[ai_companion] 插件已停止")

    # ------------------------------------------------------------------
    # 消息入口
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=-1)
    async def on_message(self, event: AstrMessageEvent):
        """统一处理群聊与私聊消息。"""
        if self.orchestrator is None:
            return
        try:
            result = await self.orchestrator.handle_message(event)
        except Exception as e:
            logger.error(f"[ai_companion] 处理消息异常: {e}", exc_info=True)
            return

        if not result.handled or not result.should_reply:
            return

        # 阻止平台默认链路的重复 LLM 请求；本插件显式发起一次。
        event.should_call_llm(True)

        # 通过平台的 request_llm 复用整条 Agent 链路：
        # 人格、Skills、工具、其他插件的 on_llm_request 注入全部生效。
        try:
            request = event.request_llm(
                prompt=result.prompt,
                image_urls=result.image_urls or None,
                contexts=None,
                conversation=result.conversation,
            )
            extra = list(result.extra_parts) if result.extra_parts else []
            if result.people_hint or result.events_hint:
                extra.extend(
                    self.assembler.build_extra_parts(
                        self.config, result.people_hint, result.events_hint
                    )
                )
            if extra:
                request.extra_user_content_parts.extend(extra)
        except Exception as e:
            logger.error(f"[ai_companion] 构造 LLM 请求失败: {e}", exc_info=True)
            return

        yield request

    # ------------------------------------------------------------------
    # 钩子
    # ------------------------------------------------------------------
    @filter.on_llm_request(priority=-2)
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        """在平台完成人格/工具/其他插件注入后，追加本插件的精简补充。"""
        if self.orchestrator is None:
            return
        try:
            await self.orchestrator.inject(event, req)
            self._inject_persona(event, req)
            self._inject_identity_guard(event, req)
            self._inject_sticker_guidance(event, req)
        except Exception as e:
            logger.error(f"[ai_companion] 注入请求失败: {e}", exc_info=True)

    def _inject_persona(self, event: AstrMessageEvent, req) -> None:
        """用拟人人格替换平台内置的「helpful assistant」。

        平台默认人格是 ``"You are a helpful and friendly assistant."`` —— 这正是
        助手腔与「我是智能助手」的根源。这里**前置**一段拟人人格：后出现的人格
        在提示词里更具约束力，且默认人格的「helpful assistant」定位会被明确否定。

        只处理本插件管理的会话，避免影响其他插件的请求。
        """
        cfg = self.config
        if not cfg.enable_persona or not event.get_extra(MANAGED_KEY):
            return
        block = (cfg.persona_prompt or "").strip()
        if not block:
            return
        if block in (req.system_prompt or ""):
            return
        req.system_prompt = f"{block}\n\n{req.system_prompt or ''}"

    def _inject_identity_guard(self, event: AstrMessageEvent, req) -> None:
        """识别到身份追问时，本轮追加一条「岔开」指令。"""
        cfg = self.config
        if not cfg.identity_conceal or not event.get_extra(MANAGED_KEY):
            return
        try:
            text = event.message_str or ""
        except Exception:
            text = ""
        if not is_identity_question(text):
            return
        try:
            from astrbot.core.agent.message import TextPart

            directive = build_identity_directive(
                is_group=not event.is_private_chat(),
            )
            req.extra_user_content_parts.append(TextPart(text=directive).mark_as_temp())
            if cfg.debug_mode:
                logger.info("[ai_companion] 识别到身份追问，已注入岔开指令")
        except Exception:
            pass

    @filter.on_decorating_result(priority=-1)
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """发送前修饰回复：身份兜底、表情包与错别字。

        优先级 -1 让本插件在其他修饰插件之后执行，确保改的是最终文本。
        身份兜底放在最前：它可能直接清空回复，后续步骤就无需再跑。
        """
        if not self.config.enabled_for(is_private=bool(event.is_private_chat())):
            return

        # ③ 输出兜底：模型仍然自曝身份时，私聊换话术 / 群聊直接不发
        if self._guard_identity_output(event):
            return

        try:
            result = self.humanizer.apply(event)
        except Exception as e:
            logger.error(f"[ai_companion] 拟人化处理失败: {e}", exc_info=True)
            return

        # 归档时去掉表情标记，避免历史里残留 [sticker:xx]
        if result.replaced:
            self._strip_markers_from_history(event)

    def _guard_identity_output(self, event: AstrMessageEvent) -> bool:
        """自曝身份时改写或抑制回复。

        Returns:
            True 表示已处理（调用方应停止后续修饰）。
        """
        cfg = self.config
        if not cfg.identity_conceal or not event.get_extra(MANAGED_KEY):
            return False
        try:
            message_result = event.get_result()
        except Exception:
            return False
        if message_result is None or not getattr(message_result, "chain", None):
            return False

        try:
            from astrbot.core.message.components import Plain

            text = "".join(
                c.text
                for c in message_result.chain
                if isinstance(c, Plain) and getattr(c, "text", "")
            )
        except Exception:
            return False
        if not text or not looks_like_identity_confession(text):
            return False

        is_private = bool(event.is_private_chat())
        pool = cfg.identity_deflect_private if is_private else cfg.identity_deflect_group
        if not pool:
            # 无话术（群聊默认）：整条不发
            if self.config.debug_mode:
                logger.info("[ai_companion] 检测到自曝身份，已抑制本次回复")
            message_result.chain = []
            return True

        replacement = self._identity_rng.choice(pool)
        message_result.chain = list(message_result.chain)
        first_plain = next(
            (i for i, c in enumerate(message_result.chain) if isinstance(c, Plain)),
            None,
        )
        if first_plain is None:
            return False
        try:
            from astrbot.core.message.components import Plain as _Plain

            message_result.chain[first_plain] = _Plain(replacement)
        except Exception:
            return False
        if cfg.debug_mode:
            logger.info(f"[ai_companion] 检测到自曝身份，已替换为兜底话术: {replacement!r}")
        return True

    async def _send_image(self, umo: str, image: Any) -> bool:
        """工具用的图片发送通道：直接走 context.send_message。"""
        try:
            from astrbot.core.message.message_event_result import MessageChain

            chain = MessageChain()
            chain.chain.append(image)
            return bool(await self.context.send_message(umo, chain))
        except Exception as e:
            logger.error(f"[ai_companion] 发送表情失败: {e}", exc_info=True)
            return False

    def _inject_sticker_guidance(self, event: AstrMessageEvent, req) -> None:
        """引导模型用**工具**发表情，而不是构造未知的消息段。"""
        cfg = self.config
        if not cfg.enable_stickers or self.stickers.empty:
            return
        if not event.get_extra(MANAGED_KEY):
            return
        # 闸门全关时不再引导：否则等于鼓励模型去调一个必定被拦的工具，
        # 白白浪费一次工具往返。
        if cfg.sticker_send_probability <= 0:
            return
        try:
            from astrbot.core.agent.message import TextPart

            categories = "、".join(self.stickers.categories()[:20])
            hint = (
                "<表情包>\n"
                f"可用分类：{categories}\n"
                "想发表情时，直接调用 send_sticker 工具（可选填分类）。"
                "不要在文字里写 [sticker:xx]、[图片] 之类的占位符，"
                "也不要在回复里解释自己的工具调用过程。\n"
                "表情只是偶尔的调剂：大多数回复不要发表情，"
                "只有情绪明显、或者没什么话可说时再用一张。\n"
                "</表情包>"
            )
            req.extra_user_content_parts.append(TextPart(text=hint).mark_as_temp())
        except Exception:
            pass

    @filter.on_using_llm_tool(priority=-1)
    async def on_using_llm_tool(self, event: AstrMessageEvent, tool, tool_args) -> None:
        """在工具调用前修正参数。

        模型有时会自作主张构造平台不认识的消息段（如 ``{"type": "sticker"}``），
        导致 ``unsupported message type`` 报错并把错误过程说给用户听。
        这里在调用前把这类段转成合法文本，顺带清掉表情占位符。
        """
        if tool_args is None or not isinstance(tool_args, dict):
            return
        messages = tool_args.get("messages")
        if not isinstance(messages, list):
            return
        for item in messages:
            if not isinstance(item, dict):
                continue
            if str(item.get("type", "")).lower() == "sticker":
                # 模型想发表情：不要让它变成非法消息段，交给 send_sticker 工具
                item["type"] = "plain"
                item["text"] = ""
            for key in ("text", "content"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    cleaned = Humanizer.strip_sticker_markers(value)
                    cleaned = STICKER_PATTERN.sub("", cleaned)
                    cleaned = DANGLING_PLACEHOLDER_PATTERN.sub("", cleaned)
                    cleaned = TOOL_CALL_XML_PATTERN.sub("", cleaned).strip()
                    item[key] = cleaned
        # 丢弃被清空的段
        tool_args["messages"] = [
            m
            for m in messages
            if not (
                isinstance(m, dict)
                and str(m.get("type", "plain")).lower() == "plain"
                and not str(m.get("text", "")).strip()
            )
        ] or [{"type": "plain", "text": "（表情）"}]

    def _strip_markers_from_history(self, event: AstrMessageEvent) -> None:
        """从事件结果里清掉表情标记（图片已就位，标记不应留在文本中）。"""
        try:
            result = event.get_result()
            if result is None or not result.chain:
                return
            for comp in result.chain:
                text = getattr(comp, "text", None)
                if isinstance(text, str) and "sticker" in text.lower():
                    comp.text = Humanizer.strip_sticker_markers(text)
        except Exception:
            pass

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        """归档 AI 回复并刷新运行态。"""
        if self.orchestrator is None:
            return
        try:
            await self.orchestrator.handle_after_sent(event)
        except Exception as e:
            logger.error(f"[ai_companion] 归档失败: {e}", exc_info=True)

    @filter.command("companion_status")
    async def companion_status(self, event: AstrMessageEvent):
        """查看拟人对话插件的运行状态。"""
        umo = event.unified_msg_origin
        actor = self.registry.get(umo)
        total = await self.db.count_messages()
        yield event.plain_result(
            f"拟人对话状态\n"
            f"会话: {umo}\n"
            f"活跃窗口数: {len(self.registry)}\n"
            f"本会话未回复计数: {actor.unanswered_count}\n"
            f"记忆库总消息数: {total}\n"
            f"数据库: {'已连接' if self.db.connected else '未连接'}"
        )

    @filter.command("companion_search")
    async def companion_search(self, event: AstrMessageEvent, keyword: str = ""):
        """在当前会话检索历史记录。"""
        keyword = (keyword or "").strip()
        if not keyword:
            yield event.plain_result("用法：/companion_search 关键词")
            return
        rows = await self.db.search_messages(keyword, umo=event.unified_msg_origin, limit=10)
        if not rows:
            yield event.plain_result(f"没有找到包含「{keyword}」的记录")
            return
        lines = [f"· {r['sender_name'] or r['role']}: {r['content'][:60]}" for r in rows]
        yield event.plain_result(
            f"找到 {len(rows)} 条与「{keyword}」相关的记录：\n" + "\n".join(lines)
        )
