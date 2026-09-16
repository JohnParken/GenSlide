---
name: tl-llm-standalone
description: 为 QwenPaw 设计、实现或维护对接公司内部 chatbbc 两段式接口的 Python provider，以 system_prompt 和模型正文承载工具协议，并适配 AgentScope 工具循环；也可连接同协议的本地 TL proxy 联调 DeepSeek、qwen3.8-flash。
---

# QwenPaw TL system_prompt provider

本技能由原 `tl-llm-provider` 改造。这里的 **standalone 是直接对接公司内部接口的客户端/provider**，不是模型代理服务器。它在 QwenPaw Python 进程内运行，通信层可独立测试；不要求启动 TypeScript 服务。本文和 references 是待实现设计，不表示项目已注册或实现该 provider。

## 固定事实与职责

- 公司接口固定为 `POST /chatbbc/init_session` → `POST /chatbbc/chat`。保留元数据、`prompt_variables`、`session_id`、`txt`、`files`、`stream` 和 JSON/SSE 成功信封。
- 公司接口**无法使用原生 tools/tool_calls 接口**。这是不可改变的约束，不是待探测能力；不能通过换 SDK、额外字段或模型支持情况绕过。
- 系统提示词只放 init 的系统变量（默认 `system_prompt`）；chat 的 `txt` 是动态 user payload。历史和工具结果可序列化为该 payload 中的数据，不能重新拼接 `system:` 角色文本。
- `tool_calling_mode="system_prompt"` 是 QwenPaw 本地配置。工具定义通过系统提示词传递，调用意图通过普通正文返回；解析后产生的 AgentScope `tool_call` 只存在于本地，不是 TL 原生 `tool_calls`。
- Provider 不执行工具。QwenPaw 的工具注册、ToolGuard、审批和 ReAct 执行链继续负责查找与执行。

```text
QwenPaw memory + Toolkit
        ↓ formatter / prompt codec
TLChatModel → init_session + chat → 公司内部 TL 网关 → 内部模型
        ↘ 同一客户端改用本地 endpoint → TL proxy → DeepSeek / qwen3.8-flash
        ↑ 完整正文校验 → ChatResponse(text / tool_call)
QwenPaw ReAct + guarded Toolkit 执行 → tool_result → 下一轮新 init + chat
```

`tl-llm-proxy` 是另一份服务端 skill：它模拟内部接口并连接公网模型，只转发正文，不解析或执行工具。公司内部直连不经过该 proxy，也不在客户端持有公网模型 key。

## 实施时按需读取

1. 先读 [客户端报文与会话](references/tl-client-contract.md)，不得增改公司协议以迁就 SDK。
2. 实现工具语义时读 [提示词、历史与输出协议](references/system-prompt-protocol.md)、[输入编译与上下文预算](references/prompt-compilation.md) 和 [输出 JSON Schema](references/tool-envelope.schema.json)。只采用这一版 QwenPaw 协议；不自动猜 PageAgent action、XML 或 Markdown 中的动作。
3. 接入项目时读 [QwenPaw 接入设计](references/qwenpaw-integration.md)，并读 [运行时兼容边界](references/runtime-compatibility.md)，核对实际消息块、事件、超时及配置测试入口。
4. 实现工具模式的逐字显示时读 [实时预览](references/streaming-preview.md)，将可撤销的界面预览与已校验的模型响应分开。
5. 用 [往返样例](examples/tool-roundtrip.json) 建立离线 fixtures，再按 [验收要求](references/acceptance.md) 检查 wire、AgentScope 语义及完整工具回路。

## 执行边界

- 面向当前项目采用 Python 3.11+、异步 HTTP transport 和 AgentScope `ChatModelBase`/`ChatResponse` 适配，不复用 PageAgent 的 MacroTool 执行器。原 TS/Python PageAgent 示例已由当前协议样例替代。
- 不把默认 OpenAI formatter 输出直接作为 TL HTTP JSON；formatter 必须保留历史及工具关联，model 再编译成一个 system 字符串和一个 user payload。
- 工具模式完整缓存并校验正文，成功终止前不发布 AgentScope 模型 block；可通过独立临时事件实时预览，支持清除与最终替换。普通文字模式按锁定 AgentScope 版本输出增量和最终完整响应。
- 最多一次、严格限定为完整输出的 JSON 语法纠错；网络/SSE 错误、工具名错误、参数校验失败不走自愈，不用 `json_repair` 猜参数。
- v1 只承诺文本输入与本地工具协议。不透传附件、原生 JSON 模式、每轮选模型或思考参数；能力与配置限制须在创建/更新/检查时明确体现。
- 本地 proxy 调试与内网真实接口分别验收。缺少账号不阻止实现和离线检查；不能把离线通过写成真实联调成功。

实现请求的交付物是 provider/transport/formatter/codec、注册与配置接入、最窄相关测试和运行说明。只请求设计或改造 skill 时交付技能规格及验证结果，不宣称运行时已经可用。
