# 工具模式实时预览与最终提交

本文件是新增的 QwenPaw 客户端/界面设计，尚未实现。公司 TL 报文、proxy chunk/done 格式及模型正文 JSON schema 均不改变。

## 两条输出路径

`stream=True` 只保证流式接收，不保证界面逐字显示。实现目标是支持实时预览，同时保留完整校验后才提交工具调用的规则：

```text
TL chunk.content → 完整正文缓存 → TL done → 严格全量校验 → 正式 ChatResponse → Agent/Toolkit
                ↘ 增量 JSON 读取 → 请求内临时预览事件 → Console 临时状态
                                                         ↑ 清除/由正式内容替换
```

| 场景 | 完成前能显示什么 | 正式提交时机 |
| --- | --- | --- |
| 无工具 / choice=none | 普通文字流，走现有增量 TextBlock | 现有文字模式最终快照 |
| 工具模式，type=final | 已解码的 content 字符串前缀，标记为正在生成 | done + 全量校验后正式 text |
| 工具模式，type=tool_calls | 工具名称及参数草稿，标记为正在生成调用 | done + 整批工具与参数校验后正式 tool_call |
| 工具执行中 | 现有工具状态及工具自身提供的进度 | 现有 ToolResult/执行流程；不由 TL 预览伪造 |
| 只返回非流式响应的部署 | 等待状态 | 完整响应后；客户端无法制造尚未收到的内容 |

普通用户视图不直接倾倒整个 envelope JSON；工具参数草稿用纯文本折叠视图。没有参数进度的工具不能承诺实时执行结果。上游只有 reasoning、没有 content 时也只能显示等待状态，不能把 reasoning_content 变成正文。

## 预览不是 AgentScope 内容

不能提前 yield TextBlock(raw_json)、未完成的 ToolCallBlock 或假工具状态。它们会被现有 agent/envelope 当作正式事件内容，进入累加与持久化路径；语法纠错、尾部错误或批次参数失败时无法可靠撤销。现有“完整非 final 响应 → 同内容 final 快照”仍用于唯一的正式提交，见 [运行时兼容边界](runtime-compatibility.md)。

定义请求范围的可选 `preview_sink`，通过独立有界队列将 provider 的临时事件交给运行时，运行时同时消费 agent stream 与该队列。此名称为实现建议，不是现有 SDK 参数。sink 应绑定 run/invocation/attempt，使用请求作用域并在结束时解除，不能用进程全局列表或 provider 实例的共享可变缓冲；不进入 TL 请求字段。

Console 支持该功能后可默认展示预览；不支持清除/替换的渠道、旧客户端和独立无 UI 调用自动沿用完整校验后显示的路径。应由实际客户端能力决定，不能把草稿降级成普通聊天消息发送给不能撤回的渠道。

## 当前项目的接入点

基线源码中没有现成的临时预览协议，以下各层需要一起完成，不能只增加 provider 的 callback：

| 现有位置 | 所需适配 |
| --- | --- |
| `src/qwenpaw/runtime/executor.py` 的 AgentExecutor.run | 在 agent stream 等待下一条事件期间也能消费预览队列；不能等正式块到来时才顺带排放预览 |
| `src/qwenpaw/runtime/envelope.py` 的 Envelope | 为临时事件增加独立投影，禁止写入 `_completed_message`、`_tool_calls` 和 `_response.output`；不复用普通 text/plugin_call 事件 |
| `src/qwenpaw/app/channels/console/channel.py`、`app/channels/base.py` | 明确识别临时事件和客户端能力；不进入正文聚合、headline 处理、通知或其他渠道发送路径 |
| `src/qwenpaw/app/task_tracker.py` 的 attach_or_start | 现有 producer 把每个 SSE 写入 run.buffer，并推入无界订阅队列；预览须从重连回放排除，实时分发使用可合并、有界的临时通道，不能只限制 provider 内的第一层队列 |
| `console/src/pages/Chat/index.tsx` 的 responseParser、`HostBubbles.tsx` | 增加独立草稿状态/展示；预览事件在进入普通消息 Builder 前消费，不并入聊天消息数组；结束/取消/切换会话时清理 |

锁定 SDK 的模型响应 metadata 不能直接保证抵达 Console；当前自定义/HINT 事件也没有可复用的预览转换。前端 responseParser 对部分控制事件用 heartbeat no-op 兼容下游 Builder，源码明确说明某些路径返回 null 会导致后续流中断；新增预览分支应在 Builder 前过滤或返回已验证的 no-op，必须使用项目实际 Console SDK 做集成检查，不能凭空假定 null/未知事件可安全忽略。

## 临时事件与界面状态

事件名称为新的本地契约，实施时接入项目 Host/Console schema；不是 chatbbc SSE 的新 event，也不是工具回执：

- `preview_start`：携带 run_id、invocation_id、attempt_id，建立独立草稿状态。
- `preview_update`：携带相同关联键、递增 revision、kind(final_text/tool_call)、item_index 和**累计文本快照**。界面按键覆盖，不追加；旧 revision 丢弃。工具 item_index 只是草稿索引，不能充当正式 tool_call ID。
- `preview_clear`：携带关联键及原因 invalid/correction/cancel/error/commit，清除此 attempt 的全部临时状态。失败说明仍通过项目现有错误路径显示，不把失败草稿保存成回答。

正式输出前先停止并排空该 attempt 的 update，将 clear(commit) 排在第一条正式内容之前，合并事件时保序；一旦 clear，该 attempt 的迟到 update 永久丢弃。正式 text/tool event 是唯一的持久内容。不要先发送一遍草稿，再把完整正文追加为第二遍回答。

语法纠错只在原有条件下发生：先 clear(correction)，第二次使用新 attempt_id。语义校验失败、error/EOF、取消及响应超限都 clear，工具执行次数为零；响应 JSON 看似闭合也要等有效 done。断连/页面切换/响应结束清空临时状态，不用本地存储或聊天历史恢复草稿。v1 重连后可对当前 attempt 关闭预览直到下一次 preview_start，正式输出继续按现有回放/实时流程处理，避免重建已经撤销的草稿。

预览更新可以节流、合并为最新快照，以限制 CPU、SSE 和前端渲染频率；start/clear/正式输出不可丢失。慢界面不能使正文缓存无限增长或阻塞 transport 超出原 deadline。队列满时只合并 update，失败可关闭该请求预览并清理草稿，正式传输/校验保持原规则。新 stream 的初始状态与终止状态都应清理所属 run 的旧草稿，避免重连残留。

## 增量解析只决定显示

- 在 TL SSE 解码之后读取 chunk.content。使用有状态 JSON tokenizer/parser，正确处理任意分块、引号、反斜杠、转义字符及跨块 Unicode 转义；只显示已经完整解码的字符，不用正则截取 content，也不每收到一字就重解析全部正文。
- 仅当顶层 type 已明确为 final/tool_calls 且结构前缀无冲突时显示对应草稿；content 先于 type 时先缓存，等判别成立后显示。提示词建议 version→type→content/calls 顺序以降低延迟，最终校验不得因合法字段顺序不同拒绝响应。
- 不扫描嵌套 content、工具结果或字符串内的 type/tool_calls 字样来改变模式；工具名称和 arguments 前缀仅作未校验数据。若出现重复键或结构矛盾，停止并清除预览，继续按原终止规则获得权威结果/错误。
- 始终保留完整原文用于独立的严格解析、schema、工具名及参数校验。增量解析成功不代表模型输出合法，不能解锁工具或绕过调用批次校验。对预览的内容与渲染大小设置上限，不对正式正文做截断修复。
- 仅一次 JSON 语法纠错的条件和调用预算不因已经展示草稿而扩大；临时预览不算模型 block，工具仍未提交。结构化输出首版可以只显示等待状态，不必同时实现任意 schema 的增量业务 UI。

## 验收时序

用可暂停的 fake TL 流，不靠时间碰巧通过：发送 final.content 的首段后暂停；断言 done 之前 Console 已有对应临时文本，AgentScope 输出和工具执行均为零；继续发送剩余正文与 done 后，断言草稿消失且正式回答只出现一次。

工具分支同样检查参数尚未完整、JSON 闭合但未 done、整批第二项参数错误、尾部 error、语法纠错、取消、两个并发 run、旧 attempt 迟到更新与不支持预览的渠道。各失败场景都没有工具调用入历史，成功场景通过现有 guard 执行一次。另测中文/emoji/转义跨分块、字段乱序、重复键以及缓慢消费者；预览顺畅与正式响应正确是两个独立断言。
