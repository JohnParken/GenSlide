# 客户端固定报文与会话契约

以下成功报文来自原 skill，是本次不能改变的兼容基线。公司错误码、认证、附件及服务端历史策略未由这组成功样例完整定义，不把模拟器新增策略当作公司事实。所有值均为合成示例。

## 两段式 HTTP

`base_url` 表示 TL 服务根地址，可带公司路径前缀。只去掉尾部 `/`，再追加固定 `/chatbbc/...`；不要用会丢失路径前缀的绝对 URL join，也不要追加 `/v1` 或 `/chat/completions`。

每次请求根级包含 `appId:string`、`trCode:string`、`trVersion:string`、`timestamp:number`（Unix 毫秒）、`requestId:string`、`data:object`。init 与 chat 各生成独立 requestId，由本地 attempt ID 关联。业务字段不是身份凭证；客户端发送配置值（可为空），内网是否接收由真实接口决定。

`POST /chatbbc/init_session`，`Content-Type: application/json`：

```json
{
  "appId": "internal-demo", "trCode": "agent-chat", "trVersion": "1.0",
  "timestamp": 1789258985000, "requestId": "init-example-1",
  "data": {"prompt_variables": [{"name": "system_prompt", "value": "完整系统提示词和工具协议"}]}
}
```

```json
{"code":0,"message":"success","data":{"session_id":"session_example"}}
```

校验 HTTP 成功、外层对象、数值 `code=0`（不接受布尔值/字符串）及非空字符串 `data.session_id`。系统变量名可配置，与内部模板一致；新 provider 始终使用非空系统变量，不依赖旧的无变量角色解析路径。

`POST /chatbbc/chat`，`Content-Type: application/json`，流式另加 `Accept: text/event-stream`：

```json
{
  "appId": "internal-demo", "trCode": "agent-chat", "trVersion": "1.0",
  "timestamp": 1789258985100, "requestId": "chat-example-1",
  "data": {"session_id":"session_example", "txt":"动态 user payload", "files":[], "stream":true}
}
```

`txt` 始终为字符串。工具模式下内容是序列化的会话数据，仍只占一个 user 正文；不是 wire messages 数组。系统指令和工具 schema 不复制到 txt。客户端明确传布尔 stream；文件用 `[]`，兼容接收方已有的全空文件占位格式。非空附件不宣称支持。

`stream:false` 成功：

```json
{"code":0,"message":"success","data":{"txt":"模型原始正文"}}
```

校验外层 `code=0`、字符串 `data.txt`。空字符串可为合法传输结果；是否满足当前工具/结构化输出契约由上层另判。

`stream:true` 成功：

```text
event: chunk
data: {"content":"模型原始正文"}

event: done
data: {"finished":true}

```

正文只能来自 `chunk.content` 或非流式 `data.txt`，不得读取 OpenAI choices、usage 或原生 tool_calls 替代。proxy 保持此 done JSON；客户端兼容原 skill 的 `event: done/end` 及 `[DONE]` 终止形式，不因此改变服务端输出。

## SSE 客户端状态机

- 使用异步字节流和有状态 UTF-8 解码器，事件状态跨 HTTP 分包保存；按空行结束一个事件，支持 CRLF、BOM、注释、多行 data 和一包多帧。
- 不对 `content` 做 trim；空格、换行及中文字节必须保持。只忽略注释，不静默吞掉畸形 JSON、错误类型或未知的非心跳事件。
- 完整 `chunk` 的 data 必须为对象且 content 为字符串。独立处理 SSE event 名及 data；不能在每次读取网络块时重置 event 名。
- `error` 事件始终先于终止判断处理，即使其 data 恰好为 `[DONE]`。错误立即失败；不拿已累计正文执行工具。
- 选定的兼容 profile 接受 `done/end`（有效 JSON 终止对象，`finished` 如存在必须为 true）或非 error 事件的 `[DONE]`；记录使用的结束形式。`done` 畸形 JSON、`finished:false`、没有结束标记的 EOF 都失败。公司有其他已确认形式时显式增加 fixture，不静默放宽。
- 成功终止、取消、超时或失败均关闭/取消 reader 与 HTTP response。无无限等尾帧、无 `except: pass`，大小和时间预算有界。
- 工具模式持续读取并完整缓存；可将 chunk.content 同时交给独立的增量预览解析器，但只有终止成功后才能进行权威全量校验并提交模型 block。预览必须可清除且不触发工具；proxy 实时 SSE 与界面显示分别验收，见 [实时预览](streaming-preview.md)。

## 历史、取消与重试

每次模型决策使用新的 init→chat；格式纠错也使用新会话及原系统变量。不要把 `session_id` 当作 QwenPaw conversation ID，不跨用户、轮次或工具 schema 复用。这样即使公司服务会积累会话历史，每个会话只发一次 chat，不依赖其历史行为。

QwenPaw memory/compaction 提供的历史由客户端序列化到动态 payload；proxy 会话只绑定提示词，不代为累计历史。模型路由由服务端决定，TL wire 中不新增 model/provider 字段，不借用 `prompt_variables.name` 或正文选模型。

内网认证在 transport 层单独配置（已授权的 header/token/TLS 配置），只发给目标网关；上游公网 key 仅在 proxy 服务端。默认验证 TLS，不为方便联调关闭校验，不发明公司 SSO 协议。

HTTP 失败与 `{error:...}`、非零业务 code、SSE error、EOF 和超时分开记录，包含本地 attempt、requestId、sessionId、阶段与安全错误摘要。内网模式默认只记录脱敏元数据；本地 proxy 的默认完整 debug 报文策略不自动套用到公司数据。

HTTP/流式 transport 默认不自动重放模型调用；取消向上传播。工具正文语法纠错与项目外层 retry/fallback 的相互作用见接入设计，必须控制实际调用次数。

## 资源预算的计量与两端配合

这些是客户端实现策略，不是增加 TL wire 字段；不得仅因为双方都写 4 MiB 就假定在数同一对象。

| 客户端配置 | 默认 | 精确定义 |
| --- | --- | --- |
| max_request_bytes | 1048576 | 每个 init/chat 序列化后的完整 HTTP JSON body 的 UTF-8 字节数，分别检查，包含转义和信封 |
| max_response_bytes | 4194304 | 解码后的模型正文 UTF-8 字节数；SSE 为所有 chunk.content 拼接值，非流式为 data.txt，不能按字符串字符数 |
| max_wire_response_bytes | 67108864 | 每个响应实际读取的 HTTP body 字节数，含 SSE framing/JSON 转义/心跳，不含 HTTP header；防止无正文无限 control 帧 |
| max_sse_event_bytes | 1048576 | 单个 TL SSE 事件的原始 UTF-8 字节上限，独立于正文总量 |
| timeout_seconds | 150 | 每个 attempt 从 init 开始至 chat 完成/失败的绝对预算，含两个 HTTP 调用；不是每次 read 都重置的 httpx inactivity timeout |
| stream_idle_timeout_seconds | 0 | 0 表示禁用额外字节空闲计时；仍受绝对预算约束。非零是部署显式选择，不能误把长思考无 TL 字节当成功完成 |

proxy 上游默认总预算 120 秒，客户端默认 150 秒为 init、网络和关闭留余量。至多一次语法纠错使用第二个独立 attempt；从首次 init 起最多两个 attempt 预算，外部请求取消或更短 deadline 随时生效。限流排队使用现有 acquire_timeout，不能无限等待；报告模型 attempt 耗时与排队耗时分别计量。

正文中引号、反斜杠、换行、多字节中文以及很多小 SSE 帧会让 wire 字节与正文大小明显不同。分别计数，任一超限明确失败，绝不能截断后发成功 done/工具调用。非流式用增量读取限制 body 后再解析 JSON，不能先无限制 response.json()。可按部署下调预算，但 client/proxy 的生效口径要一起验收。
