# 当前 QwenPaw 运行时兼容边界

本规格根据 QwenPaw `3298ddf504976606d8c1600ad785f415206bbee3` 与锁定的 AgentScope `2.0.7.post1` 源码校对。这里的适配均发生在客户端/项目内部；不更改公司 chatbbc 报文，也不开启原生 tools。尚未实现或进行完整运行时联调。

## 流式结果必须同时满足模型与事件消费方

SDK `agent/_agent.py:1545-1569` 的 `_reasoning_impl` 对 async generator 分两路：`is_last=False` 的内容转换为 text/tool start/delta 事件；`is_last=True` 只保存最终完整响应。随后以最终响应更新上下文与决定工具执行。

因此工具模式不能只 yield 一次 final 快照：虽然最终工具内容能入上下文，但工具 start/delta/end 事件不会完整出现；final 正文的显示也可能缺少对应 text 事件。正确顺序：

1. 持续读取 TL 并保留全部内容；成功终止、严格解析、整批参数校验之前，向 AgentScope **零模型 block 输出**。这是提交边界，不禁止独立界面预览；预览事件必须遵循 [实时预览](streaming-preview.md)，不能混入以下 ChatResponse 流。
2. 成功后构造本地 typed blocks，一次性 yield `ChatResponse(content=blocks, is_last=False)`；这不是提前执行或未完成参数，而是已经校验完成的整批内容。
3. yield `ChatResponse(content=blocks_copy, is_last=True)`，相同 response ID、相同 block/tool ID、相同完整内容；两个响应对象分别构造，避免历史引用被随后修改。
4. 结束流。AgentScope 只把第一份转换为事件，第二份作为权威最终状态；工具执行一次。也适用于工具协议里的 `type=final` 回答。

非流式可直接返回一个 `is_last=True` 的 ChatResponse，SDK 对非流式对象另有事件转换分支。普通文字流维持增量 A、B，最终 AB；不能把完整 AB 再作为非 final 增量重复发送。

源码分支最小复现结果：相同工具内容，`[is_last=True]` 转换事件次数为 0，`[False, True]` 为 1。该复现只执行实际 SDK 的分发分支，不代表 Agent/Toolkit 端到端测试通过。实现验收必须检查完整 start/delta/end 序列、最终上下文及实际工具执行次数。

## 外层超时和重试不能只靠 TL 异常类型控制

`src/qwenpaw/providers/retry_chat_model.py:529-551` 默认首内容与内容空闲超时均为 30 秒；空 control chunk 不延长预算。工具全文缓冲、上游仅返回 reasoning、首个可见 content 很晚的模型都可能超过 30 秒。该层自己抛出的 StreamIdleTimeoutError 并非 TL transport 错误，原来的 TL 异常分类约定不能覆盖它。

在主模型和 fallback 候选的构造路径都采用 TL 专属 wrapper 配置：

```python
RetryChatModel(
    recorded_tl_model,
    retry_config=RetryConfig(enabled=False),
    rate_limit_config=existing_rate_limit_config,
    stream_first_content_timeout=0,
    stream_idle_timeout=0,
)
```

这些参数只影响当前 TL 实例，不能更改全局环境变量或其他 provider。`enabled=False` 才关闭重试：现有 `_normalize_retry_config` 会把 `max_retries=0` 调整为至少 1。正常并发槽、QPM 与 acquire timeout 保留；缓冲期间占用请求槽是有意的资源限制。

被关闭的是 wrapper 的**内容** watchdog；TL transport 必须提供单次 attempt 绝对 deadline、响应与单事件上限，并传播取消。客户端字节空闲计时可配置，但默认关闭：公司接口/透明 proxy 可能在长时间思考时不发 TL 字节；不能把这误判为上游停滞。proxy 上游侧仍按实际 SSE 字节（包括 reasoning）执行其空闲计时。不得靠伪 text、占位工具或空 chunk“保活”。真正的网络停滞仍有限期失败。

还需按协议类型跳过 RetryChatModel 的两条 reasoning_content 自适应补齐/重放路径（同步异常与流异常，含已有 capability cache）；它们在常规重试预算之外运行。TLChatFormatter 不声明 `_qwenpaw_supports_reasoning_content_fallback`，且历史只走 Msg→TL codec，不触发已格式化 dict 的原生 reasoning 注入。

保持 TL typed errors 的 `retryable=false/fallback_eligible=false`，并在 FallbackChatModel 的候选失败路径短路 TL 错误，覆盖其现有 `index>0` 放行规则。SDK Agent ModelConfig 在该锁定版本默认 `max_retries=0, fallback_model=None`，继续维持，不额外启用 SDK 的第二层 fallback。一个逻辑调用最多一次初始 attempt 加一次符合条件的语法纠错。

## Formatter 必须在通用 wire 转换前获得消息

`model_factory.py:_create_formatter_instance` 目前总把原生 formatter 包进文件支持 wrapper。该 wrapper 默认把未知 family 按 OpenAI 规范化，并在 `:1669-1697` 准备文件/远程媒体；不能只让 TLChatFormatter 继承 FormatterBase 就假定绕过这些步骤。

选定接法：TL 模型使用实际 TLChatFormatter；在 `_install_model_formatter` / `_create_formatter_instance` 为该类型返回原生 TL formatter，**不进入**通用 FileBlockSupportFormatter。主模型和 fallback 模型都走同一分支。TL formatter 实现异步 `format(list[Msg]) -> list[dict]`，直接遍历 typed blocks，不调用 OpenAI formatter 或 FormatterBase 的媒体降级 helpers，不读取文件或 URL。

QwenPaw 的上游生命周期仍会清理上下文：`react_agent.py:243-260` 在 reasoning 前 sanitize，`agents/utils/tool_message_utils.py` 会移除无效/重复/不配对项。因此“原始损坏历史必须由 TL 报错”不是现有 agent 回路的可兑现保证。分别验收：

- 集成路径接收已清理的实际 Msg，保留剩余顺序/工具关联；不恢复已删除的调用或结果，不第二次破坏性清理，不修改存储的 Msg。
- 独立 formatter/codec 入口遇到仍存在的非法关联或重复调用 ID，明确报错。完整的同消息 call/result 配对先按 block 顺序分析，不能只检查 Msg.role 或要求结果另占一条消息。
- 历史清理与新输出解析是不同层。新生成 calls 的结构、工具名、参数仍严格校验，不因已有 sanitizer 而放宽。

## 媒体的明确边界

现有文本模型预处理会删除媒体；混合 text+media 时可能只留下文字，未必新增占位。要实现本 skill 的“原始媒体显式不支持”，应在 QwenPaw `_reasoning` 的 proactive media stripping **之前**，对当前 TL 主模型的可见输入进行只读预检；检查顶层 DataBlock、HintBlock 和 ToolResultBlock 内的媒体。发现即返回明确不支持，不能先调用清理器再检测。TL 作为后选模型时，在 TLChatFormatter 入口执行同样预检，避免通用 wrapper 下载或提升媒体。

若加载的历史已经只有明确的媒体不支持占位文本，保留为普通文本；不反向打开原文件，不宣称完成了原附件校验。此前已删除的媒体无法由 provider 还原。此规则不修改其他模型的媒体行为，也不扩展 TL files 字段。

## 配置与连接测试需要真实往返

- `TLProvider.update_config` 要验证并更新嵌套 tl_config；同时修改 router 构造的 config dict、快照更新字段及 get_info 投影，不能只有 Pydantic 声明。
- `TestProviderRequest` 和前端测试 payload 也要带当前未保存的 tl_config。现有 `test_provider` 只复制已列出的字段；不补这里，用户测试的会是旧 appId/变量名。
- 临时测试配置应合并后用 TLProvider.model_validate 校验；现有 model_copy(update=...) 不执行字段验证。不能把新的 chat_model 字符串写入旧 OpenAIProvider 实例后宣称已切换类型。
- v1 的 TL 实例协议类型固定；现有 provider 跨 TL/其他协议切换提示新建自定义实例。创建使用 `_provider_from_data` 的显式 TL 分支；同类型 endpoint/变量更新照常允许。
- `check_connection` 仍遵守 `(bool, str)` 接口，只测 init；路由为 TL 明确返回已有 TestConnectionResponse.verification=`provider_only`，并保留“仅会话初始化，未测 chat”的成功消息。当前路由会统一写 Connection successful，需要补这个投影。
- `check_model_connection` 另做完整 init→chat，返回 ModelConnectionResult(verification=`live`)。成功仅证明配置路由可调用；TL 没有回传模型身份，不能据此证明公网模型品牌或版本。
