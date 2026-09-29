> ⚠️ **基线错误警示（后续已修正）**：本报告分析的是本地 `multisync` 分支 = `origin/main` + 4 个本地提交，
> **不是 QwenPaw 官方代码**。§0 的 `office/` 归因、§8 整节 TL 协议、§9 的部分条目属本地改造或工作区未跟踪残留。
> 请以 `qwenpaw-main-extensions.md` 为准。

# QwenPaw 扩展与能力体系分析报告

> 目标仓库：`/Users/yangxuezhen/git/QwenPaw`（只读分析，未做任何修改）。
> 分析范围：`src/qwenpaw/{plugins,market,hub,tool_calls,office,pawapp,modes,agents,providers,drivers,runtime,governance,security}`、仓库根 `plugins/`、`packages/qwenpawmail-mcp`、`docs/skills/`、`test-tools/`。
> 已跳过 `.venv/`、`node_modules/`、`__pycache__/`、`.git/`。

## 0. 仓库地图与一个重要发现

QwenPaw 的"扩展体系"并不是一层，而是**七套并行机制**：插件（`plugins/`）、下游技能（`agents/skill_system/` + `agents/skills/`）、技能市场（`market/`）、工具注册（`runtime/tool_registry.py` + `governance/tool_registry.py`）、MCP 驱动（`drivers/`）、PawApp（`pawapp/`）、模式（`modes/`），再加一个**名字极易混淆**的 `hub/`。

- `hub/` **不是**包仓库。它是多用户运行时装管平台（QwenPaw Hub）：`hub/config.py` 的 `HubConfig`/`DockerRuntimeConfig(image="docker.io/agentscope/qwenpaw")`/`ControlPlaneConfig`/`RateLimitConfig`，`hub/service.py` 的 `RuntimeOrchestratorService`，`hub/registry.py` 的 `RuntimeRegistry`（SQLite、tenant），配套 `docker_provisioner.py`、`local_provisioner.py`、`windows_reverse_tunnel.py`、`websocket_proxy.py`、`process_isolation.py`。"技能 hub"在 `agents/skill_system/hub.py`，两者同名不同物。
- **`src/qwenpaw/office/` 的源码不存在**：该目录只剩 `__pycache__/{api,bundle,config,models,runtime,service,storage,tools,verification}.cpython-312.pyc`，全仓库 `grep "qwenpaw.office"` 无任何引用，`git log -- src/qwenpaw/office` 也无记录。Office 能力实际落在 **内置技能**（`agents/skills/docx-*|pptx-*|xlsx-*`）+ LibreOffice/Node 脚本上（详见 §7）。

---

## 1. 插件系统

### 1.1 发现与安装
- 插件目录：`config/utils.py:919 get_plugins_dir()` → `constant.PLUGINS_DIR`。加载入口 `app/_app.py:407 PluginLoader(plugin_dirs)`（CLI 侧 `cli/channels_cmd.py:641`）。
- 发现：`plugins/loader.py:272 PluginLoader.discover_plugins()` 只扫一层子目录，**必须含 `plugin.json`**；`_is_disabled_plugin_dir()`（loader.py:141）跳过隐藏/禁用目录。
- 元数据：`plugins/architecture.py` 的 pydantic 模型 `PluginManifest`（`extra="ignore"`）字段为 `id/version/name/description/description_i18n/author/entry{frontend,backend}/dependencies/min_version/max_version/qwenpaw_version{min,max}/meta/plugin_type`。`PluginType` 枚举给出全部扩展类别：`tool / provider / hook / command / channel / memory / frontend / app / general`；缺 `type` 时由 `_infer_type_from_meta()` 兼容推断。`_normalise_input()` 还兼容三种历史写法：i18n 对象形式的 `name/description`、顶层 `entry_point`、`max` 缺省时按 `{major}.{minor+1}.0` 推导。
- 版本门禁：`QwenPawVersionConstraint` 语义是 `>=min, <max`；`_check_version_compatibility()`（loader.py:331）不通过时**不报错**，而是写入 `PluginRecord(enabled=False, diagnostics=[...])`。
- 依赖：`dependencies`（pip 需求串）由 `_find_unsatisfied_dependencies()`（loader.py:388）/`_ensure_dependencies_installed` 处理，优先用 `uv`（`_find_uv` loader.py:833），冻结桌面版走 `_install_requirements_frozen()`（loader.py:1027）；跨进程互斥靠 `plugins/install_lock.py` 的 `fcntl.flock`/`msvcrt.locking` 咨询锁（模块 docstring 记录了 issue #5550 的 `pip` 并发自放大事故）。
- 安装：`loader.py:1093 load_plugin_from_path()` —— 复制到 `install_dir`、装依赖、加载；`force=True` 先卸载再装；`pawport_owner` 用 `.qwenpaw-pawport.json` 标记、失败时 `_remove_incomplete_pawport_plugin()` 回滚。HTTP 面在 `app/routers/plugins.py`（`install_plugin`/`upload_plugin`/`uninstall_plugin`/`get_plugin_catalog`），CLI 为 `qwenpaw plugin install|uninstall|validate`。

### 1.2 加载与隔离
- `loader.py:691 load_plugin()` → `_load_plugin_unlocked()`：按 `entry.backend` 用 importlib 执行模块，**模块必须导出 `plugin` 实例**（否则 `AttributeError`，校验器 `plugins/validation.py:validate_plugin_module()` 复刻同一语义）；只有 `entry.frontend` 的插件允许"前端-only"加载。
- 隔离：`plugins/module_isolation.py` 为每个插件建立私有顶层命名空间 `plugin_<id>`：`build_plugin_builtins()` 覆盖 `__import__`，使 `import utils` / `from models.x import y` 这类**裸绝对导入**优先解析到插件自身目录（`plugin_<id>.utils`）；`PluginNamespaceFinder` 以 `sys.meta_path` hook 保证嵌套/惰性导入同样被重定向（注释标注修复 #6683 的"首个插件抢注 utils 就污染所有人"问题）。文件头 26–53 行**主动列出 6 条已知限制**（`importlib.import_module` 绕过、插件自加 `sys.path` 项被清扫、`__builtins__` 是快照、pickle 兼容性、名字解析结果终身缓存、裸名模块从 `sys.modules` 移除）。
- 卸载：`loader.py:1478 _cleanup_plugin_tools()` 基于 `_TOOL_PLUGIN_OWNERS` 反查归属，并明确注释"manifest 里的名字只是候选，**绝不作为删除授权**"（防止恶意 manifest 卸载别人的工具/内置工具）；`plugins/registry.py:950 PluginRegistry.unregister_plugin()`、`api.py` 的 `release_tool_ownership_for_plugin()`。

### 1.3 可扩展点（`plugins/api.py`，1695 行）
`PluginApi` 提供 18+ 个注册方法：`register_tool(810)`、`register_memory_backend(347)`、`register_provider(395)`、`register_startup_hook(441)`、`register_shutdown_hook(473)`、`register_uninstall_hook(505)`、`register_workspace_created_hook(548)`、`register_http_router(590)`、`register_control_command(621)`、`register_middleware(644)`、`register_channel(679)`、`register_slash_command(954)`、`register_mode(1013)`、`register_runtime_hook(1056)`、`register_agent_stop_handler(1096)`、`register_prompt_section(1150)`、`register_skill_provider(1361)`。宿主侧注册表 `plugins/registry.py` 用多个 dataclass 承载（`ProviderRegistration/HookRegistration/ControlCommandRegistration/MiddlewareRegistration/ChannelRegistration/HttpRouterRegistration/PromptSectionRegistration`），`PluginRegistry.__new__` 为单例。可见扩展面覆盖：**工具、LLM provider、钩子（启动/关闭/卸载/工作区创建/运行时/停止）、中间件、HTTP 路由、控制与斜杠命令、消息通道、记忆后端、提示词段落、技能供给、模式、前端 JS bundle**。

### 1.4 示例插件各自演示什么
| 示例 | 演示能力 |
|---|---|
| `plugins/middleware-demo/{tracing,thinking-log}-middleware` | `register_middleware(factory, priority=N)`；工厂签名 `(ctx: HookContext, agent_config) -> MiddlewareBase|None`；`tracing` 用 `QWENPAW_TRACE` 环境变量做条件激活、`on_acting` 计时，`thinking-log` 在 `on_reasoning` 打印思维流；priority 越小越外层；**不自动加载**，需 `qwenpaw plugin install` |
| `plugins/tool/{qwen-image,wan27,gpt-image2}` | "工具插件"标准形态：`meta.tools[]` 声明工具名/描述/图标/`requires_config`/`config_fields`（type、default、options、help），UI 自动生成配置表单 |
| `plugins/channel/azure_bot` | `type:"channel"` + 依赖 `aiohttp/PyJWT/msal`，Azure Bot Framework 通道（Teams/Slack/Web Chat） |
| `plugins/memory/{powercontext,adbpg}` | `type:"memory"` + `entry.frontend` 前端 bundle + `meta.memory_backends[]`；`pack_exclude` 裁剪打包内容 |
| `plugins/bundle/qwenpaw-pet` | 复杂 bundle：后端生命周期事件 + 桌面进程（`pyside6-essentials`）+ `frontend/dist/index.js` + `meta.desktop_url` |
| `plugins/bundle/cloudpaw` | HTTP 路由（`routers/`）+ 工具（`tools/a2a_*.py`）+ 钩子（`hooks.py`）+ **Markdown agent 定义** `agents/{orchestration,verifier,executor}/{zh,en}/{SOUL.md,PROFILE.md}` |
| `plugins/bundle/computer-use` | `meta.tools[]` + 原生桌面运行时（Windows/macOS 授权应用） |
| `plugins/bundle/omp_workflows` | `type:"bundle"`，提供 Autopilot/Ralph/UltraQA/Ultrawork/Team **模式** |
| `plugins/apps/{agent-kanban,qwenpaw-data,qwenpaw-creator}` | `type:"app"`，基于 PawApp SDK；`meta.pawapp{icon,entry_page,launch_scope,category}`、`meta.permissions{chat,storage,network}`、`meta.runtime_dependencies`（python_packages / jq / ffmpeg / libreoffice）、`pack_requires`、`pack_requires_hint` |
| `plugins/bundle/chrome` | `capabilities` 声明 + 前后端入口 |

---

## 2. 技能（skill）系统

### 2.1 格式与解析
技能是 **Markdown + YAML frontmatter**（不是 JSON）。样例 `src/qwenpaw/agents/skills/docx-zh/SKILL.md`：

```yaml
---
name: docx
description: "当用户需要创建、读取、编辑或处理 Word 文档（.docx）时…"
license: Proprietary. LICENSE.txt has complete terms
metadata:
  builtin_skill_version: "1.1"
---
```

解析在 `agents/skill_system/store.py`：对外的 `frontmatter.loads` 路径是 `read_frontmatter_safe_from_path()`(195)/`_read_frontmatter()`(218)，失败时回退 `{"name": <dirname>, "description": ""}`；严格的 `load_skill_frontmatter_from_dir()`(274) 用 `_read_bounded_frontmatter_bytes()`(246) 先做**有界**头解析（`_MAX_FRONTMATTER_LINES=4096`、`_MAX_FRONTMATTER_BYTES=256*1024`、多编码回退 `_FRONTMATTER_ENCODINGS`），缺头或非法 YAML 抛 `SkillsError`。运行时身份用**目录名**而非 frontmatter `name`（`SkillInfo` docstring 明确："frontmatter can drift while the on-disk workspace identity must remain stable"）。

### 2.2 发现、池与注入
- 目录层次：内置 `src/qwenpaw/agents/skills/<name>-{en,zh}/`（28 个技能 × 中英）、共享池 `get_skill_pool_dir()`(store.py:84)、工作区 `<ws>/skills` `get_workspace_skills_dir()`(91)；清单为 `skill-pool-manifest.v1`（store.py:64）与工作区 manifest，条目字段含 `enabled/channels/config/preload/source`。
- 语言变体：`BUILTIN_SKILL_LANGUAGES=("en","zh")`、`_BUILTIN_SKILL_DIR_RE`（registry.py:69-75）、`_select_builtin_variant()`、`import_builtin_skills()`(710)、池/工作区清单 reconcile（994/1058）与内置更新（`update_single_builtin` 1668，带回滚）。
- **前置条件即能力校验**：`SkillRequirements{require_bins, require_envs, require_mcps}`（models.py:63）；`check_skill_dependencies()`（registry.py:1224）逐项校验 PATH 上的 CLI、环境变量（`_skill_env_key` 加前缀）、以及 MCP——`card_paths_for_name(workspace/"drivers", name)` 必须存在 `card.protocol == "mcp"` 且 `card.enabled`。**技能因此可以声明对 MCP server 的依赖**，这是技能与 MCP 的显式交叉点。
- 生效与通道：`resolve_effective_skills(workspace_dir, channel)`（registry.py:1261）= manifest `enabled` ∧ `channels` 命中所属通道（`ALL_SKILL_ROUTING_CHANNELS`：console/discord/telegram/dingtalk/feishu/imessage/qq/mattermost/wecom/mqtt）∧ 前置条件满足；任一不满足只记 error 并**跳过该技能，但保留 enabled 状态**，下轮重试。
- 注入提示词：两条路径。(a) 预加载——`select_preload_skills()`(registry.py:1321) 取 manifest `preload: true` 的技能，`runtime/prompt_contributors.py` 的 `PreloadedSkillsContributor`（`name="preloaded_skills"`, `priority=95`）把整份 SKILL.md 包进 `<preloaded-skills><skill><name>/<description>/<dir>+markdown</skill>`。(b) 按需——斜杠命令兜底 `runtime/builtin_commands.py:648-790` 把技能正文作为 `<skill>…</skill>` 块**追加到用户消息尾部**（"typed text stays"），前端展示时由 `app/chats/utils.py:473-500` 的正则剥掉。
- 与插件/工具的边界：技能=提示词 + 脚本 + references，**本身不注册任何代码**；插件若要供技能，走 `PluginApi.register_skill_provider(skills_dir, enabled_by_default, channels)`（api.py:1361）——宿主负责复制进工作区、reconcile manifest、装/卸时按 `source` 清理，并对新工作区挂 `workspace_created` 钩子。反向边界：技能可通过 `require_mcps` 依赖 MCP，工具可通过 `ToolDescriptor.requires_skills` 依赖技能（§3）。

### 2.3 技能市场
有，且是**多源聚合**：`market/service.py:23 list_providers()` + `providers/__init__.py` 的 `PROVIDERS = {qwenpaw, clawhub, modelscope, aliyun}`；`search_market()` 并发按 provider 分页、`_run_one()` 做 category 路由与 `_supported_kwargs()` 能力降级，返回 `MarketResult/MarketSearchError`。安装侧 `agents/skill_system/hub.py` 支持从 clawhub.ai、skills.sh、skillsmp、LobeHub（`market.lobehub.com/api/v1/skills/<id>/download`）、ModelScope、官方 QwenPaw、阿里云 AgentExplorer、GitHub（`owner/repo` 或 `…/tree/<branch>/<path>`）拉取（`_extract_github_spec` 1128 等一组 extractor），带 httpx 重试/退避、GitHub API 缓存（TTL 300s）、`SKILL_PACKAGE_MAX_ENTRIES=4096`、`SKILL_PACKAGE_MAX_BYTES=200MB`、路径段安全化（`_safe_path_parts`/`_sanitize_skill_dir_name`）。HTTP 面：`app/routers/skills.py` 的 `GET /hub/search`、`POST /hub/install/start`、`GET /hub/install/status/{id}`、`POST /hub/install/cancel/{id}`。

---

## 3. 工具注册中心与内置工具

- 声明模型：`runtime/tool_registry.py:55 ToolDescriptor(name, func, enabled_by_default, requires_modes, requires_skills, requires_features, requires_sandbox, async_execution, description, metadata, governance, ui)`；配套 `ToolGovernanceSpec(tool_type, target_param, pattern_param, policy_name, fail_without_sandbox, default_policy, policy_reason)` 与 `ToolUISpec(description, icon, display_to_user)`。`ToolRegistry.register/register_many/filter/names/default_enabled_names`。
- 批量注册：`agents/tools/__init__.py:4-14` 说明约定——**装饰 `@tool_descriptor(...)` + 在本文件 import 一次**即完成注册，`__all__` 自动生成，`discover_builtin_tool_funcs()` 返回全集，无需手工列表或目录扫描；`runtime/tool_registry.py:193-215 get_builtin_tool_funcs()` 以模块前缀 `qwenpaw.agents.tools.` 过滤。装配点在 `app/workspace/bootstrap_factory.py:39-44` → `workspace.py:291/302`。
- 内置工具清单（据 `agents/tools/__init__.py` 的 import 与各文件 `@tool_descriptor`）：文件 `read_file/write_file/edit_file/append_file`、检索 `grep_search/glob_search/ast_search`、执行 `execute_shell_command/run_tool_batch`、网络 `web_search/web_fetch`、媒体 `view_image/view_video/desktop_screenshot/send_file_to_user`、时间 `get_current_time/set_user_timezone`、计量 `get_token_usage`、多 agent `list_agents/chat_with_agent/submit_to_agent/check_agent_task/spawn_subagent/delegate_external_agent`、迁移一组 `migration_compat_*`、邮件 `activate_f1_exploration_mode`，另有 `browser.py`（统一浏览器 beta）与 `deprecated_browser/browser_control.py`、`lsp_tool.py`。注意 docstring 明确 `execute_python_code/view_text_file/write_text_file` **故意不注册**（`react_agent` 不注册它们，仅保留在 `security/tool_guard` 白名单兼容层）。
- 权限/审批与工具的绑定：`governance/tool_registry.py` 的 `ToolRegistry.register(..., sandbox_required=)`、`ALLOWED_TOOL_TYPES={"file","network","shell","internal"}`、`ALLOWED_DEFAULT_POLICIES={"allow","ask","deny",""}`、`register_tool_governance()`、`snake_to_pascal()`（把 `read_file` 映射到 UI 规则名 `ReadFile(**)`）、`requires_sandbox()` 与 fail-closed 标志；判定层在 `security/tool_guard/engine.py:59 ToolGuardEngine.guard()` + `guardians/{shell_evasion_guardian,file_guardian,rule_guardian}.py`。
- 调用生命周期（`tool_calls/`）：`ToolCoordinator.execute()`、`ToolCallEntry/ToolCallStatus`、`ToolCoordinatorMiddleware(MiddlewareBase)`、`ToolHookRegistry`，以及超时/卸载策略常量 `COORDINATOR_OWNED_EXEC_TIMEOUT_SECS`、`OFFLOAD_TIMEOUT_RATIO`、`arm_kill_deadline()`、`cancellable_wait()`。
- 插件工具（`api.py:810 register_tool`）：先 `_claim_tool_ownership()` + `_register_to_governance()`（**fail closed：治理注册失败就不暴露工具**，注释指向 #6114），再写入 `qwenpaw.agents.tools` 与 `__all__`，生成 `BuiltinToolConfig`（**默认 disabled，用户显式开启**），再 bridge 到运行时，并通过 startup hook 延迟执行。

---

## 4. MCP 集成

QwenPaw **只做 MCP 客户端**，自身不暴露 MCP server（全仓库 `FastMCP/@mcp.tool` 仅出现在 `packages/qwenpawmail-mcp`）。客户端实现在 `drivers/` 抽象层：

- 入口与校验：`drivers/handlers/mcp.py:56 MCPDriverHandler` 从 endpoint 取 `transport`（缺省 `"stdio"`）；stdio 分支读取 `command`/`args`/`env` 构造 `StdioServerParameters`（mcp.py:75-84），否则读 `url`（97-110）。`validate_mcp_endpoint()`（mcp.py:313）强制：stdio 必须有非空 `command` 与 list 型 `args`；传输只允许 `{"streamable_http","sse"}` 且 `url` 必填。工具名净化 `_sanitize_tool_name()`(463)/`_sanitize_tool_namespace()`(477)，并按 `_mcp_tool_to_capability()`(385) 转成 capability。
- 连接实现：`drivers/handlers/mcp_stateful_client.py` 的 `_MCPClientMixin`(156)、`StdIOStatefulClient`(860)、`HttpStatefulClient`(967，支持 `streamable_http` 与 `sse`，调用 `mcp.client.stdio.stdio_client` / `mcp.client.streamable_http.streamable_http_client`)，含 `connect/reload/list_tools/call_tool/close`、lifecycle task 回收、`_SessionGoneError`、熔断（`circuit_open`）、stdio 子进程退出与 HTTP `post_writer` 静默死亡两类传输故障的判别（761-800），能力列表带 10s TTL 缓存（`_CAPABILITY_CACHE_TTL_SECONDS`）。
- 配置格式与存储：不是简单的 `mcpServers` JSON，而是 **DriverCard**（`drivers/contracts.py`）：`{transport: "stdio"|"streamable_http"|"sse", command, args, env}` 或 `{transport, url, headers}`；卡片按名字落在 `<workspace>/drivers`（技能侧用 `card_paths_for_name()` 读取）。密钥不落明文：`drivers/adapters/env_ref.py` 的 `env:` 凭据引用 + `mcp_card_builder.py:126-190` 把 secret env/header 分离成 `CredentialRef`（`CREDENTIAL_KIND_STATIC` / `CREDENTIAL_KIND_OAUTH_AUTH_CODE`）。**旧格式迁移**：`drivers/adapters/mcp_legacy_config.py` 把 `agent.json` 的 `mcp.clients` 迁到 DriverCard，`CURRENT_MCP_MIGRATION_VERSION = 2`（v1 迁移、v2 修复单个 `${VAR}` 字面量 → `env:` 引用，#6029）。
- 管理与策略：`app/mcp/config_service.py:75 MCPConfigService` + `app/routers/mcp.py`（list/create/toggle/get/update/delete client、工具白名单、policy、access principals）+ `mcp_oauth.py`（OAuth，`code_verifier` 用 SHA-256）。策略侧 `MCPAccessPolicy`（config_service.py:434）、`driver_policy_from_mcp_access_update()`(464)、按主体（subject）的规则与 `POLICY_EFFECT_ASK` 默认询问。
- 示例 MCP 服务端：`packages/qwenpawmail-mcp`（`pyproject.toml` 依赖 `mcp>=1.28,<2.0` + `imap-tools`，入口 `qwenpawmail-mcp = qwenpawmail_mcp.__main__:main`）。`server.py` 用 `FastMCP` 暴露 23 个邮件工具，工具边界把异常统一转 `ToolError`（isError 语义），用 `mcp.types.ToolAnnotations` 标注 `read_only/idempotent`，并写了一个很有代表性的 `coerce_str_list()`——因为"LLM 客户端常把数组参数序列化成 JSON 字符串或逗号串"，所以做宽松归一化。`registration.py`/`providers.py` 处理网易/QQ 邮箱：由于注册需要短信验证，"无法自动化"被显式建模为 `RegistrationError` + 生成注册指引。
- 外部配置互操作：`harnesses/events.py:127 HarnessDiscoveredMCPServer`、`harnesses/codex/adapter.py:207`、`portability/providers/codex.py` 的 `SourceMCPServer` 用于从 Codex 等外部 harness 导入 MCP 配置。

---

## 5. 市场 / 分发（market 与两条独立管线）

存在**两条互不相同**的分发管线：

1. **技能市场**（`src/qwenpaw/market/`，schema 在 `market/schema.py`：`MarketResult{source,slug,name,description,source_url,version,author,icon_url,stats}`、`MarketSearchError`、`ProviderInfo{key,label,available,reason,supports_browse}`）。`market/providers/` 四个 provider（qwenpaw/clawhub/modelscope/aliyun）各有 `available()` 门禁，`market/service.py` 并发聚合 + 按 provider 路由分类；安装由 `skill_system/hub.py` 落地为 zip 目录树。
2. **插件市场**（`plugins/download_catalog.py`）：CDN 常量 `PLUGIN_DOWNLOAD_CDN = "https://download.qwenpaw.agentscope.io"`，`build_plugin_catalog()` 拉清单做版本比较（`_is_upgrade_available`/`_get_version_constraint`/`_is_entry_compatible`），条目里带 `sha256` 字段（download_catalog.py:265）；`app/routers/plugins.py` 的 `search_market_plugins()` 只是**反向代理 AgentScope Platform** `/openapi/v1/plugins`（避免 CORS）。

**包格式**：插件=zip（根目录或单层子目录含 `plugin.json`，`_find_plugin_dir()`）；技能=目录（含 `SKILL.md`）或 zip 包。

**签名/校验与来源信任：没有签名或发布者校验链。** 现有防护只有：(a) `app/routers/plugins.py:94 _safe_extract_zip()` 的 **Zip Slip** 防护（逐个成员 `resolve()` 后必须 `is_relative_to(extract_resolved)`）；(b) 下载体量上限 `_MAX_DOWNLOAD_BYTES = 500MB` 与技能包 200MB/4096 条目；(c) `loader.py:102 resolved_plugin_manifest_path()` 用 `os.path.realpath` 校验 `plugin.json` 不逃出源目录；(d) 第三方市场来源登记 `plugins/marketplace_registry.py:ExternalMarketplaceRegistry` 会把 `source` 的 URL 凭据/query/fragment 清掉再持久化，文件权限 `0o600`；(e) 工具归属冲突 fail-closed（§1、§3）。`sha256` 仅被记录/展示，未见任何校验调用点。

**安装流程（插件）**：上传/URL 下载 → 解压（Zip Slip 校验）→ 定位含 `plugin.json` 的目录 → `load_plugin_from_path()` 复制进 `PLUGINS_DIR` → 安装 `dependencies`（UV/pip + 跨进程文件锁）→ import 后端模块 → `PluginRegistry` 注册各类扩展并 `_post_load_setup()`（同步工具到 agent、注册 provider/命令）→ 失败则回滚并清占位 marker。

---

## 6. 多 agent / 模式（modes）

- 抽象：`modes/base.py:30 AgentMode`——一个模式是"命令 + 工具 + 钩子 + 提示词贡献者"的**捆绑**，`setup(workspace)` 是唯一注册入口（分别写入 `slash_command_registry` / `tool_registry` / `hook_registry` / `prompt_manager`），"which mode owns what"因此可直接由四个方法推导；`is_active(ctx)` 默认 `False`（"未配置的模式绝不静默泄漏进请求"）；`ModeGatedHook` 把门禁内建（注释：忘记门禁曾是反复出现的 bug）；`find_active_explicit_mode()` 用于跨模式查询。
- 内置模式：`app/workspace/bootstrap_factory.py:143-152` 注册 `DefaultMode / CodingMode / MissionMode / GoalMode`。实现分别是 `modes/default/mode.py`（含 `resolve_max_iterations`、gate 配置）、`modes/coding/{__init__,hooks,mixin}.py`（`CodingModeMixin` 注入编码能力）、`modes/mission/{handler,state,gates,prompts,hooks}.py`、`modes/goal/{goal_mode,gates,tools,prompts,contributor,helpers}.py`。
- 用户自定义：`modes/custom_loop/`——`DeclarativeLoopMode`（声明式门控管线模式）、`load_custom_loop_modes(workspace)`、`LoopModeActivationStore`（按 session 记录激活的模式），编译链是 `loop/compiler.py:compile_loop_mode` + `loop/gates.py:StopHandler/StopHandlerRegistration`，配置模型 `config.CustomLoopModeConfig`；模式名形如 `custom:<mode_id>`，可用 `/` 命令激活/重置。插件侧还有 `PluginApi.register_mode(mode_cls)`（api.py:1013）：**每个工作区新建独立实例**（避免 gates/stop handler 跨工作区共享），并同时挂 startup hook 与 `workspace_created` hook（priority=70）。三档门控来源：模式自身 `is_active`、`ToolDescriptor.requires_modes`、feature flag。
- 与"agent 角色"的关系：模式是**单 agent 内的行为捆绑**；多 agent 走另一条线——`app/workspace` + agent profiles、`agents/tools/agent_management.py` 的 `list_agents/chat_with_agent/submit_to_agent/check_agent_task/spawn_subagent`、`delegate_external_agent`，以及插件的 Markdown 角色定义（cloudpaw 的 `agents/<role>/<lang>/{SOUL.md,PROFILE.md}`，与 `agents/prompt.py`/`md_files` 中的 SOUL/PROFILE/AGENTS.md 加载对应）。PawApp 还提供 `agent_profile()` 与 `register_agent_stop_handler` 等。

---

## 7. Office 文档能力（office/）

**实现方式不是 python-docx/pptx 服务，而是"技能 + 命令行工具链"。** 证据：

- `src/qwenpaw/office/` 无源码（§0），全仓库无 `import qwenpaw.office`。
- 能力载体是内置技能：`agents/skills/docx-{en,zh}`、`pptx-{en,zh}`、`xlsx-{en,zh}`（另有 `pdf-*`、`wps-zh`）。`docx-zh/SKILL.md` 的"前置依赖"直接列出：`docx`（`npm install -g docx`，创建新文档）、LibreOffice `soffice`（`.doc`→`.docx`、接受修订、导出 PDF）、`pandoc`（文本提取）、`pdftoppm`（poppler，转图），并声明"Windows 上依赖须在 PATH，缺失就报告并停止，不要反复重试"。
- 实际操作路径：技能要求 `cd {this_skill_dir} && python scripts/...` 或使用 `execute_shell_command` 的 `cwd` 参数——即 **Office 任务是普通 shell 工具调用 + 技能正文指令**，没有专属 office tool。
  - Word：`docx-en/scripts/office/{unpack,pack,validate}.py` + `validators/docx.py`（直接操作 OOXML zip 内的 XML），`comment.py` 用 `defusedxml.minidom` 构造批注，`accept_changes.py` 调 `office.soffice` 接受修订。
  - Excel：`xlsx-en/scripts/recalc.py` 先 `from openpyxl import load_workbook` 再调 `get_soffice_cmd()` 做公式重算；SKILL.md 用大量篇幅规定交付标准（零公式错误、蓝/黑/绿/红/黄颜色编码、专业字体）。
  - PPT：`pptx-en/scripts/{add_slide,clean,thumbnail}.py` + `office/unpack.py`/validators；从零创建走 `pptxgenjs`（`npm install -g pptxgenjs`，见 SKILL.md:19）。
- LibreOffice 适配层：`scripts/office/soffice.py` 提供 `get_soffice_cmd()/get_soffice_env()/run_soffice()`，Linux 设 `SAL_USE_VCLPLUGIN=svp`，并在"AF_UNIX socket 被禁止的沙箱 VM"里用 `LD_PRELOAD` shim 兜底。渲染/校验链路是 LibreOffice→PDF→图片（`pdftoppm`，Python 备用 `pdf2image`）。
- 旁证：`deploy/office-node/` 只剩 `node_modules/{docx,pptxgenjs}`（无源码/包描述），对应 SKILL.md 期望的全局 Node 工具；`plugins/apps/qwenpaw-creator` 走另一条路——vendor 了 `media_toolkit/renderers/office.py`（`convert_to_pdf()` 用 soffice + 隔离 user profile → `pypdfium2` 渲染）与 `document_reader.py`，并在 `plugin.json` 的 `meta.runtime_dependencies.libreoffice` 里声明 `path_env: CREATOR_LIBREOFFICE_PATH`、`fallback` 文案。`test-tools/office-agent-console`、`test-tools/qwenpaw-office-api-console` 是这两条链的联调控制台。

---

## 8. TL 协议

**要解决的问题**：公司内部网关只提供两段式 `POST /chatbbc/init_session` → `POST /chatbbc/chat`，报文字段是 `prompt_variables` / `session_id` / `txt` / `files` / `stream`，**没有原生 `tools` / `tool_calls` / `response_format`**。TL 方案把工具协议"降维"到系统提示词与模型正文里，在 QwenPaw 本地还原成 AgentScope 的 tool_call。`docs/skills/tl-llm-standalone/SKILL.md` 明确："这是不可改变的约束，不是待探测能力"，且"Provider 不执行工具，QwenPaw 的工具注册、ToolGuard、审批和 ReAct 执行链继续负责"。

**协议形态**（代码级证据）：
- 请求信封 `{appId, trCode, trVersion, timestamp, requestId, data{...}}`（`providers/tl_transport.py:441/536/612`）。init：`data.prompt_variables = [{name: <system_prompt_variable_name>, value: <编译后的 system 字符串>}]`，响应 `data.session_id` 必填。chat：`data = {session_id, txt: <本轮完整 user payload>, files: [], stream}`；非流式读 `data.txt`，流式是 SSE，事件集合 `_KNOWN_EVENTS = {"chunk","done","end","error"}`、`_END_EVENTS = {"done","end"}`（tl_transport.py:21-22，`_SSEParser`）。
- 模型输出必须是**唯一的原始 JSON 对象**（`docs/skills/tl-llm-standalone/references/tool-envelope.schema.json`）：`{"version":1,"type":"final","content": "..."}` 或 `{"version":1,"type":"tool_calls","calls":[{"name":...,"arguments":{...}}]}`（`calls` 1–16 条，`additionalProperties:false`）。提示词侧由 `providers/tl_prompt_codec.py` 拼装：`_HISTORY_PROTOCOL` + `_TOOL_OUTPUT_PROTOCOL`/`_TEXT_OUTPUT_PROTOCOL`/`_STRUCTURED_OUTPUT_PROTOCOL`，并把工具 JSON Schema 与 `Tool choice for this turn` 直接嵌入同一个 system 字符串（`_protocol_for()` 852-890，结尾强调 "No DSML, XML, Markdown fences or text outside the object"）。历史与工具结果由 `normalize_records()`(672)、`normalize_tool_schemas()`(337)、`normalize_tool_choice()`(413) 压成一个 user payload，token 预算由 `estimate_token_count()`(790)/`_count_compiled_tokens()`(800) 控制。
- 客户端：`TLChatModel(ChatModelBase)`（`tl_chat_model.py:35`）实现 `__call__/_compile/_check_budget/count_tokens/_validated_response/_stream_response`，其中 `_validated_response()`(154) 是**最多 `json_correction_max_attempts` 次（上限硬编码为 1）的严格 JSON 语法纠错**循环；`_reject_options()`(117) 直接拒绝生成参数。配置 `TLConfig`（`tl_config.py:12`）：`app_id/tr_code/tr_version/system_prompt_variable_name="system_prompt"/tool_calling_mode=Literal["system_prompt"]/json_correction_max_attempts∈[0,1]/trust_env=False/timeout_seconds=150/stream_idle_timeout_seconds/max_request_bytes=1MiB/max_response_bytes=4MiB/max_wire_response_bytes=64MiB/max_sse_event_bytes=1MiB`，`extra="forbid"`。`trust_env=False` 的理由写在注释里：内网网关绝不能被 `HTTP(S)_PROXY` 重新路由/泄漏。
- 接线：`config/utils.py`/`providers/provider_manager.py:151` 白名单校验 `chat_model ∈ {TLChatModel, OpenAIChatModel, OpenAIResponseModel, …}`；provider 定义文件 `providers/data/tl-provider.json`，启动时读取 `$QWENPAW_WORKING_DIR/tl-provider.json`（可用 `QWENPAW_PROVIDER_CONFIG` 覆盖，缺失即报错不静默降级；`enabled:false` 关闭；缺 `api_key_env` 对应变量时报错）。仓库根 `tl-provider.json` 即实例：`base_url=http://127.0.0.1:8089`、`models[deepseek-v4-flash]`、`max_input_length=1048576`、`tl_config{tool_calling_mode:"system_prompt", json_correction_max_attempts:1, timeout_seconds:150}`。可观测性：`tl_wire_log.py` 以 `TL_WIRE` 记录 init/chat 请求与 SSE 事件，脱敏 + 单条 >32768 字符截断，INFO 级别不输出正文（`docs/tl-provider.md`）。
- **与 OpenAI 兼容协议的差异**（逐条）：① 线上没有 role 数组，只有"一个 system 字符串 + 一个 user payload"；② 没有 `tools`/`tool_calls`/`response_format` 字段——`test-tools/tl-llm-proxy` 对这类控制字段直接返回 **400**，对上游原生工具响应返回 **502**（不转正文）；③ 工具定义以文本嵌在 system prompt，工具调用以正文 JSON 表达，解析出的 `tool_call` **只存在于本地**；④ 流式是 `chunk`/`done` 文本事件，且工具模式要**完整缓存并校验正文后才发布模型 block**，预览走独立可撤销通道（`tl_preview.py`，990 行）；⑤ 每轮都要重新 `init_session` 拿 session；⑥ 结构化输出与工具**互斥**（`compile_prompt()` 里 `unsupported_combination`）；⑦ 生成/路由参数由服务端 `.env` 持有，客户端不传。
- 代理侧：`test-tools/tl-llm-proxy`（npm `tl-llm-proxy-test@0.1.0`，Node `^22.22.1||>=24`，`dist/{server,protocol,session-store,config,providers/*}.js`，providers 有 `openai-compatible/qwen/deepseek`）自称"独立、仅用于测试，不是 QwenPaw 生产依赖"，默认 `127.0.0.1:8089`，`AUTH_MODE=local|bearer`，并提供一整套 `MAX_*`/`*_TIMEOUT_MS`/`SESSION_TTL_MS`/`MAX_SESSIONS` 边界配置；其 README 还主动列出与 standalone 的 **6 处已知差异**（错误映射未对齐公司 fixture、下游只发 `done`、上游要求 `finish_reason: stop` 后再 `[DONE]`、deadline 120s vs 150s、单 SSE 事件 256KiB vs 1MiB 等）。`docs/skills/tl-llm-proxy` 是它的服务端技能，`docs/tl-port-multisync.md`、`docs/windows-direct-tl-gateway.md`、`docs/multi-user-tl.md` 是部署侧文档。

---

## 9. 这套扩展体系的清晰之处与明显问题

### 清晰之处
1. **声明式优先**：工具用 `@tool_descriptor` 装饰即注册（`agents/tools/__init__.py` 明示"新增内置工具只需两件事"）；技能用 frontmatter 声明并自校验前置条件；插件用 `meta.tools[].config_fields` 声明配置表单，UI 自动生成。
2. **单一 manifest、显式 type**：`plugin.json` + `PluginType` 枚举把"这个插件是什么"变成一等字段，并有 i18n/`entry_point`/`min_version` 三条兼容归一化，老插件不炸。
3. **卸载是有归属的**：`_TOOL_PLUGIN_OWNERS` + "manifest 名字只是候选、绝不作为删除授权"的注释，以及 `register_tool` 的 fail-closed 顺序，属于少见的认真对待热插拔的实现。
4. **技能/MCP/工具三方显式交叉**：`SkillRequirements.require_mcps` 校验 DriverCard，`ToolDescriptor.requires_skills` 反向依赖技能，`requires_modes` 绑定模式——门控是一等公民而非散落 if。
5. **诊断文化**：`module_isolation.py` 文件头列 6 条已知限制、`tl-llm-proxy/README` 列 6 处协议差异、`tl-llm-standalone/SKILL.md` 明确"这是待实现设计，不表示项目已注册"、`docs/tl-provider.md` 讲清日志含敏感内容——文档诚实度明显高于平均水平。

### 明显问题（各举例）
1. **Office 能力悬空**：`src/qwenpaw/office/` 只剩 `.pyc`（`api/bundle/service/storage/verification` 等模块名可辨），无源码、无 git 记录、无引用；实际能力退化为"技能正文 + 全局 npm 包 + LibreOffice"，依赖用户在 PATH 上凑齐 `docx`/`pptxgenjs`/`soffice`/`pandoc`/`pdftoppm`，失败模式是"报告依赖问题并停止"。
2. **同名不同物的 hub**：`src/qwenpaw/hub/`（多租户 Docker 运行时装管）与 `agents/skill_system/hub.py`（技能下载器）共用 "hub" 一词，新人极易误判架构。
3. **扩展点/抽象层过多且重叠**：可注册工具与权限的地方至少有 `runtime/tool_registry.py`、`governance/tool_registry.py`、`security/tool_guard`、`plugins/api.py`、`pawapp/app.py`、`modes/base.py` 六处；`PluginApi` 18+ 个 `register_*`；MCP 一个 secret 要穿过 card builder → `env_ref` → credential store → policy rule → 迁移 v2 五层抽象。
4. **版本门禁会静默失效**：不兼容时只写 `PluginRecord(enabled=False, diagnostics=[...])` 并 `logger.warning`，用户看到"已安装"却不生效；仓库里大量示例 `max` 停留在 `"2.1.0"`，而当前版本已到 `2.2.1b1` 量级（memory 插件 `min: 2.2.1b1`、chrome `min: 2.0.0`），矩阵式版本漂移明显。
5. **无签名/信任链**：插件下载只有 `sha256` 字段（未被校验）、Zip Slip 防护与体积上限；插件是**同进程 Python 代码**，`module_isolation` 明确只是"裸导入命名空间隔离"并自陈 6 条限制，不是安全沙箱，也无权限清单强制（`meta.permissions` 只是声明）。
6. **重复内容与双份实现**：28 个内置技能目录按 en/zh 成对复制（`docx-zh`/`docx-en` 整篇正文重复），frontmatter 又有 `_read_frontmatter`/`_read_frontmatter_safe`/`load_skill_frontmatter_from_dir` 三套读法；`app/migration.py:76` 甚至把"请用户确认所有 SKILL.md 的 YAML frontmatter 合法"写进迁移提示。
7. **TL 把结构化能力压到文本上，且与公司协议硬绑定**：工具定义进 system prompt、输出限定唯一 JSON、`calls` 上限 16、结构化与工具互斥、纠错仅 1 次；`chatbbc` 路径与字段名散落在 `tl_transport.py`(1091 行)、`tl_prompt_codec.py`(1693 行)、`tl_preview.py`(990 行)、`tl_chat_model.py`(320 行) 四个大文件里，协议一改就要动四处。
8. **插件目录发现过浅**：`discover_plugins()` 只扫一层子目录且要求 `plugin.json`，因此仓库示例里 `middleware-demo/thinking-log-middleware` 这类嵌套插件必须显式 `qwenpaw plugin install <path>`，批量安装/嵌套命名空间需要额外约定。
