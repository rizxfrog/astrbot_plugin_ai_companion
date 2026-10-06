# 真实平台端到端联调报告

对运行中的 AstrBot 实例（`http://192.168.5.247:6185`）做真实链路验证。
**不 mock 平台**：测试桩以 OneBot v11 实现端身份反向连入 `ws_reverse_port`，
投递真实事件、接收 bot 的真实 API 调用。

## 环境

| 项 | 值 |
|----|----|
| 平台 | OneBot v11 (aiocqhttp)，反向 WS `:6199` |
| 协议端 | 本地测试桩 `tests/onebot_stub.py`（伪装成 OneBot 实现端） |
| 插件版本 | v0.7.2 |

## 结果

| 场景 | 结果 | 证据 |
|------|------|------|
| 群聊 @bot | ✅ 回复 | 真实文本回复 |
| 群聊未 @（AI 自主决策） | ✅ 3/4 回复，1 条决定不回 | 决策链日志 `rule/probability/llm_judge` |
| 私聊 | ✅ 回复 | —— |
| 消息落库 | ✅ 43 条 | `/companion_status` 输出 |
| 中文检索 + 会话隔离 | ✅ | `/companion_search 在吗` 仅命中私聊；`今天` 命中群聊 |
| **表情包（真实图片）** | ✅ | 下发的 `image` 段 base64 **字节级等于**本地 `开心/happy1.png` |
| 错别字 | ✅ | `舒畅有轻盈`（应为「又」），命中混淆表 |
| **主动消息** | ✅ | 沉默 118s 后主动开口，且**无标记泄漏** |
| 工具注册 | ✅ | `search_chat_history`/`lookup_person`/`recall_events`/`send_sticker` |

## 联调发现并修复的 3 个真实缺陷

1. **主动消息绕过装饰钩子** —— 主动消息走 `context.send_message`，不经过
   `on_decorating_result`，导致 `[sticker:x]` 标记字面发给用户。
   → 新增 `Humanizer.apply_to_text()`，在主动发送路径单独处理。

2. **悬空媒体占位符** —— 模型写 `[图片]` 却拿不到图片，字面量直接发给用户。
   → 无实际图片时清掉 `[图片]/[image]` 等占位符。

3. **模型构造非法消息段** —— 提示词教它写 `[sticker:x]`，模型却发明
   `{"type":"sticker"}`，平台报 `unsupported message type 'sticker'`，
   模型还把排错过程说给用户听。
   → 表情改为真正的 `send_sticker` 工具；并在 `on_using_llm_tool` 钩子里
     把非法段降级为合法文本、清理占位符。

## 已确认为平台行为（非本插件缺陷）

平台上下文压缩 `ar.compression.overflow_strategy = "llm_compress"` 会生成英文摘要
并注入后续请求。若对话被大量工具排错过程污染，摘要会显得跑题。
建议把 `trim_turns` 设大或改用 `drop` 策略。
