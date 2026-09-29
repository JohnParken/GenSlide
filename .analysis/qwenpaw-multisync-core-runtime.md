> ⚠️ **基线错误警示（后续已修正）**：本报告分析的是本地 `multisync` 分支 = `origin/main` + 4 个本地提交，
> **不是 QwenPaw 官方代码**。其中 TL provider、`server/`、`attachments.py` 等属本地改造。
> 请以 `qwenpaw-main-core-runtime.md`（官方 main 基线）为准，本文件仅保留作差异审计用。

# QwenPaw 核心运行时与 Agent 执行架构分析

> 分析对象：`/Users/yangxuezhen/git/QwenPaw`（只读），源码 `src/qwenpaw/`，约 34.7 万行 Python（1011 个 `.py`）。
> 关键前提：**ReAct 主循环本身不在 QwenPaw 里**，而在外部依赖 `agentscope==2.0.7.post1`（`pyproject.toml` 的 `dependencies`）。QwenPaw 是 AgentScope 2.0 之上的"产品化外壳 + 治理层"。

---

## 0. 分层总览

```
L6 入口       cli/  app/  server/  pawapp/  tauri/          HTTP·SSE·CLI·多用户服务
L5 集成扩展   harnesses/  plugins/  drivers/  acp/          Codex/Qoder 适配·插件·外部能力(MCP)
L4 请求编排   runtime/  modes/  hooks/  loop/               8 阶段生命周期·模式·停止门
L3 Agent 本体 agents/                                       ReActAgent·tools·context·memory·skills
L2 执行与治理 tool_calls/  governance/  security/  sandbox/ 工具生命周期·策略·审计·沙箱
L1 基础设施   providers/  token_usage/  observability/  utils/  模型抽象·计量·追踪
L0 基座       config/  constant.py  exceptions.py  schemas.py  配置/常量/异常/DTO
```

依赖方向基本是单向的（L6→L0），L2/L3 之间有少量双向引用（见 §9.2）。

---

## 1. Agent 的定义与注册

**Agent 不是声明式的"YAML 定义体"，而是"配置 + 模板 + 代码类"三层组合。**

- **声明式配置**：`config/config.py:2217 class AgentProfileConfig(BaseModel)` —— 每个 agent 一个 `workspace/agent.json`。字段包括 `id/name/backend/template_id`、`running: AgentsRunningConfig`、`llm_routing`、`active_model`、`fallback_models`、`subagent_model`、`thinking_level`、`approval_level`（`STRICT/SMART/AUTO/OFF`）、`system_prompt_files`（默认 `["AGENTS.md","SOUL.md","PROFILE.md"]`）、`tools: ToolsConfig`、`security: SecurityConfig`、`heartbeat`、`mcp`、`channels`、`coding_mode`、`plan`。全局索引在 `config/config.py:2355 class AgentsConfig`（只存 `profiles: Dict[str, AgentProfileRef]` 与 workspace 路径，root `config.json` 不膨胀）。
- **模板**：`agents/templates.py` 定义 `SUPPORTED_AGENT_TEMPLATES = ("default","local","qa")`，由 `build_agent_template()` 产出 `AgentTemplateBuildResult`（含 `initial_skill_names`、`md_template_id`）。三个内置 agent 的差异主要是工具预置：`config/config.py:2791 build_qa_agent_tools_config()`（只开 5 个工具）与 `:2813 build_local_agent_tools_config()`（小模型 + 多 agent 协作工具）。
- **运行时类**：`agents/react_agent.py:158 class QwenPawAgent(CodingModeMixin, Agent)`，继承 AgentScope 的 `Agent`（`agentscope/agent/_agent.py:112`）。其 `__init__` 明确"所有依赖由外部注入，agent 不自己构造任何东西"，实际构造者见下条。
- **注册**：`Agent` 实例**不注册**；注册的是"每个 workspace 的插件容器"。`app/workspace_registry.py:43` 调用 `workspace.bootstrap_plugins(**kwargs)`（`app/workspace/workspace.py:288`），kwargs 由 `app/workspace/bootstrap_factory.py: WorkspaceBootstrapFactory.build_bootstrap_kwargs()` 组装，注入 `builtin_tool_funcs / builtin_hook_clses / builtin_mode_clses / builtin_contributor_clses / builtin_command_specs`。多 agent 生命周期由 `app/multi_agent_manager.py:36 MultiAgentManager` 管理（懒加载、热重载 `reload_agent`、`startup_status`）。
- **提示词注册**：`runtime/prompt_manager.py:26 PromptContributor` + `:58 PromptManager`（按 `priority` 拼装，`PROMPT_SEPARATOR="\n\n"`）；内置 9 个贡献者在 `runtime/prompt_contributors.py:492 _ALL_CONTRIBUTORS`，其中一段是硬编码的"受保护执行契约" `runtime/protected_prompt.py: PROTECTED_EXECUTION_CONTRACT_PROMPT`（用户不可覆盖）。

---

## 2. 执行循环：一次用户请求的完整路径

**入口 → 编排 → 构建 → 执行：**

1. **入口**：`app/channels/*` / `app/routers/*` → `app/workspace/workspace.py:414 Workspace.stream_query(request)`。若 `agent.json` 的 `backend != "qwenpaw"`，转交 `harnesses/runtime.py: HarnessRuntime.stream()`（外部 Codex/Qoder 进程）；否则 `Runtime(workspace=self).run(request)`。
2. **8 阶段编排**：`runtime/runtime.py:34 class Runtime.run()`，阶段枚举见 `runtime/phases.py: Phase`：
   `PRE_DISPATCH → [固定步骤1: slash 命令分发] → POST_DISPATCH → PRE_AGENT_BUILD → [固定步骤2: AgentBuilder.build] → POST_AGENT_BUILD → PRE_EXECUTE → [固定步骤3: AgentExecutor.run] → POST_RESPONSE → ON_ERROR → FINALLY`。
   每个阶段由 `runtime/hooks.py: HookRegistry.run(phase, ctx)` 执行拓扑排序后的 hook；三种返回语义 `HookAction.CONTINUE / SHORT_CIRCUIT / SKIP_AGENT`（`runtime/hooks.py:44`）。hook 之间用 `before/after/priority` 声明顺序，`_topo_sort()` 检测环并抛 `HookCycleError`。
3. **Agent 构建**：`runtime/builder.py:104 class AgentBuilder`（1450 行）。`build_toolkit()`（:118）取工具、按四维过滤、排序、绑定 skill；`build()`（:326）加载 config → 建 model → 建 prompt → 建 middlewares → `QwenPawAgent(...)`（:549）→ `agent.load_state_dict(ctx.session_state)`。
4. **执行**：`runtime/executor.py: AgentExecutor.run()` 驱动 `agent.reply_stream(inputs=msgs)`，用 `_iter_with_heartbeat` 包心跳，把 AgentScope 事件交给 `runtime/envelope.py: Envelope`（SSE 状态机）翻译成前端事件；TL provider 走 preview 队列双通道（避免长 JSON 缓冲时前端静默）。
5. **真正的 ReAct 循环在 AgentScope**：`agentscope/agent/_agent.py:915 _reply_impl()` 是主循环，决策函数 `:3248 _next_action()` 是纯函数状态机，返回 `Reasoning | Acting | Exit` 三态；`_reasoning_impl`(:1497) 调模型，`_acting_impl`(:2580) 执行工具，`_batch_tool_calls`(:1903)/`_execute_sequential_tool_calls`(:1943)/`_execute_concurrent_tool_calls`(:1993) 决定串行/并行。迭代上限由 `ReActConfig(max_iters=...)` 控制（`runtime/builder.py:554`，AgentScope 默认 50）。
6. **QwenPaw 的差异化改造点**：`QwenPawAgent` 重写 `_save_to_context`(:306) 以写穿到 ContextManager、重写 `state_dict/load_state_dict`(:321/:334) 做会话序列化与 1.x 兼容、重写 `_reply`(:1272) 作为扩展点；终止条件通过 `_run_stop_handlers`(:1357) 挂到 AgentScope 的每轮循环末尾。

**多 agent 协作 / 子 agent / 任务分解：支持，但走 HTTP 而非进程内直调。**
- 工具面：`agents/tools/agent_management.py` 的 `@tool_descriptor` 函数 —— `list_agents`(:567)、`chat_with_agent`(:592)、`submit_to_agent`(:683)、`check_agent_task`(:788)、`spawn_subagent`(:1147，内部用同文件 `_generate_subagent_session_id`(:863)/`_build_spawn_request_context`(:889))。
- 实现方式是**调用本机 REST API**（`create_agent_api_client`、`stream_agent_chat`、`build_agent_chat_request`），父 agent 通过 `request_context`（`root_session_id` / `root_agent_id` / `subagent_allowed_tools`）传递身份与白名单；子 agent 工具白名单在 `AgentBuilder.apply_subagent_tool_whitelist`(:238) 最终兜底过滤。
- 任务分解则来自 **skill 而非引擎**：`agents/skills/make_plan-*`、`multi_agent_collaboration-*`、`modes/mission/`（`/mission` 会生成 `loop dir + PRD + task.md + progress.txt`，见 `modes/mission/state.py`）。
- 另一条独立产品线：`harnesses/{codex,qoder}/adapter.py` 实现 `harnesses/base.py: HarnessAdapter`（`status/login/models/history/discover_mcp/run_turn/stop`），把第三方 CLI agent 当"后端"，事件经 `harnesses/events.py: HarnessEvent` 归一化。这是真正的"接入别的 agent"，而非 spawn 自家 agent。

---

## 3. 工具（tool）体系

- **声明与注册**：装饰器 `runtime/tool_registry.py:216 @tool_descriptor(...)` 把 `ToolDescriptor`(:55) 挂到 `fn._tool_descriptor`，同时把函数推进全局列表；`:196 get_builtin_tool_funcs()` 只认 `__module__` 以 `qwenpaw.agents.tools.` 开头的函数（防止测试/插件污染）。内置工具在 `agents/tools/__init__.py` 靠**逐行 import 触发装饰器**完成自动收集（无手工清单、无文件扫描）。
- **描述符的四维门控**：`requires_modes / requires_skills / requires_features / requires_sandbox`，另有 `enabled_by_default`、`async_execution`、`governance: ToolGovernanceSpec`（`tool_type/target_param/policy_name/fail_without_sandbox/default_policy`）、`ui: ToolUISpec`。
- **Schema 生成**：不自己写 JSON Schema——工具是普通 Python 函数（带类型注解/docstring），AgentScope 的 `Toolkit` 负责生成 function-calling schema；QwenPaw 只额外把 descriptor 的 UI/治理元数据反向喂给 `ToolsConfig`（`config/config.py:2695 _default_builtin_tools()`，用 `importlib.import_module` 刻意避开静态循环依赖）。
- **每请求选工具**：`ToolRegistry.filter()`(:137)（deny 优先 → allow 白名单 → enabled_by_default → 四维门控）由 `app/workspace/local_workspace.py:66 QwenPawLocalWorkspace.list_tools()` 调用（它继承 AgentScope `LocalWorkspace`，用自家工具替换掉 AgentScope 自带的 6 个），最后统一包成 `PolicyGuardedTool`。
- **调用生命周期**：`tool_calls/` 是 QwenPaw 最扎实的子系统。
  - `tool_calls/_middleware.py:96 ToolCoordinatorMiddleware(MiddlewareBase)` 实现 AgentScope 官方扩展点 `on_acting`，把执行权交给 coordinator。
  - `tool_calls/_coordinator.py:52 ToolCoordinator` 是**单个工具调用的唯一所有者**：`execute()` 建 `ToolCallEntry`(:22)（状态 `RUNNING/OFFLOADED/COMPLETED`），起后台 `asyncio.Task`，前台 `_await_next_event` 竞争 `chunk / cancel / deadline_changed` 四类事件。
  - **超时是四档解析**（`_resolve_timeout`：per-call override > per-agent per-tool > hook 默认 > 全局），默认值在 `react_agent.py:_register_tool_call_hooks`(:1279) 逐工具注册（shell 60s、chat_with_agent 300s、grep 30s、lsp 20s…）。
  - **前台/后台双 deadline**：`offload_deadline`（默认超时的 50%，`OFFLOAD_TIMEOUT_RATIO`）到点则"卸载到后台"，返回带 `metadata.offloaded` 的提示并让模型继续干别的；`kill_deadline` 是硬杀。后台结果通过 `_pending_hints` 队列 + `pop_pending_hints`，在每轮 `_reasoning` 前注入上下文（`react_agent.py:_inject_pending_hints`）。这是很成熟的设计：**长工具不会阻塞对话循环**。
  - 取消是"协作式 + 强制"两级：`cancel_event` 先给工具机会优雅退出，`_cancel_grace`(5s) 后才 `task.cancel()`（`_await_grace_or_force_cancel`）。
- **并行/串行**：由 AgentScope `_next_action` 决定批次，QwenPaw 另提供显式批处理工具 `agents/tools/run_tool_batch.py:1102 run_tool_batch`（JSON 批任务，支持 `${steps.N.path}` 引用、`${vars.X}` 变量、条件与循环，`MAX_BATCH_STEPS=50`）。真正的高并发编排靠它而非 LLM 原生并行调用。
- **结果回灌与错误**：`agents/utils/tool_message_utils.py`（含 `_sanitize_tool_messages`，清理由 evicted tool_call 造成的孤儿 tool_result）、`agents/utils/tool_call_coerce.py:207 _coerce_tool_input`（按 JSON Schema 把模型误输出的数字/布尔"无损"转成 string 字段，解决 `1.000001 is not of type 'string'` 类系统性失败）；非法 JSON 交给 json-repair。工具失败的呈现：错误文本进 `ToolResponse(state=ERROR)`，再由 `_cancel_message_for_llm` 明确告知模型"别重试"。**工具层面没有自动重试**（grep `retry` in `tool_calls/` 只命中提示文案）；重试只存在于模型调用层。
- **结果修剪**：`agents/middlewares.py:721 ToolResultPruningMiddleware` 在 `on_acting` 按字节分档裁剪并把全文落盘；`agents/offloader.py:29 QwenPawOffloader` 写 `dialog/{date}.jsonl` 与 `tool_results/{uuid}.txt`。

---

## 4. 模型 / Provider 抽象

- **基类**：`providers/provider.py:523 class Provider(ProviderInfo, ABC)`，配置字段 + `get_chat_model_cls()/get_chat_model_instance()`(:1164) + `get_context_size()`(:1112) + `probe_model_multimodal()`。`ModelInfo`(:83) / `ProviderInfo`(:318) 是 pydantic 声明。**注意：抽象是"配置 + 工厂"而非工具调用协议**——真正的能力接口由 AgentScope 的 `ChatModelBase` 承担。
- **内置 provider 约 30 家**：`providers/provider_catalog.py:520 BUILTIN_PROVIDERS`（dashscope / openai / openai-response / anthropic / gemini / ollama / lmstudio / modelscope / openrouter / deepseek / kimi / zhipu / minimax / volcengine / mimo / azure-openai / github-models / opencode / kilo …）。多数云厂商继承 `OpenAIProvider`（`lmstudio_provider.py:8`、`mimo_provider.py:21`、`modelscope_provider.py:12`、`ollama_provider.py:15`），只有 Anthropic/Gemini/OpenRouter 是独立实现。`providers/provider_manager.py:82 ProviderManager` 是**单例**，管内置/自定义/插件三类 provider 与持久化。
- **`tl_provider.py` 的含义**：TL = "text-only，内部两阶段 chatbbc 协议"。`providers/tl_provider.py:17 TLProvider` + `providers/tl_chat_model.py:33 TLChatModel(ChatModelBase)` 把**不支持原生 tool calling 的模型**（公司网关 / 本地 proxy）适配成 AgentScope 模型：工具说明写进 system prompt，模型输出 JSON，由 `tl_prompt_codec.py`(:compile_prompt/parse_response) 解析并做**纠错重试**（`build_correction_payload`、`is_correctable_json_error`），工具执行始终留在 AgentScope 侧。`tl_transport.py` 负责 HTTP/SSE，`tl_preview.py` 提供流式预览，`tl_wire_log.py` 做脱敏协议日志。
- **装饰器链**（`agents/model_factory.py:2188 create_model_and_formatter()`）：

```
provider.get_chat_model_instance(model_id)      # 原生 ChatModelBase（自带 self.formatter）
   │  model.max_retries = 0                     # 关掉 AgentScope 内层粗糙重试
   ▼
TokenRecordingModelWrapper(provider_id, model)  # token_usage/model_wrapper.py:63
   ▼
RetryChatModel(...)                             # providers/retry_chat_model.py
   ▼
_apply_model_fallbacks(...) → FallbackChatModel # providers/fallback_chat_model.py:40
   ▼
(formatter 单独包装) _install_model_formatter()  # 加 file-block/TL/capping 支持
```
- **流式**：AgentScope `ChatModelBase` 原生支持；QwenPaw 侧的处理在 `runtime/executor.py`（preview 队列）、`providers/stream_progress.py: has_meaningful_stream_content()`（判断"是否已产出真实内容"以决定能否安全回退）、`providers/capping_formatter.py:76 CappingFormatterMixin`（超限本地媒体替换为占位符，防止 42MB 视频被 base64 反复内联打爆请求体）。
- **重试与降级**：`providers/retry_chat_model.py:143 RetryConfig/153 RateLimitConfig`（指数退避 + Retry-After + 流空闲超时），`providers/rate_limiter.py:39 LLMRateLimiter`（**按 `provider:model` 隔离**的 QPM 滑动窗口 + 并发信号量 + 429 全局暂停，避免一个模型的 429 拖死其他模型），`providers/model_error_policy.py:65 classify_model_error()` 统一判定 `authentication/bad_request/context_overflow/rate_limit/transient` 并给出 `is_retryable_same_model / is_fallback_eligible`。
- **能力协商 / 降级**：多层组合 —— `providers/capability_baseline.py`（厂商声明的期望能力基线 + 差异上报）、`providers/model_capability_cache.py:42 ModelCapabilityCache`（**试错学习**：确认失败→恢复后才写入，带 TTL，仅在内存）、`providers/multimodal_prober.py`（32×32 PNG 探针 + `evaluate_image_probe_answer`）、`providers/model_catalog.py`、`providers/context_windows.py:126 resolve_context_window()`（四级优先级的上下文窗口解析）。运行时降级在 `agents/react_agent.py`：`_is_explicit_media_capability_error`(:1193)、`_is_global_media_capability_error`(:1236)、`_strip_media_blocks_from_memory`(:1382) —— **用 12 条正则去猜"这个模型不支持图像"**（见 §9.2 坏味道）。
- **Token 计数与核算**：
  - 计数：`agents/utils/token_counter.py: get_token_counter()` → `EstimatedTokenCounter`（按配置的 `token_count_estimate_divisor` **启发式估算**，不是 tiktoken/真 tokenizer）；媒体另算 `agents/utils/media_token_estimate.py`（图片/音频/WAV 时长分档）。
  - 核算：`token_usage/model_wrapper.py:63 TokenRecordingModelWrapper` 从响应 `usage` 提取并落库，`token_usage/manager.py:97 TokenUsageManager`（单例 + 异步批量 flush，`_query/get_summary/get_details` 支持按 agent/model/date 聚合），`token_usage/turn_usage.py: persist_turn_usage()` 把每轮用量写进消息 metadata，`token_usage/storage.py` 持久化。

---

## 5. 上下文与记忆

- **策略接口**：`agents/context/base.py: ContextManager(Protocol)` —— 三个方法 `on_save / compress / recover_from_context_overflow`。`QwenPawAgent` 在 `_save_to_context`(:306)、`_compress_context_impl`(:301)、溢出重试三处委托；**不注入则退回 AgentScope 原生行为**，完全可选。
- **三条上下文策略**：
  1. **native**（默认）：AgentScope 的摘要压缩（`SummarySchema` 五段式 task_overview/current_state/important_discoveries/next_steps/context_to_preserve，见 `agentscope/agent/_config.py:9`）。
  2. **scroll**（`agents/context/scroll/manager.py:79 ScrollContextManager`，2182 行）：把对话持久化到 SQLite `history.db`（`memoryspace.py` 2015 行），超限时把旧轮次**折叠成"驱逐索引"**，模型可用沙箱内的 `recall_history_python` REPL（`recall_tool.py`）回捞原文——本质是"可回滚的无限上下文"。
  3. **visual_compression**（`agents/context/visual_compression/`，含 `pipeline/budget.py|precision.py|receipt.py`）：**请求时把文本上下文渲染成图片**再送给多模态模型，压缩 token 占用（自述参考 pxpipe）。开关在 `config/config.py:1217 VisualCompactConfig`。
- **预算控制**：`runtime/builder.py:1025 _build_context_config()` 把 QwenPaw 的 `ContextCompactConfig` 映射为 AgentScope `ContextConfig`（`trigger_ratio` / `reserve_ratio` / `tool_result_limit` / `max_image_num`）。有意思的取舍：当统一裁剪中间件启用时，把 AgentScope 的 `tool_result_limit` 设为 `2**63-1` 让它"不起作用"，避免两套裁剪互相踩；`max_image_num` 同理让位给 visual compression。超限时 `ContextWindowUnfitError`（`agents/context/types.py`）给出可操作的报错。
- **memory 子系统**：契约在 `agents/memory/base_memory_manager.py:64 BaseMemoryManager(ABC)`（`start/close/get_memory_prompt/list_memory_tools/memory_search/build_middlewares/list_cron_jobs/submit_auto_memory/get_runtime_status`），注册表 `:844 MemoryBackendRegistry`（带实例租约 `_MemoryBackendSelectionLease` 与 owner 卸载协调）。外部插件只允许从 `src/qwenpaw/memory/__init__.py` 导入契约（该文件只有 30 行，是有意的稳定门面）。
  实现主力是 **ReMe**（依赖 `reme-ai==0.4.1.11`）：`agents/memory/reme_light_memory_manager.py`，配 `reme_embedding.py / reme_reranker.py / reme_config.py / reme_inbox.py`，即 **embedding 检索 + reranker 重排**的 RAG 记忆，外加 `proactive/` 主动记忆。注入方式不是塞 system prompt，而是**memory middleware**：`agents/middlewares.py:94 MemoryMiddleware` 在 `on_model_call/on_reply/on_compress_context` 上做自动搜索、缓存 turn 快照、flush 自动记忆。
- **持久记忆文件**：`agents/memory/agent_md_manager.py` + `agents/md_files/{zh,en,ru,id,qa,local}/{AGENTS,SOUL,PROFILE,MEMORY,BOOTSTRAP,HEARTBEAT,CONTACTS,MAIL_TRIAGE}.md`。它们通过 `runtime/prompt_contributors.py:146/178/194 AgentsMdContributor/SoulMdContributor/ProfileMdContributor` 与 `:210 WorkspacePromptFilesContributor` 进入 system prompt，并用 `<!-- memory:start -->…<!-- memory:end -->` 之类的 HTML 注释锚点做局部重写。
- **附件进入上下文**：`src/qwenpaw/attachments.py:42 extract_attachment(filename, bytes)` —— **只吃 bytes、不落盘**，白名单 pdf/docx/xlsx/txt/md/csv/json/log；Office 走内存 zip 校验（拒绝 DTD/ENTITY、externalLinks、vbaProject 等），限制 10MiB 输入 / 100k 字符输出 / 512 zip 条目。媒体侧由 `agents/utils/image_freezing.py`（冻结本地图片为稳定标识）、`runtime/builder.py:_build_offloader` 与 `hooks/request_setup/media_hook.py: MediaProcessHook` 处理。

---

## 6. 状态与持久化

- **Session/Thread 模型**：`app/chats/models.py:97 ChatSpec` + `:219 ChatHistory`，由 `app/chats/manager.py:63 ChatManager` 管理（分组、归档、批量操作）。**LLM 状态**由 `app/chats/session.py:205 SafeJSONSession` 落在 `sessions/{uid}_{sanitized_sid}.json`（Windows 非法字符替换、`get_path_lock` 做进程内文件锁、`write_json_atomic_async` 原子写）。
- **断点续跑**：`hooks/session/session_hook.py:35 SessionLoadHook`（`PRE_AGENT_BUILD` 阶段读盘）与 `:79 SessionSaveHook`（`POST_RESPONSE` 写盘）；取消/异常路径上 `Runtime._try_save_on_cancel()`(:239) 兜底，并用 `asyncio.shield` 防止二次取消丢状态——这是个真实工程细节。`QwenPawAgent.state_dict/load_state_dict` 负责 `AgentState` 的 JSON 往返，并**在线兼容 1.x 的 `{"memory": {...}}` 旧格式**；`_sanitize_loaded_context()`(:398) 清理历史遗留的孤儿 tool_result。
- **Checkpoints（git 实现，非自研存储）**：`checkpoints/repository.py:33 CheckpointRepository` 直接 shell 调 `git`（`run_git`），workspace 即 git 仓库，`write_workspace_tree()` 写 tree，`refs/heads` 索引各 session head；`checkpoints/service.py:64 CheckpointService` 提供 `snapshot/make_auto_checkpoint/timeline/graph_entries/restore/restore_with_memory/restore_with_files/gc`；`checkpoints/models.py: CheckpointEntry/SnapshotResult/RestorePlan/RestoreResult/GcResult` 是清晰的不可变数据契约；`checkpoints/runtime.py:24 Debouncer` 做自动快照去抖，`:83 CheckpointRuntime` 按 workspace 缓存 service；策略（保留数/天数/开关）在 `checkpoints/policy.py:204 CheckpointPolicy`（读 config，带 `reload(force)`）。
- **并发与锁**：进程内用 `asyncio.Lock` + `get_path_lock`（文件级）；`TokenUsageManager` 单例带后台 flush 任务；`app/chats/manager.py` 有 `_patch_locked`/`_validate_group_id_locked` 等命名约定。多用户服务侧（`server/`）则是**数据库级**并发：`server/migrations/006_session_queue.sql`、`server/storage/repository.py:79 SQLRepository` 与 `server/repository.py:85 PostgresRepository` 提供 `claim/heartbeat/finish/expired/interrupt`（worker 租约 + epoch fencing），已经是完整的分布式任务队列语义。

---

## 7. 中间件与扩展点

QwenPaw 有**两套正交的"中间件"**，文档里明确区分（`runtime/phases.py` 与 `runtime/hooks.py` 的 docstring 都强调）：

| 层 | 基类 | 作用域 | 注册点 |
|---|---|---|---|
| 请求生命周期 hook | `runtime/hooks.py:147 HookBase` → `hooks/base.py: LifecycleHook` / `modes/base.py: ModeGatedHook` | 整个 `Runtime.run()`，8 个 phase | `workspace.plugins.hook_registry` |
| Agent 循环中间件 | `agentscope.middleware.MiddlewareBase` | 单个 agent 的一次 reply（`on_system_prompt/on_model_call/on_reasoning/on_acting/on_reply/on_compress_context`） | `AgentBuilder._build_middlewares` |

- **内置 hooks**（`app/workspace/bootstrap_factory.py` 汇总，16+2 个）：`SessionLoad/SaveHook`、`BootstrapHook`（PRE_EXECUTE 首次引导）、`SkillEnvHook/SkillEnvCleanupHook`、`ContextVarsSetupHook`/`AgentContextVarsSetupHook`、`MediaProcessHook`、`ErrorNormalizeHook`、`CancelCleanupHook`、`CronContextHook/CronMemoryIsolateHook/CronMemoryRestoreHook`、`CheckpointQueryGateHook`/`CheckpointAutoSnapshotHook`、`LangfuseTraceHook`/`LangfuseTraceCleanupHook`、`MailF1CleanupHook`。注意 `ContextVarsSetupHook` 把 project dirs/session/toolkit/agent_state 灌进 `config/context.py` 的 ContextVar —— 这是 QwenPaw 把"请求上下文"传给深层工具的**主要隐式通道**。
- **中间件顺序**（`runtime/builder.py:1332 _build_middlewares`，洋葱模型由外到内）：`ToolResultPruningMiddleware` → `ToolCoordinatorMiddleware` → 记忆中间件 → Langfuse span → 插件中间件（按 priority）→ `VisualCompressionMiddleware`（最内层，紧贴 provider）。
- **插件体系**：`plugins/architecture.py: PluginManifest` + `PluginType`（`tool/provider/hook/command/channel/memory/frontend/app/general`），带 `QwenPawVersionConstraint`（`>=min, <max`）语义化版本约束；`plugins/loader.py:204 PluginLoader` 负责发现（插件目录 + `importlib.metadata` 依赖校验）、装依赖（自带 uv 子进程、安装锁 `install_lock.py`）、`_validate_entry_points`、失败回滚 `_cleanup_failed_load`；`plugins/registry.py:130 PluginRegistry` 是**单例**（`__new__`），进程级注册中心；`plugins/api.py:314 PluginApi` 是暴露给第三方的**版本化 API**，提供 `register_tool / register_mode / register_runtime_hook / register_agent_stop_handler / register_prompt_section / register_middleware / register_memory_backend / register_provider / register_channel / register_slash_command / register_http_router / register_startup|shutdown|uninstall|workspace_created_hook` —— **这是全项目对外扩展面最宽、也最清晰的一处设计**。
- **governance（治理）**：`governance/policy.py:639 GovernancePolicy` 是两层规则引擎（`builtin_rules` 系统保护 + `user_rules` 用户可改），动作 `ALLOW/DENY/ASK/SANDBOX_FALLBACK`（`:39`）；评估顺序为 Phase 0 工具类型 → Phase 1 深度检测（`detectors.py` 的 `detect_sensitive_paths/detect_dangerous_patterns/detect_shell_evasion`，无状态纯函数）→ Phase 1.5 shell 危险关键字 → Phase 2 规则。`governance/resource_governor.py:44 ResourceGovernor.assert_policy()`(:220) 是唯一入口，负责沙箱降级决策（沙箱不可用则 `SANDBOX_FALLBACK → ALLOW`，但 STRICT 提前 ASK）与 `compile_sandbox_config()`(沙箱挂载/网络端口规则编译)。`governance/tool_adapter.py:108 PolicyGuardedTool` 是包装器（取代已废弃的 `runtime/tool_guard.py: GuardedFunctionTool`，向后兼容保留）。审计落 `governance/audit.py: AuditLog`（**SQLite `~/.qwenpaw/audit.db` 单例**，含 workspace/agent/tool/decision 索引，满 10 万条自动清最旧 1 万条）。
- **security（安全）**：三块独立能力 —— `security/tool_guard/`（`ToolGuardEngine` + `BaseToolGuardian`/`RuleBasedToolGuardian`/`FilePathToolGuardian`/`ShellEvasionGuardian`，YAML 规则签名匹配）、`security/skill_scanner/`（扫描 skill 的提示注入/恶意代码，带 `scan_policy.py` 与白名单 `SkillScannerConfig`）、`security/secret_store.py`（Fernet AES-128-CBC+HMAC 加密敏感字段，主密钥优先进 OS keychain `keyring`，回退 `SECRET_DIR/.master_key` 0600；密文带 `ENC:` 前缀便于明文平滑迁移）。
- **sandbox（沙箱）**：统一工厂 `sandbox/config.py:694 create_sandbox(config)` + `SandboxMode`(:56) `SEATBELT/BUBBLEWRAP/LANDLOCK/WINDOWS/NONE`；`probe_sandbox_support()`(:650) 做能力探测，`report_unenforced_config()`(:323) **明确报告"你要求了但实际没生效"的限制**（很诚实的设计）。`sandbox/local_sandbox.py: LocalSandbox` 是 ABC，`NoneSandbox` 是无隔离直通。生命周期是 **per-tool-call**（`async with create_sandbox(cfg)`）。
- **drivers（外部能力接入）**：不是硬件驱动，而是"MCP/外部工具的可治理调用层"。`drivers/contracts.py:104 DriverCard`（声明式能力卡 + `CredentialRef` + 策略），`drivers/manager.py:48 DriverManager`，`drivers/handler.py:105 DriverHandler`（模板方法，`_resolve_driver_execution_level`），`drivers/policy_types.py`（`PolicyEffect/PolicyRule/PolicyPrincipal/TimeRange` 的驱动级策略），`drivers/approval.py: ApprovalGate`，`drivers/credentials/`（凭据提供者与绑定解析），`drivers/handlers/{mcp,mcp_stateful_client,mcp_streamable_http}.py` 是三种 MCP 传输。人类审批由 `app/approvals/service.py:115 ApprovalService` 承担（身份校验 `ApprovalIdentityMismatchError`、`PendingApproval`）。

---

## 8. 可观测性

- **日志**：`utils/logging.py`（含 `sanitize_log_value` 广泛用于日志脱敏）；`utils/telemetry.py`。
- **Trace**：`observability/langfuse.py` —— **完全可选**（`_langfuse_available()` 用 `find_spec` 探测，未装则所有 helper 变 no-op）。`agent_trace_scope()` 建一次 ReAct 循环的根 span，`tool_span()` 建子 span，`current_generation_kwargs()` 把 trace 信息透传给 `langfuse.openai` 的 SDK 包装。挂载点是 `hooks/observability/langfuse_hook.py`（PRE_EXECUTE/FINALLY）与 `agents/middlewares.py:951 LangfuseToolSpanMiddleware`。另有独立协议级 trace：`providers/tl_wire_log.py: log_wire()` 记录 TL 请求/SSE 事件（脱敏、超 32k 截断）。
- **Metrics / 统计**：`token_usage/manager.py:97 TokenUsageManager`（token）与 `agent_stats/service.py:272 AgentStatsService`（**直接扫 session JSON 文件**统计 `ChannelStats/DailyStats/AgentStatsSummary/LlmToolDaily`，用 mtime 与内容范围双重跳过避免全量重算，`_extract_turn_usage_tokens`/`_extract_turn_cache_tokens` 从消息 metadata 取数）。工具级统计另有 `agents/utils/as_msg_stat.py`、`agents/utils/context_stats.py`。
- **面向用户的实时观测**：`runtime/envelope.py: Envelope` 把 AgentScope 事件翻译成 SSE（含 heartbeat），前端可看到工具调用、offload、preview 等；`tool_calls` 的 `GET /output` 语义由 coordinator 的 `_completed_cache`（TTL 60s、上限 50）支撑。

---

## 9. 分层边界、耦合与坏味道

### 9.1 边界清晰之处
1. **`tool_calls/` 与其余一切解耦**：工具的并发、超时、取消、offload、结果缓存全部收在一个 coordinator 里，其它模块只通过 `MiddlewareBase.on_acting` 接触它。独立可测、可复用。
2. **`loop/` 的"停止门"抽象**：`loop/gates/base.py: StopGate` + `loop/gates/handler.py: StopHandler` + `loop/catalog.py: GateCatalog`（7 种内置 gate 的**白名单 + JSON Schema 元数据**）+ `loop/compiler.py: compile_loop_mode()`（校验 → 互斥组检查 → 构造）。让"什么时候该停"变成**用户可配置的声明式流水线**，而不是散落的 if。这是全项目最漂亮的一处设计。
3. **扩展面收敛到 `PluginApi` + `PromptContributor` + `HookBase` + `AgentMode` 四个基类**，且都有版本化/文档化的意图（`AgentMode.setup()` 是唯一注册路径）。
4. **配置 → 运行时的单向映射**：`AgentProfileConfig` 只管声明，`AgentBuilder` 负责解释，agent 本体不读配置（`QwenPawAgent.__init__` 显式只接依赖）。
5. **多用户侧 `server/` 与单机侧 `app/` 完全分离**，各有一套存储接口（SQL/Postgres），不互相污染。

### 9.2 明显的高耦合与坏味道（举例）
1. **超长文件**：`agents/tools/deprecated_browser/browser_control.py` **5629 行**（单文件一个浏览器自动化实现）；`config/config.py` 3956 行（30+ 个 pydantic 模型 + 加载/缓存/指纹/迁移全塞一处）；`agents/model_factory.py` 2431 行；`agents/skill_system/hub.py` 2388 行；`agents/context/scroll/manager.py` 2182 行 + `memoryspace.py` 2015 行；`governance/policy.py` 1885 行；`agents/tools/agent_management.py` 1807 行。这些文件本身就是"模块即文件"的反面教材。
2. **上帝类**：`agent_management.py` 一个模块同时承担 HTTP 客户端构造、SSE 解析、任务状态机、5 个工具的 schema 与业务逻辑。`QwenPawAgent`（1445 行）把"多模态能力探测"写成了**12 条正则 + 4 个 `_is_*_error` 分类函数**（`:1193/:1236` 及 `_GLOBAL_MEDIA_CAPABILITY_PATTERNS`），用字符串匹配猜上游语义——脆弱且难以维护。`ToolCoordinator` 也把"状态表 + 超时解析 + 缓存 + hint 队列 + 事件循环"合成一个几百行的方法集合。
3. **循环依赖靠注释和 `importlib` 硬绕**：`config/config.py:2710` 注释明说用 `importlib.import_module` 是为了"不与 `agents.tools → delegate_external_agent → config` 形成静态环"；`security/secret_store.py:37`、`config/timezone.py:4`、`agents/memory/__init__.py:20`、`app/chats/title_generator.py:98` 都在注释里承认存在环。`config/config.py` 反过来 import `runtime/tool_registry` 与 `agents.tools`（配置层反向依赖运行时层）——L0 依赖 L2/L3，分层图上这是倒挂。
4. **隐式全局状态泛滥**：`ProviderManager.get_instance()`、`TokenUsageManager.get_instance()`、`AuditLog.get_instance()`、`ModelCapabilityCache.get_instance()`、`PluginRegistry`（`__new__` 单例）、`config/context.py` 里 15+ 个 `ContextVar`（workspace_dir/project_dirs/session_id/toolkit/agent_state/shell timeout…）。ContextVar 方便，但也让函数签名无法反映真实依赖，测试隔离困难。
5. **异常吞没**：全仓 **1625 处 `except Exception`**，大量是 `logger.debug(..., exc_info=True)` 后继续。在 `bootstrap_factory.py`、`builder._build_middlewares` 里到处都是 try/except 包裹的可选组件——启动永不失败，但也永不告警（只在 debug 级别）。同时有 **701 处 `pylint: disable`**、**200 处 `too-many-*`** 抑制注释，说明复杂度是"被承认"而非"被解决"的。
6. **核心循环不在自己手里**：ReAct 主循环、消息模型、Toolkit、Formatter、Provider SDK 抽象全部来自 `agentscope`，且版本被 `==2.0.7.post1` 精确钉死到 patch 级；QwenPaw 通过**继承 + 覆写 + monkey-patch 式配置**（`model.max_retries = 0`、把 `tool_result_limit` 设成 `2**63-1`"关掉"上游逻辑）来改行为。升级 AgentScope 的代价很高，`config/config.py` 里还专门留着 1.x 会话迁移代码。

---

## 10. GenSlide 这类小项目值得学 / 难以照搬

**值得学（5）**
1. **可配置的停止条件引擎**（`loop/catalog.py` + `StopHandler`）：把"何时结束"从代码里的 `if` 变成"白名单 gate + JSON Schema 参数 + 互斥组校验"。任何 agent 项目都能直接借这个模式，成本极低。
2. **工具调用的前后台双 deadline + offload**：`OFFLOAD_TIMEOUT_RATIO=0.5` 到点自动转后台、结果经 hint 队列回注上下文。让长任务不阻塞对话，是小项目最容易缺、也最容易补的一块体验。
3. **插件 API 作为唯一扩展面**：`PluginApi` 的 16 个 `register_*` 方法 + manifest 的类型/版本约束，把"第三方能改什么"显式化。哪怕只支持 3 个扩展点，也应该像它这样集中在一处并声明合同。
4. **策略 + 审计分离的治理层**：`ResourceGovernor.assert_policy()` 只决策不落库，`audit()` 单独调用；规则两层（系统 / 用户）且系统层不可被 agent 修改。安全敏感项目可以直接抄这个形状。
5. **诚实的能力探测与降级报告**：`sandbox/config.py:report_unenforced_config()`、`model_capability_cache`（只在"失败→恢复"确认后写入）、`context_windows.resolve_context_window` 的四级优先级。比"静默假设"健康得多。

**明显过重、不建议模仿（5）**
1. **自建 git checkpoint 子系统**（`checkpoints/`，12 文件 4449 行，直接 shell 调 git）。快照/回滚用一个 `shutil.copytree` + 版本目录就能覆盖小项目 90% 需求。
2. **三条并行的上下文策略**（native 摘要 / scroll 无限上下文 / 视觉压缩成图），其中 scroll 独占 4000+ 行。小项目选一条做到位即可，同时维护三条会拖垮迭代速度。
3. **30 家 provider 的静态目录 + 能力基线 + 探测探针**（`provider_catalog.py`、`capability_baseline.py`、`multimodal_prober.py`、`model_capability_cache.py`）。小项目用"OpenAI 兼容 + 一个 base_url"即可，靠异常信息驱动降级。
4. **12 条正则猜测模型是否支持图片**（`react_agent.py:_GLOBAL_MEDIA_CAPABILITY_PATTERNS` 等）。这种基于错误文案的启发式规则既脆弱又难测，应优先用"配置 + 探针结果"两条确定性来源。
5. **一个 workspace 全套注册表 + 15 个 hook + ContextVar 隐式通道**：`Runtime` 的 8 阶段 + hook 拓扑排序 + ContextVar 注入，对 3-5 个 agent 的项目是纯负担。小项目用显式的函数调用链（`load → build → run → save`）更容易调试。

---

## 11. 一句话总结

QwenPaw 是**"AgentScope 内核 + 产品化外壳 + 治理层"**的三段式架构：真正的 ReAct 循环、消息与模型抽象全部外包给 AgentScope 2.0.7；QwenPaw 自己最有价值的产出集中在**请求编排（`runtime/` 8 阶段）、工具生命周期（`tool_calls/`）、停止门引擎（`loop/`）、治理与沙箱（`governance/`+`security/`+`sandbox/`）、插件 API（`plugins/api.py`）**这五块，而它的复杂度主要来自"支持一切"的产品定位（30 家 provider、18 个消息渠道、3 条上下文策略、2 套存储后端、Windows/macOS/Linux 三平台沙箱）以及对外部库的深度改写。
