# 更新日志

本文件记录 AI Companion 插件的所有重要变更。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

## [v0.11.0] - 2026-10-07

### 新增

- **群画像与熟悉度**：像真人一样融入群——先观望、逐步形成对这个群
  「聊什么/氛围如何/都有谁」的长期印象，熟悉度随之上升，进而更愿意主动搭话。
  - 新表 `group_profiles`：每个群的滚动更新画像，跨重启保留
  - 后台循环按消息增量更新画像（观察 → 合并成新画像）
  - 熟悉度 0~100，由「消息量 + 画像轮次 + 认识的人数」驱动，映射三档
    （观望 / 融入 / 熟悉）
  - 群聊概率随熟悉度渐进：`group_reply_probability` 作为基础值，
    熟悉度满时最多放大到 `familiarity_gate_scale` 倍（默认 3）

### 修复

- **读空气从不知群为何物**：`_judge` 此前只拿到最近 10 条消息 + 固定提示词，
  因此对不熟的群只会判断「话题与我无关、插不上话」，24 小时日志里
  105 次判断全否、1 次放行。现在把「群画像 + 对发言者的了解 + 最近事件」
  一并注入读空气，让判断有据可依。

### 变更

- 读空气提示词从「矜持旁观」改为「渐进式群友」：不熟的群少说多看，
  熟悉的群对懂的话题自然接一句
- `ProbabilityDecider` 的 `rate` 回调支持异步（需查库取熟悉度）

## [v0.10.1] - 2026-10-06

### 修复

- **连发合并时图片被丢弃**：用户「先发图、再发一句话」时，两条消息被等待窗口
  合并成一轮，但图片只从最后一条事件取，导致先到的图片丢失（表现为 bot 回
  「我这边只有个图片标记，瞅不见」）。现在合并时会把各轮图片一并继承并去重。
- 图片收集提前到进入等待之前，避免本轮被后续消息合并后事件失效。

## [v0.10.0] - 2026-10-06

### 修复

- **图片进不了模型**：插件通过 `yield event.request_llm(...)` 自己包办 LLM 请求，
  而平台只在自己创建请求时才扫描消息里的图片组件，结果用户发的图片根本到不了
  模型，模型只看到占位文本。现已补上与平台一致的收集逻辑。
- **工具调用 XML 泄漏到正文**：部分模型会把工具调用当文本写出
  （`<invoke name="send_sticker">...`），而平台只认结构化的 `tool_calls` 字段，
  不会解析这种文本，于是原样发给用户。现已在三个路径统一清除，并兼容
  `antml:` 前缀、`function_calls` 包裹与残缺/孤立闭合标签。

## [v0.9.0] - 2026-10-06

### 新增

- **连发合并（debounce）**：收到消息后不立即决策，等窗口内不再有新消息再统一处理。
  用户把一句话拆成几条发送时，不会再出现「同样的话回答两遍」。
  - 群聊与私聊等待时长分别可配（默认群聊 3 秒、私聊 5 秒）
  - 指令消息不等待，保证即时响应

### 说明

- 根因并非「每条消息各跑一次 LLM」，而是平台会把运行中 Agent 期间到达的消息
  作为 follow-up 注入，模型同时看到多条未回答的消息，于是一次性把它们都答了。

## [v0.8.2] - 2026-10-06

### 修复

- **`[图片]` 占位符泄漏**：事件路径漏做悬空媒体占位符清理，用户会直接看到
  `[图片]` 字面量（主动消息路径一直有这步）。

### 变更

- 表情闸门全关时不再向模型注入发表情引导，省去一次注定被拦的工具往返。

## [v0.8.1] - 2026-10-06

### 新增

- **表情发送统一闸门**：表情有三条发送路径（AI 写 `[sticker:x]` 标记、AI 调用
  `send_sticker` 工具、代码自动补图），此前只有自动补图被限流，导致「AI 主动要」
  必然发出。现在三条路径共用同一闸门。
  - `sticker_send_probability`（默认 0.2，即拦掉 80%）
  - `sticker_cooldown_seconds`（会话级冷却，默认关闭）

## [v0.8.0] - 2026-10-06

### 新增

- **拟人人格**：平台内置人格是 `"You are a helpful and friendly assistant."`，
  这正是「助手腔」的根源。插件现提供可覆盖的拟人人格，从源头消除客服口吻，
  并明确禁止自称 AI、罗列功能清单。
- **身份隐藏（三层防护）**：
  1. 人格层（主力）——让模型没有自称 AI 的动机；
  2. 定向指令——识别到身份追问时，本轮追加「岔开」指令，群聊与私聊话术不同；
  3. 输出兜底——回复仍然自曝身份时，私聊换成兜底话术，群聊直接不发送。

### 说明

- 身份识别使用显式句式而非「含 AI 关键词」，避免把「你觉得 AI 会取代人类吗」
  这类正常讨论误判成身份追问。拦截只在模型确实自曝身份时触发，正常对话不受影响。

## [v0.7.3] - 2026-10-06

### 变更

- **概率层语义调整**：从「最终回复概率」改为「**进入 AI 决策的概率**」（成本闸门）。
  未命中由代码直接判定不回复，零模型调用；命中才交给读空气。
- 新增 `group_reply_probability`：群聊未被 @ 时专用门槛，`-1` 表示继承默认值。

## [v0.7.2] - 2026-10-06

### 修复

- 真实平台端到端联调发现并修复三处缺陷。
- 补充真实平台联调报告（`docs/E2E_LIVE.md`）。

## [v0.7.0] - 2026-10-06

### 新增

- **拟人增强（P6）**：表情包与错别字。
  - 表情包按分类目录随机抽取，AI 可在回复中主动发表情
  - 错别字按概率制造轻微手误（默认关闭）

## [v0.6.0] - 2026-10-06

### 新增

- **事件线（P5）**：从对话中抽取事件，供后续回忆与上下文注入。

## [v0.5.0] - 2026-10-06

### 新增

- **人物画像与关系图谱（P4）**：后台增量抽取人物信息与人物间关系。

## [v0.4.0] - 2026-10-06

### 新增

- **主动消息（P3）**：在会话静默超过阈值后由 bot 主动发起对话。

## [v0.3.0] - 2026-10-06

### 新增

- **短期记忆压缩（P2）**：历史过长时把最早的一段对话总结成摘要滚动保留。

## [v0.2.0] - 2026-10-06

### 新增

- **LLM 读空气（P1）**：群聊中由一次轻量模型调用判断「此刻想不想接话」。
  该调用刻意不走平台 Agent 链路——没有工具、没有人格、上下文极小，
  因此又快又便宜，也不会污染主对话历史。

## [v0.1.0] - 2026-10-06

### 新增

- **P0 骨架**：群聊与私聊统一抽象，AI 自主决定「这条要不要回」。
  - 可插拔决策链（硬过滤 / 规则 / 限流 / 概率 / AI 读空气）
  - 每会话运行态注册表，支持同时关注多个窗口而不 per-window 常驻 Agent
  - 全量消息落库（SQLite + FTS5 trigram 中文检索）
  - 精简动态上下文注入

[未发布]: https://github.com/rizxfrog/astrbot_plugin_ai_companion/compare/v0.11.0...HEAD
[v0.11.0]: https://github.com/rizxfrog/astrbot_plugin_ai_companion/compare/v0.10.1...v0.11.0
[v0.10.1]: https://github.com/rizxfrog/astrbot_plugin_ai_companion/compare/v0.10.0...v0.10.1
[v0.10.0]: https://github.com/rizxfrog/astrbot_plugin_ai_companion/compare/v0.9.0...v0.10.0
[v0.9.0]: https://github.com/rizxfrog/astrbot_plugin_ai_companion/compare/v0.8.2...v0.9.0
[v0.8.2]: https://github.com/rizxfrog/astrbot_plugin_ai_companion/compare/v0.8.1...v0.8.2
[v0.8.1]: https://github.com/rizxfrog/astrbot_plugin_ai_companion/compare/v0.8.0...v0.8.1
[v0.8.0]: https://github.com/rizxfrog/astrbot_plugin_ai_companion/compare/v0.7.3...v0.8.0
[v0.7.3]: https://github.com/rizxfrog/astrbot_plugin_ai_companion/compare/v0.7.2...v0.7.3
[v0.7.2]: https://github.com/rizxfrog/astrbot_plugin_ai_companion/compare/v0.7.0...v0.7.2
[v0.7.0]: https://github.com/rizxfrog/astrbot_plugin_ai_companion/compare/v0.6.0...v0.7.0
[v0.6.0]: https://github.com/rizxfrog/astrbot_plugin_ai_companion/compare/v0.5.0...v0.6.0
[v0.5.0]: https://github.com/rizxfrog/astrbot_plugin_ai_companion/compare/v0.4.0...v0.5.0
[v0.4.0]: https://github.com/rizxfrog/astrbot_plugin_ai_companion/compare/v0.3.0...v0.4.0
[v0.3.0]: https://github.com/rizxfrog/astrbot_plugin_ai_companion/compare/v0.2.0...v0.3.0
[v0.2.0]: https://github.com/rizxfrog/astrbot_plugin_ai_companion/compare/v0.1.0...v0.2.0
[v0.1.0]: https://github.com/rizxfrog/astrbot_plugin_ai_companion/releases/tag/v0.1.0
