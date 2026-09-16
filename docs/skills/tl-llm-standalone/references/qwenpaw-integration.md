# QwenPaw 接入设计与实施边界

设计基线：QwenPaw revision `3298ddf504976606d8c1600ad785f415206bbee3`，2026-09-13；`pyproject.toml` 锁定 `agentscope[model-ollama]==2.0.7.post1`，已有 httpx、jsonschema。以下文件与符号来自本仓库只读检查；新增项尚未实现。当前解释器未安装可直接运行的完整 AgentScope 环境，另核对了 uv 缓存中同版本 SDK 源码与 METADATA，不能把源码检查记为运行测试通过。

## 选定架构

新增专用 `TLProvider` + `TLChatModel` + `TLChatFormatter`，固定本地 `tool_calling_mode="system_prompt"`。两个可替换 endpoint 共用这套代码：公司 TL 网关，或模拟该网关的 TS proxy。不要继承 OpenAI HTTP 请求构造/探测逻辑，不通过现有 OpenAI-compatible provider 加 base_url 冒充 TL。

| 新组件（建议路径） | 职责及不变量 |
| --- | --- |
| `src/qwenpaw/providers/tl_provider.py` | Provider 配置、模型路由标签、检查方法、返回本地 TLChatModel；不构造 OpenAI SDK client |
| `src/qwenpaw/providers/tl_chat_model.py` | AgentScope adapter；取得 formatter 结果与 tools，编译 prompt、调用 transport、形成 ChatResponse/StructuredResponse；不执行工具 |
| `src/qwenpaw/providers/tl_formatter.py` | 真实 FormatterBase；接收 Msg，返回规范化 `list[dict]` 历史记录；保留工具 ID 和结果类型，在 model_factory 中注册跳过通用文件 wrapper 的分支 |
| `src/qwenpaw/providers/tl_prompt_codec.py` | 纯函数：系统/数据分离、tools schema 注入、选择约束、严格输出及参数校验；不联网、不调用 Toolkit |
| `src/qwenpaw/providers/tl_transport.py` | 异步 init/chat、JSON/SSE、关闭/取消、大小/超时、阶段错误；不理解工具正文 |

工具模式的模型边界收工具 schema、返回本地 ToolCallBlock；整个系统仍用 QwenPaw 原工具执行链。原 `OpenAIChatModelCompat` 会从 text/thinking 中猜 `<tool_call>` 并补本地调用，不把它或 `local_models/tag_parser.py` 用作新严格 codec，避免普通正文被误执行。

## 当前入口及需改动位置

路径均相对 QwenPaw 根；行号用于基线定位，实施前按符号核对。

| 当前文件/符号 | 接入要求 |
| --- | --- |
| `providers/provider.py:317` ProviderInfo、`:521` Provider、`:1178` get_info | 定义可返回、保存、更新的 `tl_config`；防止基类投影时丢失。保留既有凭证掩码/加密能力 |
| `providers/provider.py:754` get_chat_model_cls、`:1151` get_chat_model_instance | TLProvider 显式返回本地 TLChatModel 类和实例；不能在 agentscope.model 里查不存在的 TLChatModel |
| `providers/provider_manager_persistence.py:840` _provider_from_data | 在默认 OpenAI 回落前增加 `chat_model == "TLChatModel"` 分支，保存后重载仍为 TLProvider |
| `providers/provider_manager.py:455` add_custom_provider、`:197` update_provider | 以自定义协议类型支持多个 TL endpoint 实例；创建、更新和重载均保留 tl_config。无需制造 DeepSeek/Qwen 两套实现 |
| `providers/provider_update_fields.py` CONNECTION_CONFIG_FIELDS 及快照更新选择 | tl_config 改变应失效已有连接/可用性证据；更新 allowlist、投影与加密快照往返，不能仅新增 Pydantic 字段 |
| `providers/provider_discovery_policy.py:20` CustomChatModelName / CUSTOM_DISCOVERY_POLICIES | 加 TLChatModel，自动模型发现禁用；不能默认调用 /models 或保留自动发现开关 |
| `app/routers/providers.py:51` ChatModelName、`:165` CreateCustomProviderRequest 和配置路由 | 新协议类型、tl_config 创建/更新 schema 与返回字段一致；保持用户/agent 所属范围，不使用进程全局 TL 凭证 |
| `agents/model_factory.py:2187` create_model_and_formatter、`:2332` _create_formatter_instance | 模型自带真实 TLChatFormatter；显式跳过通用 file-support wrapper，主/后选模型一致；原始媒体在清理前预检 |
| `token_usage/model_wrapper.py:220` generate_structured_output、`:229` __call__ | 正常 tools 参数仅供本地 codec；auto 被规范化为 None；旧 structured_model kwarg 会被移除；TL count_tokens 必须穿过外层包装接通，见输入编译规格 |
| `agents/routing_chat_model.py:90` __call__ | 同样移除旧 structured_model；验证 agent/main/subagent 路由都到 TL，不新增独立旁路 |
| `providers/model_error_policy.py:65` classify_model_error、retry_chat_model / fallback_chat_model | 添加 TL 类型错误的显式判定，遵守下述有限重试和失败路由；不靠错误正文关键词猜阶段 |
| `console/src/api/types/provider.ts`、`api/modules/provider.ts`、`pages/Settings/Models/components/modals/CustomProviderModal.tsx` / ProviderConfigModal | 增加 TL 协议选项与 tl_config 表单/请求字段；只显示该协议能兑现的功能；补对应现有本地化条目 |

表中省略的 `providers/`、`agents/` 等前缀均位于 `src/qwenpaw/`。采用自定义 provider 类型作为 v1 入口，不需把带公司地址的 TL 实例硬编码进 `provider_catalog.py`。如果以后需要内置模板，可复用同类，但不添加真实 endpoint/key。

## 配置契约（新增设计，不是现有可导入配置）

```json
{
  "id":"tl-qwen-local",
  "name":"TL Qwen local",
  "chat_model":"TLChatModel",
  "base_url":"http://127.0.0.1:8089",
  "api_key":"",
  "generate_kwargs":{},
  "tl_config":{
    "app_id":"internal-demo",
    "tr_code":"agent-chat",
    "tr_version":"1.0",
    "system_prompt_variable_name":"system_prompt",
    "tool_calling_mode":"system_prompt",
    "json_correction_max_attempts":1,
    "timeout_seconds":150,
    "stream_idle_timeout_seconds":0,
    "max_request_bytes":1048576,
    "max_response_bytes":4194304,
    "max_wire_response_bytes":67108864,
    "max_sse_event_bytes":1048576
  },
  "models":[{
    "id":"qwen3.8-flash",
    "name":"qwen3.8-flash via local TL route",
    "supports_multimodal":false,
    "supports_image":false,
    "supports_video":false
  }]
}
```

映射 `app_id/tr_code/tr_version` 到 wire camelCase；其余本地控制项均不发送。验证别名非空、预算为正、纠错次数仅 0/1。`custom_headers`/现有凭证系统承担实际公司 HTTP 认证，不在 tl_config 复制 secret；Bearer 的 api_key 是目标 TL 网关凭证，绝非 DeepSeek/Qwen 公网 key。

一个 endpoint 实例绑定一个已知服务端路由，v1 只允许一个启用模型标签。DeepSeek 联调另建 `tl-deepseek-local` 指向 8090；公司直连另建 `tl-internal`，模型标签使用已知部署名称或本地 `internal-default`。本地 model ID 用于选择/显示和上下文预算，**不进入 TL 报文且不能保证改变服务端模型**。创建与更新拒绝会造成假切换的多个启用模型标签。

禁用自动发现和多模态探测；所有 TL 模型媒体能力明确 false。`fetch_models` 返回配置列表且不联网；`check_connection` 可用新 init 验证 TL 路径，仍返回 tuple，由路由明确投影 verification=provider_only；`check_model_connection` 用无工具的最小 init→chat，完整成功终止才算 live。404 在 TL 可能是路径/session 错误，不能直接解释为模型不存在。

首版 `generate_kwargs` 只允许空对象；`stream` 和超时由 adapter 本地控制。temperature/max_tokens/thinking/response_format、tools 等尝试覆盖都在配置更新与运行时边界明确拒绝，不能静默忽略或透传。上下文/输出上限 metadata 是已配置服务端能力或保守本地预算，不是每请求生成参数；不要凭公网模型目录就宣称公司路由具备相同能力。代理上游参数只在 proxy 服务端设置。

## AgentScope 2.0.7.post1 适配细节

同版本 SDK `model/_base.py` 的 `__call__(messages, tools, tool_choice, **kwargs)` 调 `_call_api(model_name, messages, tools, tool_choice, **kwargs)`；模型在 `_call_api` 内调用 `self.formatter.format(messages)`。不能因为 QwenPaw wrapper 的类型注解是 list[dict] 就假定传入的已经是 wire；应实测实际 Msg 流并只格式化一次。formatter 返回的规范化记录再交 codec 编译。

SDK `tool.ToolChoice` 使用 `mode` 和可选 `tools` 白名单。适配时支持该对象，以及项目 wrapper 的 None/auto/none/required/指定工具名字符串；白名单在 schema 注入前应用并校验。不得把本地对象直接 JSON 化成 API 参数。

`ChatResponse` 需要 `content` 与 `is_last`。实际使用该版本 `TextBlock` / `ToolCallBlock` 类型构造：`message/_block.py` 的 ToolCallBlock.type 固定为 `tool_call`，input 必须是 JSON 字符串。先校验正文 arguments 对象再序列化一次给 input；旧版 tool_use/input对象不是当前输出接口。往返 fixture 的 local_response_blocks 展示 SDK 字段，而 chat.txt 内的 arguments 对象是 formatter 规范化数据。

- 非流式返回完整 `ChatResponse(..., is_last=True)`。
- 文字流返回增量 A、B（各 `is_last=False`），最后完整 AB（`is_last=True`），同一 response/block ID；不把累计 A、AB 再交累加器造成 AAB。
- 工具模式始终完整缓存正文；TL 成功终止且全量校验后，stream=True 先发一次携带完整 typed blocks 的 `is_last=False` 响应用于事件转换，再发同 ID/同内容的 `is_last=True` 最终快照。校验前向 AgentScope 零输出；临时预览只走独立的可清除事件，不发送未校验的 TextBlock/ToolCallBlock。非流式直接返回完整响应。
- 本地工具 ID 成功解析后生成，所有封装层/事件保持不变。usage 不在固定 TL wire 中，保留 None/未知，不虚构精确消耗；估计值必须明确标为估计。
- SDK 基类会把 CancelledError 转为 interrupted 响应。TLChatModel 应显式控制其调用/流式包装，使取消向上传播并关闭 transport，不把未校验缓冲转换成成功工具结果；避免重复格式化、累积或设置 is_last。

结构化输出采用 2.0 的独立 `generate_structured_output(messages, structured_model, **kwargs)`，返回 `StructuredResponse(content=validated_object)`，不在普通 __call__ 上依赖旧 structured_model，也不塞入 ChatResponse.metadata 冒充结果。覆盖默认 forced/auto/no_think 策略循环：工具能力不能通过原生特性或多策略重试获得。直接编译目标 schema 到 system、传输、严格校验，复用同一个最多一次语法纠错预算。不要执行 SDK 内部用于结构输出的伪工具。若普通 ReAct 目录中真实出现框架临时输出工具，则仍按其 schema 当本地工具请求处理。

流事件、30 秒 watchdog、通用 formatter、Hint/媒体预处理和连接测试的具体接法必须同时遵循 [运行时兼容边界](runtime-compatibility.md)。特别注意：仅有最终快照不生成流式工具事件；仅声明 TL 异常不可重试不能覆盖 wrapper 自身超时。

输入取值必须在 model-call middleware 之后，按每次调用冻结 system、历史与工具；计数覆盖编译后文本并穿过包装器，见 [输入编译与上下文预算](prompt-compilation.md)。工具模式实时显示需要新增临时事件和前端状态，不能只设置 stream=True，见 [实时预览](streaming-preview.md)。

## 错误、重试与工具执行

错误带明确阶段：configuration / prompt_encode / init_session / chat_http / sse_decode / response_parse / tool_lookup / tool_args_validation。工具实际执行失败由现有 executor 记录，不伪装成 provider 解析错误。

Transport 与 SDK 原生重试为 0，语法纠错仅由 codec/model invocation 预算控制。v1 以 TL 专属 RetryConfig(enabled=False) 和本地 watchdog 覆盖关闭外层重放，再用 TL 错误分类禁止跨模型路由（正常速率限制仍保留）；这样报文截断或纠错第二次 HTTP 失败不会被套娃重试。扩展共享错误分类时先识别 TL 错误，令 retryable=false、fallback_eligible=false，保留实际 HTTP status 与阶段供显示。普通 provider 的现有错误规则不改变。CancelledError 不包成普通 TL 错误；本地 context overflow 沿用已有一次 context recovery 路径，只在发送前检测时触发。

`model_factory` 已设置 model.max_retries=0 并统一包 token/retry/fallback；不要另加 OpenAI SDK 或 httpx 自动 retry。测试包括 TL 作为 fallback 列表里的备选模型时也不会因 `index>0` 的既有规则绕过终止约束；如需调整，应只针对 TL 错误增加短路，不能只改共享分类后假定覆盖全部路径。

`runtime/builder.py` 构建受治理的 Toolkit；`agents/react_agent.py:809` 的 `_execute_tool_call` 是顺序/并发执行入口，并会做既有参数 coercion 后交 AgentScope；工具协调见 `tool_calls/_middleware.py` 和 `_coordinator.py`。新 codec 在更早的模型边界严格校验，不通过该 coercion 自动补救错误参数，不绕开 guard/审批。全部校验通过也不代表动作已获执行许可。

## 实施顺序与测试入口

1. 独立纯 codec、formatter 与 transport；使用本技能 schema/fixtures 与 fake TL，先闭合 wire 和工具关联。
2. TLProvider/TLChatModel 适配真实锁定 SDK；确认 block 类型、ToolChoice、增量/最终响应、取消和 StructuredResponse。
3. 配置类型、持久化、错误分类和 API/console 入口一起接入；保存/重载/激活/检查往返通过后再联调 agent。
4. 单所有者执行完整工具回路，复用现有 guard/coordinator，无副作用 add 工具只执行一次。最后接 proxy 和真实模型。

可复用现有测试风格：`tests/unit/providers/test_provider_manager.py`（持久化/激活）、`test_openai_provider.py`（mock HTTP/关闭）、`test_openai_stream_toolcall_compat.py`（流式最终响应）、`tests/unit/utils/test_model_response.py`（最终文本提取）、`tests/unit/agents/test_react_agent_tool_coercion.py`（执行入口）。复用测试结构，不继承旧 XML/JSON 宽松解析语义。

建议新增 `tests/unit/providers/test_tl_transport.py`、`test_tl_prompt_codec.py`、`test_tl_provider.py` 及一个工具回路 integration 测试；UI 扩展对应 modal/API tests。先跑相关窄测试，再跑受到注册/formatter/retry 修改影响的现有测试。离线通过、proxy 端到端通过、公司接口通过各自记录，不互相替代。
