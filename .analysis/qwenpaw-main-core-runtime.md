# QwenPaw 官方 main 分支核心运行时架构分析（修正版）

> 基线：`/tmp/qpmain`（`git archive origin/main` 的干净导出）。**所有结论只以此为准**，路径均相对 `/tmp/qpmain/src/qwenpaw/`。
> 对照物：`/Users/yangxuezhen/git/GenSlide/.analysis/qwenpaw-core-runtime.md`（写于本地 `multisync` 分支）。
> 规模实测：**978 个 `.py`、334,695 行**（排除 `__pycache__/` 与 `tokenizer/`）。旧报告的 1011 个 / 34.7 万行是 `multisync` 的数字。
> 关键前提不变：ReAct 主循环不在本仓库，而在被钉死到 patch 级的依赖 `agentscope[model-ollama]==2.0.7.post1`（`pyproject.toml:8`）中。

---

## 0. 分层总览（修正）

```
L6 入口       cli/  app/  pawapp/  tauri/          HTTP·SSE·CLI·单账号服务
L6' 控制面    hub/                                多租户控制面（进程/容器编排 + 反向代理）
L5 集成扩展   harnesses/  plugins/  drivers/  agents/acp/  Codex/Qoder 适配·插件·外部能力(MCP)
L4 请求编排   runtime/  modes/  hooks/  loop/      8 阶段生命周期·模式·停止门
L3 Agent 本体 agents/                              ReActAgent·tools·context·memory·skills
L2 执行与治理 tool_calls/  governance/  security/  sandbox/
L1 基础设施   providers/  token_usage/  observability/  utils/
L0 基座       config/  constant.py  exceptions.py  schemas.py
```

**`src/qwenpaw/server/` 在官方 main 上不存在**，`src/qwenpaw/office/`、`src/qwenpaw/attachments.py`、`src/qwenpaw/cli/serve_cmd.py`、`providers/tl_*.py`、`test-tools/` 同样不存在。它们是 `multisync` 相对 main 的净新增（`git diff --name-status origin/main multisync -- src/qwenpaw` 中 42 个 `A` 条目）。main 上唯一被忽略的分层是 **`hub/`（24 个模块 / 7,475 行）**，见 §9。

---

## 1. Agent 的定义与注册

Agent 仍是「配置 + 模板 + 代码类」三层，且 main 上的实现与旧报告一致：

- **声明式配置**：`config/config.py:2217 class AgentProfileConfig(BaseModel)`，字段包含 `id/name/backend/template_id/workspace_dir/running/llm_routing/active_model/fallback_models/subagent_model/fallback_policy/channels/mcp/heartbeat`。全局索引 `config/config.py:2355 class AgentsConfig`。该文件共 3,956 行、**75 个顶层 class**（旧报告"30+ 个 pydantic 模型"偏低）。
- **模板**：`agents/templates.py:23 SUPPORTED_AGENT_TEMPLATES = ("default","local","qa")`，`build_agent_template()` 产出 `agents/templates.py:37 AgentTemplateBuildResult`（`agent_config/initial_skill_names/md_template_id`）。差异化在 `config/config.py:2791 build_qa_agent_tools_config()` 与 `:2813 build_local_agent_tools_config()`。
- **运行时类**：`agents/react_agent.py:150 class QwenPawAgent(CodingModeMixin, Agent)`（旧报告写 :158，是 `multisync` 的行号）。`__init__` 只接注入依赖，不自行构造。
- **注册的是 workspace 而非 Agent**：`app/workspace_registry.py:43` 调 `workspace.bootstrap_plugins(**kwargs)`（`app/workspace/workspace.py:288`），kwargs 由 `app/workspace/bootstrap_factory.py:18 class WorkspaceBootstrapFactory` 组装 5 类内建清单（`builtin_tool_funcs / builtin_hook_clses / builtin_mode_clses / builtin_contributor_clses / builtin_command_specs`）。多 agent 生命周期由 `app/multi_agent_manager.py:36 class MultiAgentManager` 负责（懒加载、热重载、并行启动）。
- **提示词注册**：`runtime/prompt_manager.py:26 PromptContributor` / `:58 PromptManager`（按 `priority` 拼装）；内建贡献者清单 `runtime/prompt_contributors.py:492 _ALL_CONTRIBUTORS`；不可覆盖的执行契约 `runtime/protected_prompt.py:6 PROTECTED_EXECUTION_CONTRACT_PROMPT`。
- **技能（skill）是 agent 能力的第二个注入面**：`agents/skills/` 为每个技能提供 `-en` / `-zh` 双语目录（如 `make_plan-en|zh`、`multi_agent_collaboration-en|zh`、`chat_with_agent-en|zh`、`docx|pdf|pptx-en|zh`），属**文件系统 + 声明式 SKILL.md**而非代码注册；模板通过 `AgentTemplateBuildResult.initial_skill_names` 预置（`agents/templates.py:30 LOCAL_TEMPLATE_SKILL_NAMES = ("make_plan",)`），`qa` 模板另用 `constant.py` 的 `BUILTIN_QA_AGENT_SKILL_NAMES`。技能装载/信任扫描由 `agents/skill_system/`（含 2,388 行的 `hub.py`）与 `security/skill_scanner/` 承担。

---

## 2. 执行循环与阶段

1. **入口**：`app/workspace/workspace.py:414 stream_query()`。若 `agent.json` 的 `backend != "qwenpaw"` → `harnesses/runtime.py:41 class HarnessRuntime` 的 `stream()`(:112)，适配器契约在 `harnesses/base.py:27 class HarnessAdapter(ABC)`（`status/models/history/discover_mcp/run_turn/stop`），事件经 `harnesses/events.py` 归一化；否则 `Runtime.run()`。
2. **8 阶段**：`runtime/phases.py:28 class Phase` 定义 `PRE_DISPATCH / POST_DISPATCH / PRE_AGENT_BUILD / POST_AGENT_BUILD / PRE_EXECUTE / POST_RESPONSE / ON_ERROR / FINALLY`。`runtime/runtime.py:51 run()` 在阶段之间插入三个**固定步骤**（不是 hook）：`[fixed 1] slash 命令分发`(:74)、`[fixed 2] AgentBuilder.build`(:107)、`[fixed 3] AgentExecutor.run`(:127)。对应关系 :66/:84/:94/:111/:114/:139/:154/:221。
3. **Hook 语义**：`runtime/hooks.py:46 class HookAction` 三态 `CONTINUE / SHORT_CIRCUIT / SKIP_AGENT`；`HookRegistry.run()`(:293) 对 `SHORT_CIRCUIT` 立即返回并停止本阶段，对 `SKIP_AGENT` **粘性累积**（后续 hook 仍跑，阶段结果携带 SKIP_AGENT）；`HookRegistry` **不吞异常**，异常走 `ON_ERROR`。排序由 `_topo_sort()`(:168) 按 `before/after` 建边、`(priority, 注册序)` 破平、环则 `HookCycleError`。数据结构 `HookContext`(:74) 暴露 `mode_state` 与 `extras` 两个逃生口，并含 `inject_context()`(:113)；`HookBase`(:145)、`HookRegistry`(:256)。
4. **执行**：`runtime/executor.py: AgentExecutor.run()` 驱动 `agent.reply_stream()` 并用 `_iter_with_heartbeat` 包心跳，事件交给 `runtime/envelope.py:83 class Envelope`（`translate_event()`:195 / `heartbeat()`:777）翻译为 SSE。**main 上没有 preview 队列双通道**（本地为 TL 新增约 119 行）。
5. **真正的 ReAct 循环在 AgentScope**（依赖内，`/tmp/qpmain` 不含其源码，无法文件级验证）。QwenPaw 侧的改造点可在 `agents/react_agent.py` 验证：`compress_context()`(:230)、`_compress_context_impl()`(:274)、`_reasoning()`(:833)、`_register_tool_call_hooks()`(:1206)、`_run_stop_handlers()`(:1293)。
6. **多 agent 协作走 HTTP**：`agents/tools/agent_management.py` 的 `@tool_descriptor` 工具 `list_agents`(:567)、`chat_with_agent`(:592)、`submit_to_agent`(:683)、`check_agent_task`(:788)、`spawn_subagent`(:1147)，辅助函数 `_generate_subagent_session_id`(:863)、`_build_spawn_request_context`(:889)；子 agent 白名单在 `app/workspace/local_workspace.py:66 list_tools()` 内做最终过滤（`subagent_allowed_tools`，空列表=全禁）。

---

## 2b. modes/：模式即"hook 集合 + 提示词贡献者"

`modes/` 共 4,263 行 / 14 处 `except Exception`，机制是 `modes/base.py:30 class AgentMode`（`setup()` 是唯一注册路径）与 `:112 class ModeGatedHook(HookBase)`——即模式不是分支判断，而是往 `HookRegistry` 与 prompt contributor 里注册一组受模式门控的 hook/文本。内建模式：`modes/coding/`（`mixin.py` 提供 `CodingModeMixin`，正是 `QwenPawAgent` 的第一个基类；`hooks.py` 注册编码模式 hook）、`modes/default/mode.py`、`modes/custom_loop/`（`loader.py` + `mode.py`，从 `CustomLoopModeConfig` 经 `loop/compiler.py:11 compile_loop_mode()` 编译出 `StopHandler`）、`modes/goal/`（`goal_mode.py`/`gates.py`/`tools.py`/`contributor.py`/`prompts.py`）、`modes/mission/`（`state.py` 生成 loop dir + PRD + task.md + progress.txt，`hooks.py`/`gates.py`/`handler.py`/`prompts.py`）。任务分解能力来自这些模式的提示词与 gate，而非引擎原语。

---

## 3. 工具生命周期

- **声明与注册**：`runtime/tool_registry.py:216 @tool_descriptor(...)` 把 `ToolDescriptor`(:55) 挂到 `fn._tool_descriptor` 并进全局表；`get_builtin_tool_funcs()`(:196) 只收 `__module__` 以 `qwenpaw.agents.tools.` 开头的函数。`agents/tools/__init__.py`（171 行）靠逐行 import 触发装饰器自动收集——无手工清单、无文件扫描。
- **门控维度修正**：`ToolDescriptor` 声明四个 `requires_*`（`requires_modes/requires_skills/requires_features/requires_sandbox`，:70-79），但 `ToolRegistry.filter()`(:137) **只用前三维**判选；:67 的 docstring 明确写 `requires_sandbox` "Not used for selection here"，实际由 `runtime/tool_guard.py:13 class GuardedFunctionTool` 消费。旧报告把它算作"四维门控"是对描述的过度概括。
- **每请求选工具**：`app/workspace/local_workspace.py:41 class QwenPawLocalWorkspace` 的 `list_tools()`(:66) → `ToolRegistry.filter()` → 统一包成 `governance/tool_adapter.py:108 class PolicyGuardedTool`。该子类替换掉 AgentScope `LocalWorkspace` 自带的六个工具；`set_governor()`(:57) 由 `AgentBuilder` 注入（main 上是实例状态，不是逐次传参）。
- **调用生命周期**（全项目最扎实的一块）：`tool_calls/_middleware.py:96 ToolCoordinatorMiddleware.on_acting` 把执行权交给 `tool_calls/_coordinator.py:49 class ToolCoordinator`。
  - **四档超时**：`_resolve_timeout()`(:1026) 注释即"override > per-agent > hook default > global"。
  - **前后台双 deadline**：`OFFLOAD_TIMEOUT_RATIO = 0.5`（`tool_calls/_timeout_helper.py:16`），`offload_deadline = now + timeout*0.5`(:245)；到点 `_begin_offload()`(:266) 转后台并回 `metadata.offloaded`。
  - **取消两级**：`cancel_grace_period_secs=5.0`(:64)，先协作式 `cancel_event`，超时才 `task.cancel()`。
  - **结果回注**：`_pending_hints`(:78) 由 `pop_pending_hints()`(:527) 在下一轮推理前取走。
  - **完成缓存**：`_COMPLETED_CACHE_TTL_SECS=60.0` / `_COMPLETED_CACHE_MAX=50`(:31-32) 支撑 `GET /output`。
  - **工具层没有自动重试**：`grep -rn retry tool_calls/` 只命中 3 处提示文案（:958/:963/:969）。重试只在模型层。
- **批处理**：`agents/tools/run_tool_batch.py:27 MAX_BATCH_STEPS = 50`，`run_tool_batch`(:1102)。
- **结果修剪**：`agents/middlewares.py:721 class ToolResultPruningMiddleware`；落盘由 `agents/offloader.py:29 class QwenPawOffloader`（`offload_context`/`offload_tool_result`/`cleanup_expired`）。
- **错误回灌**：`agents/utils/tool_message_utils.py:574 _sanitize_tool_messages`（清孤儿 tool_result）、`agents/utils/tool_call_coerce.py:31 _coerce_tool_input`（按 JSON Schema 把数字/布尔无损转 string）。

---

## 4. Provider 抽象与能力协商（官方无 TL provider）

**这是旧报告偏差最大的一节。** main 的 `providers/` 下**没有任何 `tl_*.py`**。`providers/` 共 14,522 行、47 处 `except Exception`；最长文件为 `provider.py` 1,269、`provider_manager_persistence.py` 1,182、`retry_chat_model.py` 1,041、`openai_provider.py` 999、`openai_chat_model_compat.py` 981。

- **抽象 = 配置 + 工厂，不是协议**：`providers/provider.py:521 class Provider(ProviderInfo, ABC)`。核心字段 `chat_model: str`(:331，默认 `"OpenAIChatModel"`，语义是 **AgentScope `ChatModelBase` 子类的类名**)，由 `Provider.get_chat_model_cls()`(:754) 用 `getattr(agentscope.model, self.chat_model)` 反射解析；`get_chat_model_instance()`(:1151) 实例化；`get_context_size()`(:1099)。声明模型为 `ModelInfo`(:82) / `ProviderInfo`(:317)。真正的工具调用协议由 AgentScope 的 `ChatModelBase`/`FormatterBase` 承担——这正是"没有 TL provider"的结构性原因：**只要模型能被某个 AgentScope ChatModel 类说，它就被支持**；反之官方 main 没有"模型不支持原生 tool calling"的适配层。
- **内置 provider = 35 家**：`providers/provider_catalog.py:520 BUILTIN_PROVIDERS` 是 35 个 `Provider` 常量的元组（qwenpaw/ollama/lmstudio/openrouter/github-models/modelscope/dashscope/aliyun-codingplan(+intl)/aliyun-tokenplan(+intl)/opencode/kilo/openai/openai-response/azure-openai/anthropic/gemini/deepseek/kimi-cn(+intl,codingplan)/minimax-cn/minimax/zhipu-cn(+codingplan)/zhipu-intl(+codingplan)/siliconflow-cn(+intl)/volcengine-cn(+codingplan,agentplan)/mimo-tokenplan/mimo），旧报告"约 30 家"偏低。继承关系与旧报告一致：`providers/openai_provider.py:141 OpenAIProvider(Provider)` 是主力，`lmstudio_provider.py:8`、`mimo_provider.py:21`、`modelscope_provider.py:12`、`ollama_provider.py:15` 均继承它；仅 `anthropic_provider.py:80`、`gemini_provider.py:172`、`openrouter_provider.py:25` 直接继承 `Provider`。
- **管理器**：`providers/provider_manager.py:81 class ProviderManager` 是单例，管 builtin / custom / plugin 三类并持久化（`provider_manager_persistence.py`）。
- **能力协商的真实机制是"声明 + 探测 + 试错学习 + 报错正则"四源混合**：
  1. **静态声明**：`ModelInfo` 上的 `thinking_enabled` / `thinking_param_style` / 上下文窗口等字段，被 `Provider.supports_agent_thinking()`(:815) 与 `_apply_agent_thinking_level()`(:831) 消费。
  2. **厂商基线比对**：`providers/capability_baseline.py:31 ExpectedCapability` / `:78 ExpectedCapabilityRegistry` / `:204 compare_probe_result`——把"厂商声明的期望能力"与探测结果比对并记录差异。
  3. **试错学习**：`providers/model_capability_cache.py:42 class ModelCapabilityCache`（`get_instance()`:67 单例、`learn()`:74、`get()`:91），带 `CAPABILITY_CACHE_TTL_SECONDS` 过期，**仅内存**。
  4. **主动探测**：`providers/multimodal_prober.py` 用一张 32×32 红 PNG（:16-18 注释说明避免小图被 Ollama 等拒收）发探针，`evaluate_image_probe_answer()`(:136) / `evaluate_video_probe_answer()`(:186) 判定；入口 `Provider.probe_model_multimodal()`(:1155) 默认返回全 False，由子类覆写。
  5. **上下文窗口五级优先级**：`providers/context_windows.py:126 resolve_context_window()` —— 显式用户值 > API 探测值 > 非默认 provider 值 > 静态 pattern catalog > `DEFAULT_CONTEXT_WINDOW`。
  6. **运行时兜底靠错误文案正则**：`agents/react_agent.py:60 _GLOBAL_MEDIA_CAPABILITY_PATTERNS`（**3 条**）与 `:79 _EXPLICIT_UNSUPPORTED_MEDIA_PATTERNS`（3+**5** 条，含全局子集），分类函数 `_is_explicit_media_capability_error()`(:1120) / `_is_global_media_capability_error()`(:1163)，处置函数 `_strip_media_blocks_from_memory()`(:1309)。两个 tuple 共 **8 条正则**（旧报告写"12 条"），文件内 `re.compile(` 总计 8 次；`_is_*_error` 分类函数共 **5 个**（另有 `_is_audio_fallback_error`:610、`_is_context_overflow_error`:637、`_is_content_safety_error`:1102）。
- **装饰器链**（`agents/model_factory.py:2187 create_model_and_formatter()`，2,414 行）：`provider.get_chat_model_instance()` → `model.max_retries = 0`(:2287-2288，关掉 AgentScope 内层重试) → `token_usage/model_wrapper.py:62 TokenRecordingModelWrapper`(:2291) → `providers/retry_chat_model.py:372 class RetryChatModel`(:2296) → `_apply_model_fallbacks()`(:2096) → `providers/fallback_chat_model.py:39 class FallbackChatModel`；formatter 由 `_install_model_formatter()`(:2398) 单独包装（`:2279`）。fallback 分支同样把 `fallback_model.max_retries = 0`(:2166-2167)。
- **重试/限流/降级**：`providers/retry_chat_model.py:142 RetryConfig` / `:152 RateLimitConfig`（指数退避 + Retry-After + 流空闲超时）；`providers/rate_limiter.py:39 class LLMRateLimiter`（按 `provider:model` 隔离的 QPM 滑窗 + 并发信号量 + 429 全局暂停）；`providers/model_error_policy.py:65 classify_model_error()` 返回 `ModelErrorDecision(retryable, fallback_eligible)`，其中 `retryable = kind in {"rate_limited","transient"}`、`fallback_eligible = retryable or kind == "model_not_found"`(:80-81)，`is_retryable_same_model()`(:90) / `is_fallback_eligible()`(:95) 是薄封装；`providers/stream_progress.py` 的 `has_meaningful_stream_content()` 决定能否安全回退；`providers/capping_formatter.py:76 CappingFormatterMixin` 做超限媒体占位替换。
- **Token 核算**：计数走 `agents/utils/token_counter.py:12 get_token_counter()` → `EstimatedTokenCounter`，**按 `light_context_config.token_count_estimate_divisor` 启发式估算**（`config/config.py:1255`），不是真 tokenizer；媒体另算 `agents/utils/media_token_estimate.py`。核算走 `TokenRecordingModelWrapper` → `token_usage/manager.py:97 class TokenUsageManager`（单例 + 异步批量 flush + 聚合），每轮写消息 metadata 由 `token_usage/turn_usage.py:298 persist_turn_usage()`。

---

## 5. 上下文与记忆

- **策略接口**：`agents/context/base.py:22 class ContextManager(Protocol)`，三方法 `on_save / compress / recover_from_context_overflow`，不注入则退回 AgentScope 原生行为（可选、纯增量）。
- **三条策略**：native（默认摘要压缩）、`agents/context/scroll/manager.py:79 ScrollContextManager`（2,182 行）+ `scroll/memoryspace.py`（2,015 行），SQLite 历史 + 驱逐索引 + `scroll/recall_tool.py` REPL 回捞；`agents/context/visual_compression/`（`pipeline/{budget,precision,receipt,request,static_context,tool_results,tool_schemas,messages,history}.py` + `rendering/renderer.py` + `runtime/{middleware,recovery}.py`），开关 `config/config.py:1217 class VisualCompactConfig`。
- **预算映射**：`runtime/builder.py:1029 _build_context_config()` 把 `ContextCompactConfig`(:1037) 映射为 AgentScope `ContextConfig`；`non_binding_limit = 2**63 - 1`，当 `ToolResultPruningConfig`(:1068) 启用时把 `tool_result_limit` 设为该值"关掉"上游二次裁剪，并让 `max_image_num` 让位给视觉压缩——与旧报告一致。
- **memory**：`agents/memory/base_memory_manager.py:63 class BaseMemoryManager(ABC)`（`working_dir/agent_id/context: MemoryBackendContext`，异步 auto-memory worker 队列）、注册表 `:843 MemoryBackendRegistry`（带实例租约）。**契约门面只有 30 行**：`memory/__init__.py` 仅重导出 `BaseMemoryManager / MemoryBackendRegistry / MemoryBackendContext / AutoMemorySearchOptions / NO_RELEVANT_MEMORIES / get_memory_manager_backend / memory_registry`。实现主力是 ReMe：`agents/memory/reme_light_memory_manager.py` + `reme_embedding.py / reme_reranker.py / reme_config.py / reme_inbox.py` + `proactive/`、`dummy.py`、`embedding_model.py`。注入靠 `agents/middlewares.py:94 class MemoryMiddleware`（而非塞 system prompt）。
- **持久记忆文件**：`agents/md_files/{zh,en,ru,id,qa,local}/`，每语言 8 个文件 `AGENTS/SOUL/PROFILE/MEMORY/BOOTSTRAP/HEARTBEAT/CONTACTS/MAIL_TRIAGE.md`；经 `runtime/prompt_contributors.py` 的贡献者进入 system prompt。
- **注意**：`src/qwenpaw/attachments.py` **在 main 上不存在**——旧报告 §5 "附件进入上下文"整段是本地新增。main 侧与媒体相关的可验证代码是 `agents/utils/image_freezing.py`、`hooks/request_setup/media_hook.py:20 class MediaProcessHook`、`runtime/builder.py` 的 offloader 构造。

---

## 6. 状态与持久化

- **Session**：`app/chats/models.py:97 ChatSpec` / `:219 ChatHistory`，由 `app/chats/manager.py:63 ChatManager` 管理；LLM 状态由 `app/chats/session.py:205 class SafeJSONSession` 落在 `sessions/{uid}_{sanitized_sid}.json`，用 `sanitize_filename()`(:31)、`get_path_lock`、`write_json_atomic_async`（import 于 :19-21）。
- **断点续跑**：`hooks/session/session_hook.py:35 SessionLoadHook`（PRE_AGENT_BUILD）/ `:79 SessionSaveHook`（POST_RESPONSE）；取消路径由 `runtime/runtime.py:237 _try_save_on_cancel()` 兜底。
- **Checkpoints（直接 shell 调 git）**：12 个文件 / 4,449 行。`checkpoints/repository.py:97 run_git()` 用 `subprocess.run(["git", ...])`（:74/:99/:141），`checkpoints/service.py:64 CheckpointService`，`checkpoints/models.py` 的 `CheckpointEntry`(:14) / `SnapshotResult`(:34) / `RestorePlan`(:45) / `RestoreResult`(:59) / `GcResult`(:74)，`checkpoints/runtime.py:24 Debouncer` / `:83 CheckpointRuntime`，策略 `checkpoints/policy.py:204 CheckpointPolicy`。
- **并发**：进程内 `asyncio.Lock` + 文件锁；`hub/` 侧是 SQLite + 进程锁。**main 上没有 `server/`、没有 `SQLRepository`/`PostgresRepository`、没有 worker 租约与 epoch fencing**——旧报告 §6 末段所述"完整的分布式任务队列语义"是本地新增。

---

## 7. 中间件与扩展点

两套正交机制（`runtime/phases.py` 与 `runtime/hooks.py` 的 docstring 都明确强调）：

| 层 | 基类 | 作用域 | 注册点 |
|---|---|---|---|
| 请求生命周期 hook | `runtime/hooks.py:100 HookBase` → `hooks/base.py:22 LifecycleHook` / `modes/base.py:112 ModeGatedHook` | `Runtime.run()` 8 阶段 | `workspace.plugins.hook_registry` |
| Agent 循环中间件 | `agentscope.middleware.MiddlewareBase` | 单次 reply | `AgentBuilder._build_middlewares` |

- **内建 hooks = 16 + 2 = 18**：`app/workspace/bootstrap_factory.py:94` 的主清单 16 个（`CronContext/CronMemoryIsolate/CronMemoryRestore/SessionLoad/SessionSave/Bootstrap/SkillEnv/SkillEnvCleanup/ContextVarsSetup/AgentContextVarsSetup/MediaProcess/ErrorNormalize/CancelCleanup/CheckpointQueryGate/CheckpointAutoSnapshot/MailF1Cleanup`），另由可选分支追加 `LangfuseTraceHook`/`LangfuseTraceCleanupHook`(:123-133)。全部 hook 类见 `hooks/*/*.py`。
- **中间件顺序**（`runtime/builder.py:1336 _build_middlewares()`，洋葱由外到内）：`ToolResultPruningMiddleware` → `ToolCoordinatorMiddleware` → memory 中间件 → `LangfuseToolSpanMiddleware` → 插件中间件 → `VisualCompressionMiddleware`（最内）。
- **插件体系**：`plugins/architecture.py:12 PluginType`（tool/provider/hook/command/channel/memory/frontend/…）、`plugins/loader.py:204` 负责发现（目录 + `importlib.metadata`）、依赖安装（`_find_uv()`:833、`plugin_install_lock`(:459-461)）、`_validate_entry_points()`(:475)、失败回滚 `_cleanup_failed_load()`(:643)；`plugins/registry.py` 为单例注册中心；**对外 API `plugins/api.py` 提供 17 个 `register_*` 方法**（旧报告写 16，少数了 `register_skill_provider`:1361）：`register_memory_backend/provider/startup_hook/shutdown_hook/uninstall_hook/workspace_created_hook/http_router/control_command/middleware/channel/tool/slash_command/mode/runtime_hook/agent_stop_handler/prompt_section/skill_provider`。
- **governance**：`governance/policy.py:639 GovernancePolicy`，动作枚举 `:39 GovernanceAction = ALLOW/DENY/ASK/SANDBOX_FALLBACK`；`evaluate()`(:696) 的 docstring 是 **Phase 0（工具类型）→ Phase 1（深度安全扫描）→ Phase 2（规则 first-match-wins）→ Phase 3（fallback + execution_level 阈值）**，旧报告漏了 Phase 3 且把 shell 危险关键字单列为 1.5。唯一入口 `governance/resource_governor.py:44 class ResourceGovernor` 的 `assert_policy()`(:220) / `compile_sandbox_config()`(:328)。审计 `governance/audit.py:91 class AuditLog`（`~/.qwenpaw/audit.db` 单例，`MAX_RECORDS = 100_000`(:108) 触发 `PURGE_COUNT = 10_000`(:109) 的 `_auto_purge()`(:380)，按 ts/workspace/agent/tool 建索引）。`runtime/tool_guard.py:13 GuardedFunctionTool` **在 main 上仍然存在**（427 行），与 `governance/tool_adapter.py:108 PolicyGuardedTool` 并存。
- **security**：`security/tool_guard/engine.py:59 ToolGuardEngine` + guardians（`BaseToolGuardian`:17、`FilePathToolGuardian`:313、`RuleBasedToolGuardian`:713、`SharedSafetyToolGuardian`:682、`ShellEvasionGuardian`:539）；`security/skill_scanner/scanner.py:76 SkillScanner`（+ `analyzers/pattern_analyzer.py`、`scan_policy.py`）；`security/secret_store.py` 用 Fernet（AES-128-CBC+HMAC），主密钥优先 OS keyring、回退 `SECRET_DIR/.master_key` 0600，密文带 `ENC:` 前缀。
- **sandbox**：`sandbox/config.py:56 SandboxMode`（`SEATBELT/BUBBLEWRAP/LANDLOCK/WINDOWS/NONE`）、`create_sandbox()`、`probe_sandbox_support()`、`report_unenforced_config()`(:323)。生命周期确认为 **per-tool-call**：`agents/tools/shell.py:1012` 与 `agents/context/scroll/repl.py:234` 都是 `async with create_sandbox(...)`。
- **drivers**（MCP/外部能力的可治理调用层）：`drivers/contracts.py:62 CredentialRef` / `:104 DriverCard`，`drivers/manager.py:48 DriverManager`，`drivers/handler.py:46 _resolve_driver_execution_level()` / `:105 DriverHandler(ABC)`，`drivers/policy_types.py`，`drivers/approval.py`，`drivers/handlers/{mcp,mcp_stateful_client,mcp_streamable_http}.py`。人类审批在 `app/approvals/service.py:115 ApprovalService`。

---

## 7b. config/ 与 exceptions.py（基座层）

- `config/` 共 5,650 行 / 23 处 `except Exception`，只有 5 个模块：`config.py`（3,956 行，配置模型 + 加载 + 缓存 + 指纹 + 迁移全塞一处，**75 个顶层 class**）、`context.py`（**10 个 `ContextVar(`**，是请求上下文注入深层工具的主要隐式通道，见 `hooks/request_setup/contextvars_hook.py:47 ContextVarsSetupHook` 与 `hooks/request_setup/agent_context_hook.py:26 AgentContextVarsSetupHook`）、`timezone.py`（其 docstring 自承存在循环依赖问题）、`utils.py`、`__init__.py`。
- **配置层反向依赖运行时层**：`config/config.py:2695 _default_builtin_tools()` 用 `importlib.import_module` 刻意规避 `agents.tools → … → config` 的静态环（旧报告此处判断成立）。分层图上 L0 依赖 L3，是真实倒挂。
- `exceptions.py`（824 行）是清晰的三层异常树：`AppBaseException`(:10) → `ConfigurationException`(:30) / `AgentRuntimeErrorException`(:60) / `AgentException`(:245) / `SandboxViolationError`(:456)；模型侧细分 `ModelExecutionException`(:74)、`ModelTimeoutException`(:92)、`UnauthorizedModelAccessException`(:110)、`ModelQuotaExceededException`(:128)、`ModelContextLengthExceededException`(:146)、`ModelNotFoundException`(:206)、`RateLimitExceededException`(:224)、`ProviderError`(:253)、`ModelFormatterError`(:264)；框架侧 `AgentStateError`(:309)、`HookCycleError`(:336)、`SkillsError`(:325) 及其子类。`constant.py`（461 行）承载路径/环境变量/超时常量与 `EnvVarLoader`。**main 上 `constant.py` 没有 `QWENPAW_SERVER_MODE` 分支**（那是本地为 `server/` 新增的）。

---

## 8. 可观测性

- **Trace**：`observability/langfuse.py` 完全可选——`_langfuse_available()`(:36) 用 `importlib.util.find_spec("langfuse")` 探测，`is_langfuse_enabled()`(:40) 还要求 `LANGFUSE_SECRET_KEY`；`current_generation_kwargs()`(:93)、`agent_trace_scope()`(:116)、`tool_span()`(:190)。整个 `observability/` 只有 2 个文件 / 239 行。挂载点 `hooks/observability/langfuse_hook.py:21 LangfuseTraceHook` / `:67 LangfuseTraceCleanupHook` 与 `agents/middlewares.py:951 LangfuseToolSpanMiddleware`。**`providers/tl_wire_log.py` 在 main 上不存在**。
- **Metrics**：`token_usage/manager.py:97 TokenUsageManager`；`agent_stats/service.py:272 AgentStatsService` **确实直接扫 session JSON 文件**——`_should_skip_by_mtime()`(:31)、`_extract_session_messages()`(:52)、`_should_skip_by_content_range()`(:68)、`_extract_turn_usage_tokens()`(:114)、`_extract_turn_cache_tokens()`(:135)、`_process_session_file()`(:159)，用 mtime + 内容范围双重跳过。
- **实时观测**：`runtime/envelope.py:83 Envelope` 把 AgentScope 事件翻译为 SSE（含 heartbeat）；工具 `GET /output` 由 coordinator 的 `_completed_cache`（60s/50 条）支撑。

---

## 9. 【单列】官方 main 的"多用户 / 服务化"能力

**结论：main 有多用户能力，但它不在 `app/` 也不在 `server/`（`server/` 不存在），而在 `hub/`；`app/` 本身是明确的单账号设计。** 旧报告在此处被本地改造误导：它把本地新增的 `server/` 当成"多用户侧"，又完全遗漏了官方真实的 `hub/`。

**(a) `hub/` 是 main 上真实存在的多租户控制面**（24 个模块 / 7,475 行，14 处 `except Exception`）：

- 定位由 `hub/__init__.py` 声明为 "QwenPaw Hub runtime control plane"，导出 `RuntimeRecord/RuntimeSpec/RuntimeState`。HTTP 控制面 `hub/control_app.py:131 create_hub_app()`（1,598 行），路由包括 `/api/hub/runtimes`（创建/启动/停止/重建/禁用/删除）、`/api/hub/admin/users`、`/api/hub/admin/settings`、`/api/hub/admin/audit`、`/api/hub/credentials`、`/api/hub/images`、以及 `/api/auth/{register,login,verify}`；`@app.websocket("/api/{path:path}")`(:1423) 做反向代理。
- **身份与角色是真正的多用户**：`hub/auth.py:31 class HubUser`（`user_id/username/role/disabled/token_version/preferences`，`is_admin` 属性），`hub/auth.py:67 class HubAuthService`（PBKDF2 600,000 轮、`_TOKEN_TTL_SECONDS = 7*24*3600`、版本化 HMAC bearer token、`status()` 返回 `mode="hub"` 与 `bootstrap_required`），管理员 API 模型 `hub/api_models.py:36 AdminUserCreateBody` / `:42 AdminUserPatchBody`。租户与账户持久化在 SQLite（`hub/database.py:62 ensure_tenant`、`HubExtensionStore`(:141)）。凭据按租户隔离：`hub/credentials.py:56 TenantCredentialVault`。
- **隔离靠"每租户一个运行时进程/容器"，而非进程内多会话**：`hub/models.py:30 RuntimeSpec`（`tenant_id`、`owner_user_id`）与 `:41 RuntimeRecord`（含 `working_dir/secret_dir/backup_dir/port/pid`、`RuntimeState`(:12)、`RuntimeStartPolicy`(:22)）。编排在 `hub/service.py:49 class RuntimeService`（`create/list(owner_user_id)/start/stop/restart/rebuild/status/delete`）。两种 provisioner：`hub/local_provisioner.py:43 LocalProcessRuntimeProvisioner` 与 `hub/docker_provisioner.py:41 DockerRuntimeProvisioner`；进程隔离抽象 `hub/process_isolation.py:50 ProcessIsolator(ABC)`（`:132 LinuxBubblewrapIsolator`，另有 Windows AppContainer 路径）。配置模型 `hub/config.py:245 HubConfig` / `:42 RegistrationConfig` / `:62 RateLimitConfig` / `:73 AccessSecurityConfig`。反向代理与限制在 `hub/websocket_proxy.py` / `hub/proxy_limits.py`。CLI 入口 `cli/hub_cmd.py`。

**(b) `app/` 是单账号**：`app/auth.py` 的模块 docstring 第 10-13 行直言 "Single-user design: only one account can be registered. If the user forgets their password, delete `auth.json` from `SECRET_DIR` and restart the service to re-register."；`register_user()`(:363) 的 docstring 是 "Register the single user account"，实现里 `if data.get("user"): return None`（:378-380，注释 "Only one user allowed"）。登录**默认关闭**，须 `QWENPAW_AUTH_ENABLED` 真值才启用（`is_auth_enabled()`:340，`AuthMiddleware`:708）。凭据是 `SECRET_DIR/auth.json` 里的加盐 SHA-256 + 自签 JWT（`_hash_password`:104 / `create_token`:137 / `verify_token`:177），支持 `allow_no_auth_hosts` + loopback 双层校验（`_should_skip_auth`:735）。
- 面向 hub 托管场景，`app/auth.py` 另有一个**机器边界令牌**：`app/auth.py:784 class RuntimeBoundaryMiddleware`，读 `QWENPAW_RUNTIME_INTERNAL_TOKEN`（:45）、比对 `x-qwenpaw-runtime-token` 头（:46），用 `hmac.compare_digest`；未通过时 HTTP 返 401、WebSocket 关 4401。挂载于 `app/_app.py:737`，令牌由 `hub/control_app.py:111/1220/1294/1455` 与 `hub/{local,docker}_provisioner.py` 注入。**这是"每运行时一个共享密钥"的进程间信任，不是每终端用户身份**——hub 终止用户认证后再反代。

因此口径应为：**main = 单账号运行时（`app/`）+ 多租户控制面（`hub/`），多租户通过进程/容器级隔离实现**。本地 `multisync` 的 `server/`（`server/api.py`、`server/repository.py`、`server/storage/repository.py` 1,499 行、6 个 SQL migration、worker 租约）是**另一套、进程内数据库多用户**的设计，main 上完全没有。

---

## 10. 分层边界与坏味道（量化）

### 10.1 边界清晰之处（main 可验证）
1. `tool_calls/` 与其余模块解耦：仅通过 `MiddlewareBase.on_acting` 接触，1,698 行、8 处 `except Exception`。
2. `loop/` 的停止门引擎：`loop/gates/base.py:65 StopGate(ABC)`、`loop/gates/handler.py:28 StopHandler`、`loop/catalog.py:154 GateCatalog`（校验入口 `validate_params()`:164 / `validate_exclusive_groups()`:175），`loop/catalog.py:203 _entries()` 恰好 **7 种内置 gate**（`iteration / doom_loop / token_budget / timeout / tool_call_budget / qualitative_rubric / completion_rubric`），每个带 pydantic 参数模型（即 JSON Schema 元数据），`qualitative_rubric` 与 `completion_rubric` 共享 `exclusive_group="completion_rubric"`；`loop/compiler.py:11 compile_loop_mode()` 做"校验参数 → 校验互斥组 → 构造 `ConfiguredGate`"的原子编译。`loop/` 仅 2,327 行 / 4 处 `except Exception`，是全项目最干净的设计。
3. 扩展面收敛到 `PluginApi`(17 个 register_*)、`PromptContributor`、`HookBase`、`AgentMode`（`modes/base.py:30`）四个基类。
4. 配置→运行时单向映射：`AgentProfileConfig` 只声明，`AgentBuilder` 负责解释。
5. `hub/` 与 `app/` 的职责分离干净（控制面 vs 运行时），通过 `RuntimeBoundaryMiddleware` 单一契约通信。

### 10.2 高耦合与坏味道（量化数字）
1. **超长文件**（`find ... | wc -l` 实测）：`agents/tools/deprecated_browser/browser_control.py` **5,629 行**、`config/config.py` **3,956 行（75 个顶层 class）**、`app/channels/dingtalk/channel.py` 3,845、`app/channels/matrix/channel.py` 3,525、`sandbox/windows_unelevated_sandbox.py` 2,986、`sandbox/windows_elevated_sandbox.py` 2,928、`app/channels/feishu/channel.py` 2,823、`app/mail/monitor.py` 2,485、`app/channels/qq/channel.py` 2,445、`agents/model_factory.py` 2,414、`agents/skill_system/hub.py` 2,388、`app/routers/workspace.py` 2,317。
2. **目录级体量与异常密度**：`app/` 91,486 行 / **627** 处 `except Exception`；`agents/` 85,591 行 / 448；`providers/` 14,522 / 47；`sandbox/` 9,732 / 8；`drivers/` 7,947 / 25；`security/` 6,821 / 36；`plugins/` 5,821 / 26；`config/` 5,650 / 23；`governance/` 5,518 / 21；`checkpoints/` 4,449 / 12；`runtime/` 8,422 / 43；`hub/` 7,475 / 14；`tool_calls/` 1,698 / 8；`loop/` 2,327 / 4。
3. **异常吞没**：全仓 **1,598 处 `except Exception`**（旧报告 1,625）、**64 处 `except BaseException`**、**142 处 `broad-except` 抑制**。`bootstrap_factory.py` 与 `builder._build_middlewares` 里可选组件全部 `try/except` 包裹并只写 debug 日志——启动永不失败，也永不告警。
4. **复杂度是被承认而非解决的**：**697 处 `pylint: disable`**（旧报告 701）、**296 处 `too-many-*` 抑制**（旧报告 200，明显低估）。`governance/policy.py:696 evaluate()` 挂着 `too-many-return-statements, too-many-branches`，`runtime/builder.py` 的 `_build_middlewares`/`build` 同为重灾区，都靠注释豁免。
5. **隐式全局状态**：`ProviderManager.get_instance()`、`TokenUsageManager.get_instance()`、`AuditLog.get_instance()`（`governance/audit.py:120`）、`ModelCapabilityCache.get_instance()`(:67)、`PluginRegistry` 单例，以及 `config/context.py` 的 **10 个 `ContextVar(`**（旧报告称"15+"）。`ContextVarsSetupHook` 把 workspace/session/toolkit 等灌进 ContextVar，是深层工具获取上下文的**主要隐式通道**。
6. **错误文案启发式**：`agents/react_agent.py` 用 8 条正则 + 5 个 `_is_*_error` 函数猜上游语义（§4），与"配置 + 探针结果"两条确定性来源并存，属脆弱面。
7. **核心循环不在自己手里**：ReAct 主循环、消息模型、Toolkit、Formatter 全部来自 `agentscope`，且被钉到 `==2.0.7.post1`；QwenPaw 通过继承覆写与"关掉上游逻辑"的方式改造（`model.max_retries = 0`、把 `tool_result_limit` 与 `max_image_num` 设成 `2**63-1`），升级成本高。

---

## 11. 一句话总结

官方 main 是**「AgentScope 内核 + 单账号产品化外壳（`app/`）+ 治理层 + 多租户控制面（`hub/`）」**的四段式架构：真正的 ReAct 循环与模型/消息抽象外包给 `agentscope==2.0.7.post1`；QwenPaw 自己的产出集中在**请求编排（`runtime/` 8 阶段 + 3 个固定步骤）、工具生命周期（`tool_calls/` 四档超时 + 前后台双 deadline）、停止门引擎（`loop/` 7 gate + 互斥组编译）、治理与沙箱（`governance/`+`security/`+`sandbox/`）、插件 API（`plugins/api.py` 17 个 register_*）、多租户编排（`hub/` 每租户一进程/容器 + 反向代理）**；而它的复杂度来自"支持一切"的产品定位（35 家 provider、18 个消息渠道、3 条上下文策略、5 种沙箱模式、18 个 hook、978 个文件 33.5 万行）与对上游库的深度改写。
