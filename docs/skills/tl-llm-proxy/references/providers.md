# 模型接入与待验证项

Qwen 模型为用户指定的 `qwen3.8-flash`。DeepSeek 精确 ID、服务商及 endpoint 通过服务端配置确定；缺少真实账号不阻止 fake provider 测试。

## 请求基线

- model、messages、stream 由已验证 TL 请求和服务端配置生成，base URL 正确保留路径前缀并只追加一次 /chat/completions。
- 不发送 response_format，不强制 json_object/json_schema，不因提示词包含 JSON 就自动更改 API 参数。
- 不发送 tools、tool_choice、functions、function_call、parallel_tool_calls。组织内部未开放原生 tool_calls，必须通过提示词及普通正文测试工具语义。
- stream:true（含缺省）实时处理 delta.content；stream:false 返回 message.content。输出正文可以是任意提示词约定的文本格式。
- 请求使用 Content-Type:application/json 及服务端 Bearer key；此 JSON 是 API 外层，不是对模型正文的限制。
- provider 不支持流式或配置参数时明确失败，不静默改为非流式、换模型或再生成。
- 不把 reasoning_content、usage 或原生 tool_calls 混入可见正文。默认 debug 会记录实际上游事件；日志不代表这些字段可在 TL 响应中使用。

## Qwen

使用 OpenAI 兼容 HTTP API、qwen3.8-flash 和用户账号对应的地域/工作空间 endpoint。截至 2026-09-13，官方模型页明确给出 `qwen3.8-flash` 调用 ID；账号可用性仍需真实联调。[模型页](https://help.aliyun.com/zh/model-studio/qwen3-8-flash)。[官方 Chat API](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)

base URL 与 key 的地域要匹配，使用控制台给出的实际 endpoint；旧开发代理的公共地址和 Origin:http://localhost 不是目标测试环境的固有要求。[官方兼容接口说明](https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope)

思考参数按精确模型显式配置，默认 provider-default。当前 Qwen 混合思考使用 `enable_thinking` 布尔值；不要把 DeepSeek 官方的 `thinking` 对象发给 Qwen。[Qwen 思考说明](https://help.aliyun.com/zh/model-studio/deep-thinking)REST 顶层参数与 SDK extra_body 的包装不同，不能误把 SDK 包装直接发给 HTTP API。不因希望输出工具样式文本就启用原生 tools。以后若加 assistant 历史，再验证 preserve_thinking 等历史规则；当前只发送 system + user。

## DeepSeek

使用服务端配置的模型，通过 /chat/completions 调用；官方 API base 可以是 https://api.deepseek.com，第三方托管则采用其明确契约。模型参数需按精确 ID 验证，不能把 Qwen 的 enable_thinking 原样套用。[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)

截至 2026-09-13，DeepSeek 官方文档示例使用 `deepseek-v4-flash` / `deepseek-v4-pro`；这只是可配置示例，不替用户决定账号路由。官方 V4 的 `thinking` 使用 `{"type":"enabled"}` / `{"type":"disabled"}`，默认不发送该字段即可沿用服务端默认。第三方托管 DeepSeek 的参数按其实际 endpoint 契约另建 profile，不能照搬官方映射。

> 2026-09-18 实测 `GET https://api.deepseek.com/models` 仅返回 `deepseek-flash` 与 `deepseek-v4-pro`，`deepseek-v4-flash` 已不可用。配置以账号实际返回为准。

旧 DsProxy 的 JSON 提示注入、name 选模型和角色文本解析不作为统一门面的默认行为。即使 provider 支持 JSON Output/原生工具，这个模拟器也不默认启用；输出格式靠提示词，工具含义由客户端处理。

## 联调记录

记录 provider、model、endpoint 地域/工作空间、日期、思考设置、输出上限、实时结果和使用的合成提示词；不记录 key 的真实值。公开资料核对日期为 2026-09-13，不代表用户账号联调通过。

至少测试四种提示词：普通文字回答、Markdown 列表、XML 文本、JSON 文本；再测试把工具操作描述为普通正文的提示词。确认请求没有 response_format 或原生工具控制字段、正文不被改写、首增量早于上游完成、默认四向 debug 报文可追踪。

真实 API 测试放 test:live，默认 npm test 使用 fake upstream。若经共享测试入口，重复首增量时序验证。报文日志中的可见模型内容与工具样式文本用于检查，不由代理执行。

## 服务端配置示例（待实际账号补齐）

以下为未来 proxy 的配置，不是 QwenPaw provider 配置。`UPSTREAM_BASE_URL` 指向 OpenAI 兼容根地址；适配器只追加一次 `/chat/completions`。环境变量的 key 值不进入仓库。

```dotenv
# Qwen 实例：QwenPaw TL endpoint 指向 http://127.0.0.1:8089
TL_PROXY_PORT=8089
UPSTREAM_PROVIDER=qwen
UPSTREAM_MODEL=qwen3.8-flash
UPSTREAM_BASE_URL=<账号控制台给出的地域及工作空间兼容根地址>
UPSTREAM_API_KEY=<从本机环境注入>
UPSTREAM_THINKING=provider-default
LOG_LEVEL=debug
```

```dotenv
# DeepSeek 官方示例实例：QwenPaw TL endpoint 指向 http://127.0.0.1:8090
TL_PROXY_PORT=8090
UPSTREAM_PROVIDER=deepseek
UPSTREAM_MODEL=deepseek-flash
UPSTREAM_BASE_URL=https://api.deepseek.com
UPSTREAM_API_KEY=<从本机环境注入>
UPSTREAM_THINKING=provider-default
LOG_LEVEL=debug
```

内部统一 `UPSTREAM_THINKING` 仅接受 provider-default/enabled/disabled；前者不发字段，其余由已确认 adapter 映射。增添其他模型时复用透明文本 transport，仅增加精确服务商/模型参数与终止 profile，不改变 TL 协议或开启原生 tools。
