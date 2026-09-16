# 输入编译与上下文预算

本文件补充 system_prompt 协议的取值位置与预算，属于待实现规格。数据形状只以 [正文协议](system-prompt-protocol.md) 为准。

## 复用已经准备好的输入

锁定的 AgentScope `agent/_agent.py` 中，`_get_system_prompt` 合并基础指令、已激活 skill、workspace/offloader 指令及 system middleware；`_prepare_model_input` 再依次加入 system、压缩摘要和当前 context，并通过 `get_tool_schemas(activated_groups)` 取得本轮工具。随后 model-call middleware 仍可修改输入：例如 QwenPaw `agents/middlewares.py` 的 MemoryMiddleware 注入自动记忆检索结果。

因此以**所有 model-call middleware 之后、TLChatModel 实际收到的 messages/tools/tool_choice**为唯一输入，在调用开始时做不修改源对象的快照，再交 TLChatFormatter/codec。不能在构造 provider 时缓存整段 system，不能从 agent.state.context 重新取历史，也不能让 codec 自己读取 AGENTS.md、检索记忆或列举全局 Toolkit。

| 输入来源 | TL 中的位置 | 要保留的语义 |
| --- | --- | --- |
| 最终收到的 SystemMsg 文本 | init 的系统变量 | 既有指令、已激活 skill、workspace 与记忆使用规则，按原顺序保留 |
| 本轮 tools 的 function name/description/parameters | init 系统变量中的工具目录 | 当前可见工具及完整参数约束；仅从 schema 容器提取，不发送原生 tools 字段 |
| ToolChoice / 输出 schema | init 系统变量中的本轮输出协议 | auto/none/required/指定名及白名单；文字、工具、结构化三种输出协议互斥 |
| 压缩摘要、记忆检索结果、user/assistant 文本 | chat.txt 的有序历史记录 | 接收 middleware 给出的顺序与来源，不再从存储补回已压缩的旧对话 |
| call/result/hint 及其状态 | chat.txt 的有序历史记录 | 按 block 拆分、依 ID 关联、保留失败与部分结果；Hint 不是 system |

系统中的记忆**使用说明**与动态检索到的记忆**内容**要分别保留；不能把两者都移到 system，也不能因为 memory 未写入 state.context 就漏掉它。

## 编译不变量

1. 每次决策重新使用本轮快照，先应用 ToolChoice 白名单，再编译工具目录和对应校验器。同名重复工具、无法支持的 schema 在发送前明确报错，不能由后一个覆盖前一个。
2. 同一 attempt 的提示词、可用工具名及参数校验器来自同一份快照。流中 Toolkit 变化不回写快照；执行时仍由现有 guarded Toolkit 检查当前可用性与权限。下一次模型决策取得新工具和新系统指令，覆盖动态激活/关闭工具及临时 generate_response 工具。
3. 仅由 codec 追加一次当前协议和目录，不在代理、formatter 和 model 三处重复追加。不用全文去重误删正常指令；旧 PageAgent/XML 工具模板不能与新 JSON 协议一起安装。原有任务与行为指令保留，调用的序列化形式由 TL 协议段规定。
4. 历史 JSON 只序列化一次成为 txt；HTTP JSON 再对该字符串做必要转义属于正常 wire 编码。arguments 对象与 SDK input 字符串的转换仅发生在明确边界，不能逐层反复转义。
5. 输入 txt 始终为有序历史 JSON。`none` / 无工具只改变**输出**为普通正文，不丢摘要或历史、不因看到 JSON 样式文本执行工具。独立结构化模式输出目标业务 schema，不同时要求工具 envelope。
6. 语法纠错复用原始输入与工具快照，失败正文只作为动态数据。正常下一轮才获取新的调用输入，不能因纠错又检索一份历史或悄悄改变允许工具。

## 预算必须覆盖编译后的两段正文

当前 SDK 默认 `ChatModelBase.count_tokens` 从 Msg 文本/块与 tools JSON 估算，未计算新增 TL 协议、规范化历史标签与 JSON 转义。QwenPaw scroll 的压缩判断调用外层 `agent.model.count_tokens`；TokenRecordingModelWrapper、RetryChatModel、FallbackChatModel 和 RoutingChatModel 当前继承该默认实现，没有自动转发新的 TL 计数方法。只重写 TLChatModel.count_tokens 不足以让压缩决策生效。

实现时必须做到：

- codec 提供无网络、无源对象修改的编译过程，计数与发送使用同一套系统/历史序列化规则。计算 `compiled_system + serialized_user_payload` 的输入成本，工具目录只计一次，并保留输出预算。TL HTTP 信封的转义字节由 transport 的独立 max_request_bytes 检查，不当作模型输入 token 重复计数。
- 覆盖 TLChatModel.count_tokens，并在实际最外层 wrapper/routing 路径显式接通 TL 计数；普通 provider 的计数行为保持原样。fallback 在每个实际候选发送前用该候选预算检查，不能拿主模型窗口大小替代它。
- SDK 的 count_tokens(messages, tools) 未接收 ToolChoice，预压缩可按全部可见工具估算；实际发送前再按选定模式和最终 middleware 输入做预算检查。后者包括新注入记忆、临时输出 schema 和语法纠错 payload，不能只依赖之前的压缩估值。
- 有已知部署 tokenizer 时用它；公司隐藏模型或 tokenizer 不明时使用可配置的保守估算与余量，明确标记为估计，不宣称精确用量。不要拿默认 UTF-8 字节数除以 4 当作中文或 JSON 的安全上界。
- 本地发送前超限接已有一次 context recovery，只在重建输入发生变化后再试。若 system/工具目录本身已占满预算或重建后仍超限，明确失败；不能截断 JSON、删 required/enum、静默移除工具或恢复已压缩历史。纠错附带原失败正文后超限则停止纠错，不扩大已授权调用预算。

验收除了普通短对话，还要包含：激活工具后下一轮目录更新、仅由 middleware 注入的记忆、压缩摘要、纯 system/工具目录超限、中文及大量转义、外层模型实际调用 TL 计数器。计数过程不得触发 init/chat、文件读取或工具执行。
