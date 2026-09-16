# 原 PageAgent tl-proxy 的历史观察

以下为原技能携带的历史取证，未在 QwenPaw 中重新运行或验证；不作为新 provider 的实现依据。原记录日期：2026-09-13。基线仓库 page-agent，revision `bcb64ca3605fe65fd1ad01f540ef80c64e22cced`。下列源码路径相对原仓库根目录，仅作溯源；本技能可脱离该仓库使用。

| 特点 | 已观察的行为 | 独立服务的意义 |
| --- | --- | --- |
| TL 门面 | 两个 chatbbc POST 端点，支持查询字符串路径解析 | 内部调用方继续使用 TL 报文 |
| 原生提示词变量 | init 校验变量名、值、重复项，session 保存变量 Map | system 与 user 分开映射 |
| 单轮状态 | 会话只保留模板变量，不保存历史，无 TTL | 适合每轮 init→chat；需补容量和过期管理 |
| OpenAI 兼容上游 | 请求 /chat/completions，发送 JSON Object 模式 | 可抽出共享 transport 和 provider 参数配置 |
| 缓冲 SSE | 上游 stream:false，完整文本按 32 个 Unicode 码点分块 | 有 SSE 外观，无实时首 token 优势 |
| 取消与背压 | 客户端断开触发 abort；写缓冲满等待 drain | 需补关闭时正在等待 drain 的覆盖 |
| 历史路径 | 无系统变量时解析行首 system/user/assistant 标记 | 隔离为 legacy 模式，不能用于原生变量模式 |
| 工具响应兜底 | content 是字符串时原样返回；否则尝试首个 tool_call 转 TL 文本 | 不代表代理执行工具或支持完整工具协议 |

以上表格仅记录旧代码。新模拟器采用用户确定的 TypeScript/Node.js 技术栈，首版实时转发、默认 debug 打印四向完整报文，正文由提示词决定，不强制 JSON。不能照搬旧 writeSimulatedStream、response_format:json_object 或工具响应兜底。组织内部未开放原生 tool_calls 是重要模拟约束，工具语义只经提示词和普通正文承载。

## 两份实现不能直接互换

| 项目 | TlProxyServer（Qwen） | DsProxyServer（DeepSeek） |
| --- | --- | --- |
| init 绑定 | 完整 prompt variables，可自定义 system 变量 | 读取 name 作为会话模型，没有绑定原生 system_prompt |
| chat 提示词 | 有 system 变量则原样映射；缺失才解析角色文本 | 总是解析 txt 角色文本，并补充 JSON 提示 |
| session 检查 | 缺失/非法 400，未知 404 | 缺失允许；未知只警告并继续 |
| 上游模型 | 服务器 qwenModel，name 不控制路由 | 会话 name 优先于 deepseekModel |
| 服务监听 | 仅配置 port，未限定 loopback host | 默认 127.0.0.1，可配置 host |
| 错误 | 请求错误 {error}；上游错误大多压成 500 | 可保留上游状态，details 可能含原始响应 |

统一版以当前 TlAiClient + TlProxyServer 的原生变量协议为主，复用 DeepSeek transport 思路。不能只更换 URL 就宣布两份开发服务器具有相同语义。

## 客户端职责

TlClient 的 buildPromptPayload 要求至少一条非空 system 消息、恰好一条 user 消息，拒绝 assistant/tool 消息。多条 system 用两个换行连接。invokeAttempt 每次新建 session，携带空文件占位项。配置中的 model 不经 TL 请求传输。

当前 TlClient.parseStreamingResponse 会先 await readStreamResponse 读取完整响应，再调用 parseChatbbcSseContent；因此代理实时转发不自动带来当前 UI 的逐字展示。代理验收使用增量读取 HTTP body 的客户端；若另外要求 UI 逐字显示，再修改客户端增量事件回调，工具执行仍等待完整结果。

JSON 提取、工具查找、参数校验、工具执行和一次 JSON 纠错属于客户端。streaming.ts 接受 done/end/[DONE]，代理实际输出 done + JSON。客户端宽容解析格式不等于服务端必须产生的格式。

## 拆出测试模拟器时的边界

两者是开发模拟器，尚缺独立构建发布入口、TTL/容量、请求大小和明确上游超时等。Qwen 的 info 日志也记录 txt 和非流式结果。新测试模拟器按用户要求默认 LOG_LEVEL=debug，规范为四向完整报文和逐事件日志，只对凭证定向掩码；显式调低日志等级才停止正文打印。两者原有宽泛 CORS 与 appId 都不是认证；新方案默认 loopback 本地测试，共享测试凭证可选，不把公司 SSO 作为启动前置。

独立启动入口还有一个具体缺口：TlProxyServer 类支持 qwenApiKey，但源码 CLI 没有读取并传入 QWEN_API_KEY。现有 npm 启动脚本直接运行 dev-tools 源文件并强制 debug；@page-agent/llms 的发布元数据没有代理 bin 或 dev-tools 导出。独立包需要自己的 CLI、配置、编译产物和运行入口。代理代码仅依赖 Node 内建模块、本地 logger 和 Message 类型，不必为拆包引入 PageAgent 的 UI、DOM 或完整 LLM 包。

新方案的可选测试认证、限额、TTL 和错误规范属于模拟器设计，不代表公司现行规范。真实错误码、附件、认证和多轮规则仍应与组织报文对比。

## 源码索引

- `packages/llms/src/dev-tools/TlProxyServer.ts`：validatePromptVariables、handleChat、extractResponseContent、writeSimulatedStream、parseChatText。
- `packages/llms/src/dev-tools/DsProxyServer.ts`：handleInitSession、handleChat、ensureJsonPrompt、handleRequestError。
- 两个同名 .test.ts：模拟 fetch 的契约验证。
- `packages/llms/src/TlClient.ts`：initSessionWithPromptVariables、buildPromptPayload、invokeAttempt、canAttemptJsonCorrection。
- `packages/llms/src/streaming.ts`：parseChatbbcSseContent。
- `packages/llms/src/dev-tools/logger.ts` 和 `packages/llms/package.json`：开发日志和运行入口依赖。
