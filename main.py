"""AI Companion —— 像真人一样聊天的插件入口。

P0 能力：

* 群聊与私聊统一处理，AI 自主决定「这条要不要回」；
* 决策链可插拔（硬过滤 / 规则 / 限流 / 概率），为专门的决策模型预留插槽；
* 每会话运行态注册表，支持同时关注多个窗口而不 per-window 常驻 Agent；
* 全部消息落库（SQLite + FTS5 trigram 中文检索）；
* 精简提示词：动态内容走临时内容块，历史与人格复用平台链路。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig

from .context import ContextAssembler
from .core import CompanionConfig, Orchestrator, ProactiveScheduler, SessionRegistry
from .decision import (
    DecisionChain,
    HardFilterDecider,
    LLMJudgeDecider,
    ProbabilityDecider,
    RateLimitDecider,
    RuleDecider,
)
from .memory import Compactor, KnowledgeExtractor
from .storage import MemoryDB
from .tools import LookupPersonTool, SearchHistoryTool

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
                conversation_manager=getattr(
                    self.context, "conversation_manager", None
                ),
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
        if self.config.enable_llm_judge:
            # 概率层改为「未通过时弃权」，把最终拍板权交给读空气
            deciders.append(ProbabilityDecider(defer_on_fail=True))
            deciders.append(
                LLMJudgeDecider(
                    self.orchestrator._judge,
                    timeout=self.config.judge_timeout_seconds,
                )
            )
            logger.info("[ai_companion] 已启用 AI 读空气决策")
        else:
            deciders.append(ProbabilityDecider())
        self.orchestrator.chain = DecisionChain(deciders)

        # 注册 LLM 工具（自建工具类，不依赖装饰器解析）
        try:
            self.context.add_llm_tools(
                SearchHistoryTool(db=self.db),
                LookupPersonTool(extractor=self.extractor),
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
                contexts=None,
                conversation=result.conversation,
            )
            extra = list(result.extra_parts) if result.extra_parts else []
            if result.people_hint:
                extra.extend(
                    self.assembler.build_extra_parts(self.config, result.people_hint)
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
        except Exception as e:
            logger.error(f"[ai_companion] 注入请求失败: {e}", exc_info=True)

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
        rows = await self.db.search_messages(
            keyword, umo=event.unified_msg_origin, limit=10
        )
        if not rows:
            yield event.plain_result(f"没有找到包含「{keyword}」的记录")
            return
        lines = [f"· {r['sender_name'] or r['role']}: {r['content'][:60]}" for r in rows]
        yield event.plain_result(
            f"找到 {len(rows)} 条与「{keyword}」相关的记录：\n" + "\n".join(lines)
        )
