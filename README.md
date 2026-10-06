# 拟人对话 (AI Companion)

> 让 AI 像真人一样聊天：**自己决定要不要接话**，而不只是被动应答。

一个以「人」为模型重构的 AstrBot 聊天插件。群聊与私聊统一抽象，AI 在每一轮
自主判断「这条消息我想不想回」，并为后续的主动发言、长期记忆、关系图谱与
专门决策模型预留了扩展位。

---

## 设计取向

与「自建整条上下文链路」类插件不同，本插件复用平台原生 Agent 链路：

| 关注点 | 方案 |
|--------|------|
| 人格 / Skills / 工具 / 其他插件注入 | **复用平台**（`event.request_llm`），零成本获得 |
| 「要不要回」 | **自建可插拔决策链**（本插件的核心） |
| 短期对话历史 | 复用平台 conversation |
| 长期记忆 / 检索 | 自建 SQLite（FTS5 trigram 中文检索） |
| 提示词 | 精简：只补人格与临时环境信息，不堆砌规则 |

这样既拿到自主决策能力，又不必重造平台已经做好的部分。

---

## 已实现（P0 – P6）

- **统一群聊 / 私聊**：一个入口处理所有会话，决策链内部区分场景。
- **回复决策链**（可插拔，按成本从低到高短路）：
  1. `HardFilterDecider` — 空消息 / 指令 / Bot 自身消息 / 未启用会话
  2. `RuleDecider` — 私聊、被 @、被引用、唤醒前缀 → 直接回复
  3. `RateLimitDecider` — 最小回复间隔、冷却
  4. `ProbabilityDecider` — 基础概率（启用读空气时：未通过则**弃权**）
  5. `LLMJudgeDecider` — **AI 读空气**，由一次轻量 LLM 调用最终拍板
- **AI 读空气（P1）**：群聊里没被点名的消息，先按概率粗筛，未通过时再让 AI
  判断「此刻想不想接话」。读空气调用**不进平台 Agent 链路**：无工具、无人格、
  上下文极小，又快又便宜，也不污染主对话历史。支持独立指定便宜模型与超时。
- **每会话运行态注册表**：同时关注 N 个窗口，内存开销 ~1KB/会话，
  **不 per-window 常驻 Agent**；同会话串行（锁），跨会话并行。
- **短期记忆压缩（P2）**：历史过长时，把最老的一段交给模型总结成摘要并替换原文，
  腾出上下文窗口。近期原文与 checkpoint 原样保留，摘要**滚动合并**（早期远记忆不丢），
  原始档案永不删改。压缩在平台会话锁内进行，失败时保持历史原样。
- **主动消息（P3）**：冷场够久时主动搭话。**一个全局调度循环**扫描所有会话，
  命中的窗口才创建临时任务，关注 1000 个窗口也只跑一个循环。带**稳定抖动**（各会话
  不同时刻开口，更像真人节奏）；「未回复即闭嘴」——连续主动无人回应就进入冷却。
  生成时显式带入人格与近期上下文，发送走 `context.send_message`。
- **人物画像与关系图谱（P4）**：像真人一样「对每个人都有印象，并知道这些人彼此
  是什么关系」。后台**增量**总结聊天记录，沉淀出性格特点、说话风格与人际关系
  （姐妹 / 恋人 / 同事…）。实体**全局唯一**（用平台用户 ID，同一个人在哪个群都是
  他/她），支持昵称/群名片归一；关系双向可查、重复出现自动强化。回复时可摘要提示
  「你对这个人的了解」，也有 `lookup_person` 工具供模型主动查询。
- **事件线（P5）**：像真人一样记得「我们之前约好过什么」。在与人物抽取同一次调用里
  顺带总结关键事件（约定 / 计划 / 经历 / 变化 / 冲突），入库并关联参与者；相似事件
  自动去重。回复时摘一句「最近发生的事」，也有 `recall_events` 工具按关键词回忆。
- **拟人增强（P6）**：
  - **表情包**：把图片按情绪分类放进 `stickers/`（子目录名即分类，如
    `stickers/开心/`）。AI 在回复里写 `[sticker:开心]`，发送前被替换成真实图片；
    也能按概率自动补一张。**标记绝不会泄露给用户，也不会留在聊天记录里。**
  - **错别字**：按概率制造轻微手误（只改常见同音字，不碰标点/数字/英文/标记）。
    默认关闭。
  - **分段与打字延迟不重复实现**——平台自带的「分段回复」在本钩子之后立即执行，
    并由 `RespondStage` 逐段按对数间隔发送，重复实现只会造成双重延迟。
- **全量消息落库**：即使 AI 决定不回复也会记录（真人也记得别人说过的话）。
- **中文历史检索**：SQLite + FTS5 `trigram`；2 字符短词自动回退 `LIKE`。
- **LLM 工具**：`search_chat_history`，模型需要时可自行翻聊天记录。
- **指令**：`/companion_status`、`/companion_search <关键词>`。

---

## 安装

```bash
# 放入 AstrBot 插件目录
cp -r astrbot_plugin_ai_companion /path/to/AstrBot/data/plugins/
# 重载插件（或重启 AstrBot）
```

无第三方运行时依赖（数据库用标准库 `sqlite3`）。

---

## 配置

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `enable` | true | 总开关 |
| `enable_private_chat` | true | 私聊 |
| `enable_group_chat` | true | 群聊 |
| `reply_probability` | 0.85 | 基础回复概率（0~1） |
| `enable_llm_judge` | true | 启用 AI 读空气（概率未通过时由 AI 拍板） |
| `judge_provider_id` | "" | 读空气专用模型（留空=默认模型） |
| `judge_timeout_seconds` | 15 | 读空气超时，超时保守不回复 |
| `ignore_command_messages` | true | 指令消息交给平台指令链路 |
| `min_reply_interval_seconds` | 3 | 同会话最小回复间隔 |
| `record_all_messages` | true | 记录全部消息 |
| `enable_compact` | true | 启用短期记忆压缩 |
| `compact_trigger_turns` | 60 | 历史超过该条数时触发压缩 |
| `compact_keep_recent` | 20 | 始终保留最近 N 条原文 |
| `compact_min_dropped` | 10 | 可压缩内容少于该条数则跳过 |
| `compact_provider_id` | "" | 压缩专用模型（留空=默认模型） |
| `enable_proactive` | **false** | 主动消息总开关（默认关闭） |
| `proactive_sessions` | [] | 主动消息白名单；**留空=不主动任何会话**，填 `*` 表示全部 |
| `proactive_threshold_minutes` | 60 | 沉默多久后主动 |
| `proactive_check_interval_seconds` | 60 | 全局扫描间隔 |
| `proactive_max_unanswered` | 2 | 连续主动上限，达到即冷却 |
| `proactive_cooldown_minutes` | 240 | 被无视后的冷却时长 |
| `proactive_history_turns` | 10 | 主动消息参考的历史条数 |
| `proactive_prompt` | "" | 主动消息提示词（支持三个占位符） |
| `enable_knowledge_extraction` | true | 启用人物与关系抽取 |
| `extraction_interval_minutes` | 30 | 增量抽取间隔 |
| `extraction_min_messages` | 8 | 新消息不足该数则跳过 |
| `extraction_batch_size` | 40 | 单批处理消息上限 |
| `extraction_provider_id` | "" | 抽取专用模型（留空=默认模型） |
| `inject_people_context` | true | 回复时提示相关人物 |
| `inject_events_context` | true | 回复时提示最近发生的事 |
| `enable_stickers` | true | 启用表情包 |
| `sticker_auto_probability` | 0.15 | 自动补表情概率（0=只按 AI 要求发） |
| `enable_typos` | false | 启用错别字 |
| `typo_probability` | 0.03 | 整条回复出一次手误的概率 |
| `system_prompt_extra` | "" | 额外系统提示词（建议精简） |
| `inject_time` | true | 注入当前时间（临时块，不入历史） |
| `debug_mode` | false | 输出每层决策结果 |

**注意**：本插件与 AstrBot 平台自带的「主动回复」功能定位不同（后者是消息触发，
本插件是自主判断），无需额外关闭；但本插件接管了回复决策，若同时使用其他
「主动回复/主动对话」类插件，请避免在同一会话重复配置。

---

## 架构

```
main.py                    入口：注册钩子、组装依赖
core/
  config.py                配置强类型视图
  registry.py              每会话运行态（锁 / 时间戳 / 未回复计数 / 抖动）
  orchestrator.py          记录 → 决策 → 生成/拦截 → 归档
  proactive.py             主动消息：全局调度循环 + 人格化生成 + 冷却
decision/
  base.py                  ReplyDecider 协议 + TurnContext/Decision
  chain.py                 策略链（短路即停）
  filters.py / rules.py / rate_limit.py / probability.py
  llm_judge.py             LLM 读空气
  plugins/                 专门决策模型插槽（Jev 等）
context/
  renderer.py              消息链 → 可读文本
  assembler.py             动态上下文块（临时、不污染历史）
humanize/
  humanizer.py             发送前修饰：表情标记替换、错别字
  stickers.py              本地表情包库（子目录即分类）
  typo.py                  错别字混淆表
memory/
  compactor.py             短期记忆压缩（滚动摘要）
  knowledge.py             人物画像 / 关系图谱 / 事件线抽取（增量 + 全局实体）
storage/
  db.py                    SQLite 门面（含结构升级）
  schema.sql               表结构（messages/session_state/entities/relations/…）
tools/
  history_search.py        search_chat_history 工具
  lookup_person.py         lookup_person 工具
  recall_events.py         recall_events 工具
stickers/                  表情包目录（子目录名即情绪分类）
  README.md                放置说明
```

### 数据流

```mermaid
sequenceDiagram
    participant P as 平台
    participant O as Orchestrator
    participant D as 决策链
    participant DB as 记忆库
    P->>O: 消息事件
    O->>DB: 记录消息
    O->>D: 决策
    alt think 回复
        O-->>P: yield request_llm(conversation)
        Note over P: 平台注入人格/工具/其他插件
        P-->>P: 发送回复
        P->>O: after_message_sent
        O->>DB: 归档 AI 回复
    else 不回复
        O->>P: should_call_llm(True)
    end
```

---

## 存储

数据目录：`data/plugin_data/astrbot_plugin_ai_companion/companion.db`

| 表 | 用途 | 状态 |
|----|------|------|
| `messages` (+ `messages_fts`) | 原始消息 + 中文全文检索 | ✅ 已用 |
| `session_state` | 会话运行态（主动消息跨重启恢复） | ✅ 已用 |
| `entities` | 人（全局唯一实体） | ✅ 已用 |
| `entity_aliases` | 称呼归一（昵称/群名片 → 实体） | ✅ 已用 |
| `profiles` | 人物画像（特点 / 风格 / 备注 / 好感度） | ✅ 已用 |
| `relations` | 关系图谱（姐妹 / 恋人 / 同事…，双向可查） | ✅ 已用 |
| `extraction_state` | 抽取游标（增量，不重复烧 token） | ✅ 已用 |
| `events` / `event_participants` | 事件线（约定 / 计划 / 变化…） | ✅ 已用 |

---

## 路线图

| 期 | 内容 | 状态 |
|----|------|------|
| P0 | 决策链 / 注册表 / 落库 / 检索工具 | ✅ 已完成 |
| P1 | LLM 读空气接入策略链 | ✅ 已完成 |
| P2 | 短期记忆 compact（窗口不足时压缩沉淀） | ✅ 已完成 |
| P3 | 主动消息（全局扫描 + 沉默触发 + 免打扰） | ✅ 已完成 |
| P4 | 人物画像与关系图谱（`lookup_person` 工具） | ✅ 已完成 |
| P5 | 事件线抽取与关系派生 | ✅ 已完成 |
| P6 | 拟人增强（错别字 / 表情包） | ✅ 已完成 |
| P7 | 人格面板 / 好感度 / 专门决策模型插槽 | 插槽已预留 |

---

## 开发

```bash
# 需要 AstrBot 源码在 PYTHONPATH 上
PYTHONPATH=/path/to/AstrBot python -m pytest tests/ -q
```

---

## 许可

AGPL-3.0
