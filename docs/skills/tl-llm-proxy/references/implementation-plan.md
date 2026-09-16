# TypeScript/Node.js 测试代理实现方案

本文件是待实现服务的规格。核心目标是模拟组织内部 TL 报文，提供测试环境；组织内部尚未开放原生 tool_calls，因此采用提示词约定工具语义、普通正文承载结果。代理只做协议转换和流量观测。

## 架构与文件边界

```text
Test client using TL init/chat
   |
   v
TL facade -> session store -> provider adapter -> /chat/completions
   ^                              |               DeepSeek / qwen3.8-flash
   |------ TL envelope / live SSE-|
          Four-direction debug message logs
```

技术栈固定为 TypeScript + Node.js ESM，编译成 JavaScript 运行。Node 基线沿用源项目的 ^22.22.1 || >=24；交付时在实际目标版本验证。使用 Node 内建 HTTP、fetch、AbortController，TypeScript 和测试工具为锁定的开发依赖；不依赖原 monorepo 或 Node 直接执行 .ts。

每个进程配置一个 provider，切换环境变量即可在同一门面上测试 Qwen 或 DeepSeek；需要同时使用时启动两份实例。model、base URL、API key 由服务端固定，不能通过 TL 字段、name 变量或正文改变。默认单实例内存 session，不自动累积聊天历史。

```text
tl-proxy/
  package.json
  package-lock.json
  tsconfig.json
  .env.example
  .gitignore
  .dockerignore
  Dockerfile
  README.md
  src/
    index.ts
    cli.ts
    config.ts
    server.ts
    protocol.ts
    session-store.ts
    errors.ts
    logger.ts
    providers/
      types.ts
      openai-compatible.ts
      sse.ts
      deepseek.ts
      qwen.ts
  test/
    contract.test.ts
    provider.test.ts
    streaming.test.ts
    logging.test.ts
    lifecycle.test.ts
    fixtures/
  dist/
```

源码/注释使用英文。dist 是编译产物，exports/types/bin 指向编译 JS 和声明；CLI 有 shebang。默认 private:true，可用 npm pack 制作独立 tgz。Docker 用于可选共享测试环境，不作为本地测试前置。

createProxy(config) 提供异步 start()/close() 及实际 address；测试端口用 0。构造或 import 不自动监听、注册信号或写全局日志。CLI 负责读取配置、信号和失败退出。公开 TypeScript API 明确定义请求、响应、配置、provider 事件类型。

## 模块契约

| 模块 | 职责 |
| --- | --- |
| protocol/server | 校验 TL 外层结构、路由/方法、输出原有 TL 信封；不解析正文语义 |
| session-store | 保存不可变变量、routeId、createdAt/expiresAt；可选共享测试认证下再绑定 principal |
| provider types | complete() 返回 content/finishReason/usage；stream() 在上游 HTTP 校验后返回包含 AsyncIterable 事件的对象；输入含 messages、服务端配置、stream、AbortSignal |
| transport/sse | HTTP 调用、增量 UTF-8/SSE 解码、取消、超时、大小限制；不依赖 TL HTTP 对象 |
| adapters | 模型特有参数，输出正文及控制事件；不设置原生工具协议 |
| logger | 有方向、有序列、有 requestId 的四向完整报文；凭证定向掩码 |
| errors | 区分 TL 请求、上游协议、HTTP、取消、日志和超时错误，禁止伪造成功 |

## 请求与正文处理

init：校验 TL 信封 → 校验 prompt_variables → 创建随机 session → 返回 data.session_id。不调用上游。默认 system_prompt 变量，支持配置别名；保留 legacy 空变量/name-only 路径，开关为 LEGACY_ROLE_TEXT=true。

chat：校验信封 → 查 session → 绑定 system + 当前 user → 调用上游 → 实时 TL SSE 或完整 data.txt。存在系统变量时两个字符串保持原样；即使 txt 含 system:/user:，也不解释为角色。只有无系统变量且启用 legacy 时才采用原角色文本解析。

**外层协议与正文格式分开处理：**

- 外层请求和非流式响应仍是 JSON；SSE data 仍是 JSON 对象。需要 JSON.parse 的是这些协议封装。
- 正文可为纯文本、Markdown、XML、JSON、代码、工具调用样式文本。原样传递换行、空白、引号和代码块，不 JSON.parse 正文，不剥 Markdown/XML，不校验工具 Schema。
- 默认不发送 response_format，也不读取/猜测提示词来自动启用 JSON 模式。即使提示词要求 JSON，也只是把该提示词原样交给模型。
- 不追加 JSON 关键词、格式示例或修复提示，不做代理侧生成纠错/重试。上层测试客户端决定是否校验模型是否遵守提示词。
- 只要外层响应有效且模型正常结束，空字符串和纯空白也是允许的正文；不因正文不像 JSON 或没有工具操作就拒绝。
- 缺失/非字符串的非流式 content、错误的 SSE 结构、上游错误或显式截断仍是协议/生成失败。正文中的普通拒答文字不是协议错误，应原样转发。

## 未开放原生工具字段的模拟

组织内部不开放原生 tool_calls 是本代理的主要约束，不能为提升成功率偷偷改为原生工具模式。

上游请求不携带 tools、tool_choice、functions、function_call、parallel_tool_calls；TL 请求中出现这些不受支持的原生控制字段时返回清晰 400，不透传。此校验只针对根级/data 级协议字段，不搜索 prompt_variables.value 或 txt 中相同单词。

工具描述、调用格式和参数要求可完全写在系统提示词中。模型若把 JSON/XML/自然语言“工具调用”作为 message.content/delta.content 返回，代理仅把它当普通文本；是否解析并执行属于测试客户端。

上游若意外返回非空 message.tool_calls、delta.tool_calls 或 function_call，则视为不匹配组织协议：发头前 502；发头后 error 并关闭，不发 done。即使同一响应带 content 也不能静默忽略原生工具调用；不得转换成 tool_name/parameters。null 和空数组等无调用占位可忽略；finish_reason:tool_calls/function_call 不算成功结束。此规则同时用于 legacy 和原生变量路径。

## 实时流式状态机

stream:true（含缺省）要求上游 stream:true。先校验上游 HTTP 状态、text/event-stream 和可读 body，再立即发送下游头并 flushHeaders。成功 HTTP 却返回普通 JSON 也不能降级为分片回放。stream:false 才读取完整上游 JSON，返回字符串 data.txt。

用 TextDecoder 流式解码，处理 UTF-8、CRLF 跨分包、完整事件边界、多行 data、注释、BOM。仅缓存未完成事件，设置单事件和累计字节上限。禁止 response.text()/json() 或先累计整段 rawBody 实现流式路径。

选择 index:0，n 固定 1。每个非空 delta.content 字符串立即写 TL chunk，空增量无需发正文帧；空格属于有效字符串，应保留。日志逐事件记录。不要等待正文完整、攒 32 个字符或等待 JSON.parse 正文成功。role-only、reasoning_content、usage-only 不进入 TL 正文；原始上游事件仍按 debug 规则记录。

状态为 waiting_headers → streaming → finished/failed/cancelled。默认兼容 profile 要求 finish_reason:stop 后 [DONE] 才发送唯一 done/finished:true。允许最终正文为空，允许 usage 位于 finish 与 [DONE] 间。EOF 独自出现、提前 [DONE]、length/content_filter、原生工具终止、畸形事件或上游 error 不算成功。显式原生 refusal 字段按不受支持的上游控制字段报错；普通 content 拒答文本原样返回。终止后新增正文失败；非标准 provider 的完成规则须明确验证后配置。

背压时暂停上游迭代，以有界缓冲工作。客户端断开、deadline、解析失败、字节超限或日志写入失败时停止读取、中止 fetch、清理 reader/监听器/计时器。已发头则发一个 error 后关闭；连接已断直接清理，不发 done，不重放部分输出。

若经过共享测试 ingress，关闭该路由响应缓冲、攒批压缩和缓存，保留 no-cache/no-transform、X-Accel-Buffering:no。直接访问与经过 ingress 都验收首增量在上游完成前到达。旧 PageAgent TlAiClient 整段读取后解析；新的 QwenPaw standalone 在工具模式下也等待完整正文校验，而普通文字可增量展示。代理实时与最终 UI 展示分开验收。

## 默认 debug 报文日志

LOG_LEVEL 默认为 debug，默认打印到进程控制台，独立于原仓库的 FileLogger 路径。可显式配置 info/warn/error 降低输出；不能默认只记元数据。

| direction | 记录内容 |
| --- | --- |
| client_to_proxy | TL init/chat 的 method、path、headers、完整 body，包括 prompt_variables 和 txt |
| proxy_to_upstream | 实际发往 provider 的 method、URL、headers、完整 body |
| upstream_to_proxy | provider status、headers、完整非流式 body；流式逐个完整 SSE 事件、终止标记和错误 |
| proxy_to_client | 实际返回的 TL status、headers、完整 JSON body；流式逐帧记录 chunk/done/error |

每条日志有 timestamp、requestId、sessionId（已知时）、provider/model、direction、sequence 和阶段；与实际请求关联，不能凭日志方向猜发往谁。记录凭证掩码后的实际报文，不重新构造一份可能与发送值不同的摘要。

完整保留提示词、用户输入、模型内容和 provider 错误报文，以便定位协议问题。对 Authorization、Proxy-Authorization、Cookie、Set-Cookie、API-key header，以及 URL/对象中的已知凭证字段和当前配置的真实凭证值做定向掩码；不能以脱敏为由省略整段测试正文。返回给客户端的错误仍使用简洁的协议错误信息，不直接泄露 provider 原始错误。

流式日志逐事件输出，不能为了打印“完整响应”先读完整条流。正常情况下写日志不应等待 provider 完成；stdout 背压用有界队列和有序 drain 处理，不静默丢弃 debug 帧。stdout 出错必须可见并停止受影响请求，不能继续假装报文已完整记录。默认不截断合法限制范围内的正文；超过请求/响应限制时记录已收范围、总计数和明确超限事件，不谎称完整捕获。

## 配置与测试环境

| 变量 | 默认/要求 |
| --- | --- |
| TL_PROXY_HOST / TL_PROXY_PORT | 127.0.0.1 / 8089 |
| UPSTREAM_PROVIDER / UPSTREAM_MODEL | 必填；qwen 示例 qwen3.8-flash，DeepSeek 使用实际账号模型 |
| UPSTREAM_BASE_URL / UPSTREAM_API_KEY | 服务端配置；只追加一次 /chat/completions；key 不出现在客户端报文 |
| SYSTEM_PROMPT_VARIABLE_NAME | system_prompt |
| LEGACY_ROLE_TEXT | true |
| LOG_LEVEL | debug，完整四向报文；可显式调低 |
| UPSTREAM_MAX_TOKENS | 可选，按模型限制配置 |
| UPSTREAM_THINKING | provider-default；按 adapter 映射，不跨 provider 透传同一字段 |
| UPSTREAM_TIMEOUT_MS | 120000，总预算涵盖读取及下游排空 |
| STREAM_IDLE_TIMEOUT_MS | 30000，等上游新字节的最长时间；背压暂停读取时暂停空闲计时，总 deadline 仍有效 |
| MAX_REQUEST_BYTES / MAX_RESPONSE_BYTES | 1048576 / 4194304 |
| MAX_SSE_EVENT_BYTES | 262144 |
| SESSION_TTL_MS / MAX_SESSIONS | 900000 / 10000 |
| MAX_INFLIGHT_REQUESTS | 32，超限 503 |
| AUTH_MODE | local，loopback 测试无需额外凭证；共享测试可显式使用 bearer |
| TEST_ACCESS_TOKEN | AUTH_MODE=bearer 时必填；与上游 key 分开，映射固定 test principal |
| CORS_ALLOWED_ORIGINS | 空；浏览器联调按测试站点配置 |

不提供默认强制 JSON 或原生工具开关；response_format/tools 等字段不由代理自行设置。表中数字为可覆盖的实现默认值，不是组织现行规范。启动校验非法配置，但在注入 fake provider 的离线测试中不要求真实 API key。

appId/trCode 是业务元数据，保持可为空；默认 local 模式仅监听 loopback、共享一个 local principal，不假称完成公司身份认证。可选 bearer 模式校验独立测试凭证并绑定 session；组织真实 SSO、复杂授权和审计对接不是本模拟器的前置任务。共享环境暴露地址和接入方式由部署配置明确选择。

单实例 session 重启失效，用 TTL 和容量限制避免测试进程无限增长。多副本需要共享 store 时再实现，不能让随机负载均衡破坏会话。SIGTERM/SIGINT 停止新连接，限时排空请求与日志后清理；close() 可等待且幂等。

## 与 QwenPaw standalone 配合

QwenPaw 每次模型决策新建 session，把经过 memory/compaction 的历史与工具结果序列化为一个 user payload。proxy 始终把 txt 当不透明字符串；不得把历史 role 标签提升为上游 messages、重复累积历史或解析本地 `tool_calls` 正文。工具 JSON 格式错误但上游正常结束时，proxy 仍正常 done；由 standalone 决定是否进行一次语法纠错。纠错是另一对 init/chat，proxy 不主动重试。

两模型同时联调时启动两份 loopback 实例（如 8089/8090），各固定一个模型。QwenPaw 配置对应的两个 TL provider 实例；模型名仅作为已配置路由标签，不经 TL wire 动态传递。内网直连则把 provider 指向公司 endpoint，无需 proxy。

## 实施阶段

1. 固定 TL 信封和测试 fixtures：同时覆盖纯文本、Markdown、XML、JSON 和提示词表达的工具动作。
2. 搭建独立 TS/Node 包、会话、路由和 CLI，用 fake provider 跑通 init/chat；不要引入 UI/DOM 依赖。
3. 完成两种 adapter 和实时 SSE；验证无 response_format/原生工具字段，门闩测试证明首 token 增量在上游结束前到达。
4. 实现默认四向 debug 日志、掩码、限额、取消和可等待关闭；测试日志不会将实时流变成整包回放。
5. build、npm pack，在仓库外安装 tgz 并连接本地 fake upstream 运行编译 CLI；交付 .env.example、测试启动和日志读取说明。
6. 获授权后用真实账号分别联调 Qwen/DeepSeek，按不同提示词验证正文格式与流式时序；如使用共享 ingress，另测入口是否缓冲。

实时转发、任意正文和默认完整 debug 日志是首版要求。附件、多轮历史、自动故障切换、复杂模型路由和生产级组织认证留作后续明确需求；原生工具字段透传不属于当前目标。

## 与客户端一致的资源计量

固定 wire 契约不变；以下补齐实现默认值的计数对象：

- MAX_REQUEST_BYTES=1048576 计每个收到的完整 TL JSON body UTF-8 字节，包括信封及转义；init/chat 分开校验。
- MAX_RESPONSE_BYTES=4194304 计解码后模型正文 UTF-8 字节；stream 为 delta.content 拼接，非 stream 为 message.content。reasoning/control 帧不算正文，但仍受原始字节上限约束。
- 新增 MAX_UPSTREAM_WIRE_BYTES=67108864 与 MAX_DOWNSTREAM_WIRE_BYTES=67108864，分别约束实际收到的 provider body、实际写出的 TL body（含 SSE/JSON 封装），不能把不同封装大小混成一个计数器。日志有自己的有界队列，不复用正文计数。
- 现有 MAX_SSE_EVENT_BYTES=262144 约束单个上游 SSE 事件；生成 TL 帧也应在发送前检查其序列化大小。本套 standalone 的 TL 单事件上限为 1048576；自定义部署改变上游帧上限时验证下游帧也能被接受。
- proxy 上游 UPSTREAM_TIMEOUT_MS 默认 120000；配套客户端每对 init/chat 的 deadline 默认 150 秒。proxy 的 STREAM_IDLE_TIMEOUT_MS 计实际上游字节，包括 reasoning/control；不因暂时没有正文而误触发。TL 客户端不必有 30 秒首正文限制。

流中任一上限触发发 error 并关闭，不发 done；非流式在受限增量读完整 JSON 后响应。增加很多空 control/心跳帧、全反斜杠正文、中文正文与接近上限的合法 JSON/SSE fixtures，分别验证正文计数和 wire 计数。传输日志仍遵循原默认 debug 策略，不为“完整日志”绕过资源上限。
