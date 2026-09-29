# QwenPaw 官方 main 扩展与能力体系分析

> 基线：`/tmp/qpmain`（`git archive origin/main` 的干净导出），版本 `src/qwenpaw/__version__.py:3 = "2.2.2b1"`。
> 方法：只分析 `/tmp/qpmain`；用 `git -C /Users/yangxuezhen/git/QwenPaw diff --shortstat origin/main..HEAD -- <dir>` 把"官方真身"与本地 `multisync` 改造逐目录分离。行号均指 `/tmp/qpmain` 内的相对路径。

## 0. 归因：先把"官方"和"本地"切开

逐目录 diff（`origin/main..HEAD`）结果：

| 目录 | 本地改动 |
|---|---|
| `src/qwenpaw/plugins`、`agents/skill_system`、`market`、`modes`、`drivers`、`hub`、`pawapp`、`tool_calls`、`governance`、`security`、`agents/tools`、仓库根 `plugins/`、`packages/` | **0 改动**（与官方 main 逐字节一致） |
| `src/qwenpaw/providers` | 21 个文件：新增 `tl_chat_model.py`/`tl_transport.py`/`tl_prompt_codec.py`/`tl_preview.py` 等全套 + `data/tl-provider.json`，并改动 `provider_manager.py`、`fallback_chat_model.py` 等 |
| `src/qwenpaw/agents/skills` | +1 文件：`wps-zh/SKILL.md` |
| `src/qwenpaw/server` | +27 文件（本地多用户服务端） |
| `docs` | +29 文件（`docs/skills/`、`docs/tl-*.md`、`docs/api-reference.md`…） |
| `test-tools` | +114 文件（TL 代理 + 两个 office 控制台） |
| 根 `tl-provider.json`、`deploy/server`、`pyproject.toml` | 本地新增 |

关键事实（已用 git 对象库核实）：`git ls-tree origin/main packages/` 只有 `packages/qwenpawmail-mcp`；`git cat-file -e origin/main:{docs/skills,test-tools,src/qwenpaw/server,src/qwenpaw/office,tl-provider.json}` 全部返回不存在。`src/qwenpaw/office/` 与 `deploy/office-node/` 是**未跟踪的构建残留**——`git status --porcelain --ignored -- src/qwenpaw/office` 输出 `!! src/qwenpaw/office/`（整目录被 ignore），`git ls-files` 为空，目录内只剩 `__pycache__`。

这条归因是本次审计的支点：旧报告 §1–§6 的机制描述在官方 main 上**逐行可复现**（下文行号与旧报告高度吻合）；被本地改造污染的是 §7（Office 归因）与 §8（TL，整节本地新增），§9 的第 4、6、7 条结论也需要改写。

---

## 1. 插件系统

**发现与目录。** 插件根目录 `src/qwenpaw/constant.py:283 PLUGINS_DIR = WORKING_DIR / "plugins"`，由 `src/qwenpaw/config/utils.py:919 get_plugins_dir()` 暴露。`src/qwenpaw/plugins/loader.py:272 PluginLoader.discover_plugins()` **只扫一层子目录**且**必须存在 `plugin.json`**（loader.py:298），隐藏/禁用目录由 `loader.py:141 _is_disabled_plugin_dir()` 跳过。

**manifest 与类型枚举。** `src/qwenpaw/plugins/architecture.py:122 PluginManifest`（`extra="ignore"`，architecture.py:136）字段为 `id/version/name/description/description_i18n/author/entry/dependencies/min_version/max_version/qwenpaw_version/meta/plugin_type`。`architecture.py:12 PluginType` 恰好 9 个成员：`tool/provider/hook/command/channel/memory/frontend/app/general`——**没有 `bundle`**。`architecture.py:76 _infer_type_from_meta()` 是缺 `type` 时的兼容推断；`architecture.py:158 _normalise_input()` 处理三种历史写法：i18n 对象形式的 `name/description/author`、顶层 `entry_point`、`type` 缺失/非法。`architecture.py:108 QwenPawVersionConstraint` 语义是 `>=min, <max`，`max` 缺省按 `{major}.{minor+1}.0` 推导。

**版本门禁（旧报告在此误判）。** `loader.py:331 _check_version_compatibility()` 委托 `src/qwenpaw/_version_compat.py:37 check_plugin_version_compat()`。该函数第 67–77 行有一段**被注释掉的完整区间检查**，并留有说明："Temporary: only enforce >= min"。即官方 main 目前**只校验下界**，上界被官方主动临时禁用。不兼容时不抛错，而是 `loader.py:742` 写入 `PluginRecord(enabled=False, diagnostics=[compat_msg])`。

**依赖与安装。** `loader.py:348 _is_requirement_satisfied()`、`388 _find_unsatisfied_dependencies()`、`445 _install_requirements_locked()`、`833 _find_uv()`、`912 _install_requirements()`、`1027 _install_requirements_frozen()`（冻结桌面版）；跨进程互斥在 `src/qwenpaw/plugins/install_lock.py`。安装入口 `loader.py:1093 load_plugin_from_path()` → `1197 _load_plugin_from_path_unlocked()`。

**装载与隔离。** `loader.py:691 load_plugin()` → `_load_plugin_unlocked()`：按 `entry.backend` 用 importlib 执行模块，模块**必须导出 `plugin` 实例**（`src/qwenpaw/plugins/validation.py:24 validate_plugin_module()`，第 95–97 行报错文案 `"Plugin module must export a 'plugin' instance"`）；只有 `entry.frontend` 的插件允许"前端-only"加载。隔离由 `src/qwenpaw/plugins/module_isolation.py` 提供：`build_plugin_builtins()` 覆写 `__import__`，把裸绝对导入解析到插件自己的目录；`PluginNamespaceFinder` 作为 `sys.meta_path` 钩子覆盖嵌套/惰性导入。模块头 docstring 第 13 行点名 issue **#6683**，并在第 24–56 行**主动列出 6 条已知限制**（`importlib.import_module` 绕过、插件自加 `sys.path` 项被清扫、`__builtins__` 是快照、pickle 兼容性、名字解析终身缓存、裸名模块被移出 `sys.modules`）。

**卸载的归属。** `loader.py:1478 _cleanup_plugin_tools()` 基于 `plugins/api.py:60 _claim_tool_ownership` 维护的 `_TOOL_PLUGIN_OWNERS` 反查归属，并在第 1499–1501 行写下明确注释："Manifest names are candidates only — never deletion authority. A misconfigured / malicious plugin must not unload another plugin's tool, a builtin, or a hot-reload replacement."

**可扩展点。** `plugins/api.py:314 PluginApi` 提供 **17 个** `register_*`：`register_memory_backend(347)`、`register_provider(395)`、`register_startup_hook(441)`、`register_shutdown_hook(473)`、`register_uninstall_hook(505)`、`register_workspace_created_hook(548)`、`register_http_router(590)`、`register_control_command(621)`、`register_middleware(644)`、`register_channel(679)`、`register_tool(810)`、`register_slash_command(954)`、`register_mode(1013)`、`register_runtime_hook(1056)`、`register_agent_stop_handler(1096)`、`register_prompt_section(1150)`、`register_skill_provider(1361)`，外加 `unregister_skill_provider(1448)`。宿主侧 `src/qwenpaw/plugins/registry.py:130 PluginRegistry` 用 7 个 dataclass 承载（registry.py:56/68/79/88/97/111/120），且 `registry.py:139 __new__` 明确是单例。`src/qwenpaw/plugins/runtime.py:10 RuntimeHelpers` 只暴露 provider 查询与日志。

**示例插件（16 个 `plugin.json`）。** `tool/{qwen-image,wan27,gpt-image2}` 演示 `meta.tools[].config_fields` 声明配置表单（如 `plugins/tool/qwen-image/plugin.json` 的 `type:"select"` + `options`）；`channel/azure_bot` 是 `type:"channel"` + 依赖 `aiohttp/PyJWT/msal`；`memory/{powercontext,adbpg}` 是 `type:"memory"` + `meta.memory_backends` + `pack_exclude`；`apps/{agent-kanban,qwenpaw-data,qwenpaw-creator}` 是 `type:"app"` + `meta.pawapp/{icon,entry_page,launch_scope,category}` + `meta.permissions` + `meta.runtime_dependencies`（creator 声明了 `libreoffice.path_env = CREATOR_LIBREOFFICE_PATH`）；`bundle/cloudpaw` 同时用 routers + tools + hooks + Markdown 角色定义（`plugins/bundle/cloudpaw/agents/{orchestration,verifier,executor}/{zh,en}/{SOUL.md,PROFILE.md}`）；`bundle/chrome` 用**顶层** `capabilities` 数组（`extra="ignore"` 会忽略它）；`middleware-demo/{tracing,thinking-log}-middleware` 演示 `register_middleware(factory, priority=...)`，tracing 以 `QWENPAW_TRACE` 条件激活（`tracing_plugin.py:59-61`）。注意 `plugins/bundle/omp_workflows/plugin.json` 写的是 `"type": "bundle"`，**不在枚举里**，会落到 `_infer_type_from_meta`（meta 为空、无 frontend）→ `GENERAL`；它真正的能力来自 `plugins/bundle/omp_workflows/plugin.py:29` 的 `api.register_mode(...)`（Autopilot/Ralph/Team/UltraQA/Ultrawork）与 `plugin.py:32` 的 `register_skill_provider`。

**插件市场与信任链。** `plugins/download_catalog.py:21 PLUGIN_DOWNLOAD_CDN = "https://download.qwenpaw.agentscope.io"`，`198 build_plugin_catalog()` 拉清单并用 `164 _is_entry_compatible()` 过滤；`download_catalog.py:265` 把 `sha256` 写进条目——全树 grep 确认**没有任何校验调用点**。`src/qwenpaw/app/routers/plugins.py:1032 /market/search` 只是反代 AgentScope Platform 的 `/openapi/v1/plugins`（避免 CORS）。`plugins/marketplace_registry.py:29 ExternalMarketplaceRegistry` 用 `_clean_source()`（第 17 行）剥掉 URL 凭据/query/fragment 再持久化，落盘用 `new_file_mode=0o600`。安全防护只有 Zip Slip（`app/routers/plugins.py:94 _safe_extract_zip()`，第 110 行 `is_relative_to`）、`_MAX_DOWNLOAD_BYTES = 500MB`（plugins.py:1086）与 `loader.py:102 resolved_plugin_manifest_path()` 的 realpath 逃逸检查。

---

## 2. 技能系统

**格式。** 技能是 Markdown + YAML frontmatter。`src/qwenpaw/agents/skills/docx-zh/SKILL.md:1-9` 是典型样本（`name/description/license/metadata.builtin_skill_version`）；带前置条件的样板见 `agents/skills/mailbox-en/SKILL.md:5-10`：

```yaml
metadata:
  builtin_skill_version: "1.3"
  qwenpaw:
    emoji: "📧"
    requires:
      mcp: ["qwenpawmail"]
```

解析在 `src/qwenpaw/agents/skill_system/store.py`：`195 read_frontmatter_safe_from_path()`、`218 _read_frontmatter()`、`274 load_skill_frontmatter_from_dir()`（严格版，缺头/非法 YAML 抛 `SkillsError`）、`246 _read_bounded_frontmatter_bytes()`（有界解析，`store.py:62 _MAX_FRONTMATTER_LINES=4096`、`63 _MAX_FRONTMATTER_BYTES=256*1024`）。运行时身份是**目录名**：`skill_system/models.py:47 SkillInfo` 的 docstring 写明 "frontmatter can drift while the on-disk workspace identity must remain stable"。要求解析在 `store.py:868 parse_skill_requirements()`，命名空间按 `store.py:53 _REQUIREMENTS_METADATA_NAMESPACES = ("openclaw", "qwenpaw", "clawdbot")` 依次探测。模型：`models.py:66 SkillRequirements{require_bins, require_envs, require_mcps}`、`models.py:15 ALL_SKILL_ROUTING_CHANNELS`。

**发现与池。** `store.py:84 get_skill_pool_dir()`、`91 get_workspace_skills_dir()`、`64 _POOL_MANIFEST_SCHEMA = "skill-pool-manifest.v1"`。内置语言变体 `registry.py:69 BUILTIN_SKILL_LANGUAGES = ("en","zh")` + `70 _BUILTIN_SKILL_DIR_RE`，导入 `registry.py:710 import_builtin_skills()`，单技能带回归回滚的升级 `registry.py:1668 update_single_builtin()`。**规模（旧报告数字有误）**：`src/qwenpaw/agents/skills/` 下是 **16 个技能 × 2 语言 = 32 个目录**（QA_source_index、browser、channel_message、chat_with_agent、cron、dingtalk_channel、docx、file_reader、guidance、mailbox、make-skill、make_plan、multi_agent_collaboration、pdf、pptx、xlsx），**不存在 `wps-zh`**。

**前置条件即能力校验。** `registry.py:1224 check_skill_dependencies()` 三段：环境变量（`_skill_env_key()`，第 1219 行，Windows 大写）、PATH 上的 CLI（`shutil.which(binary, path=shell_execution_path(env["PATH"]))`）、MCP（`card_paths_for_name(workspace_dir/"drivers", name)` 必须存在，且 `load_card(paths[0])` 的 `card.name == name`、`card.protocol == "mcp"`、`card.enabled`）。`registry.py:1261 resolve_effective_skills()` = manifest `enabled` ∧ `channels` 命中 ∧ 前置满足；任一项不满足只 `logger.error` 并**跳过该技能但保留 enabled 状态**，下轮重试。

**注入提示词的两条路径。** (a) 预加载：`registry.py:1321 select_preload_skills()` 取 manifest `preload: true`，由 `src/qwenpaw/runtime/prompt_contributors.py:451 PreloadedSkillsContributor`（`name="preloaded_skills"`、`priority=95`）包成 `<preloaded-skills><skill><name>/<description>/<dir>` + 整份 markdown（451–485 行）。(b) 按需：`src/qwenpaw/runtime/builtin_commands.py:640 _build_skill_injection()` + `697 _skill_fallback_handler()`（由 `842 get_skill_fallback_handler()` 注册为斜杠命令兜底），把技能正文作为尾部 `<skill>` 块追加，注释明确"Keep the typed command at the head; append the skill body in a trailing `<skill>` block"，即**用户输入文本原位保留、技能体追加在后**；前端展示由 `src/qwenpaw/app/chats/utils.py:475` 的 `$` 锚定正则 + `482 strip_injected_skill_block()` 剥掉。两条路径的差别是：预加载进 system prompt（常驻、优先级高），按需走 user 消息尾部（一次性、可隐藏）。

**与插件/工具的边界。** 技能本身不注册代码；插件供技能走 `plugins/api.py:1361 register_skill_provider(skills_dir, enabled_by_default, channels)`——宿主负责复制进工作区、reconcile manifest、按 `source` 清理，并对新工作区挂 `workspace_created` 钩子。反向依赖：技能用 `requires.mcp` 依赖 MCP；工具用 `runtime/tool_registry.py:77 requires_skills` 依赖技能。

**技能市场与安装（两条子管线）。** 搜索侧 `src/qwenpaw/market/`：`market/providers/__init__.py:17 PROVIDERS = {qwenpaw, clawhub, modelscope, aliyun}`，`market/service.py:23 list_providers()`、`36 search_market()`（并发按 provider 分页）、`79 _run_one()`（分类路由 + `118 _supported_kwargs()` 能力降级）。安装侧 `src/qwenpaw/agents/skill_system/hub.py`（2388 行）：`105 SKILL_PACKAGE_MAX_ENTRIES=4096`、`106 SKILL_PACKAGE_MAX_BYTES=200MB`、`109 _GITHUB_CACHE_DEFAULT_TTL=300`，来源提取器 `977 _extract_clawhub_slug_from_url`、`989 _extract_skills_sh_spec`、`1003 _extract_skillsmp_slug`、`1018 _extract_lobehub_identifier`、`1038 _extract_modelscope_skill_spec`、`1066 _extract_qwenpaw_skill_spec`、`1109 _extract_aliyun_skill_spec`、`1128 _extract_github_spec`（支持 `owner/repo` 与 `/tree/<branch>/<path>`）；路径安全 `734 _safe_path_parts`、`911 _sanitize_skill_dir_name`。HTTP 面 `src/qwenpaw/app/routers/skills.py:948 /hub/search`、`984 /hub/install/start`、`1013 /hub/install/status/{task_id}`、`1021 /hub/install/cancel/{task_id}`。

**技能安全扫描（旧报告完全遗漏）。** 官方 main 有 `src/qwenpaw/security/skill_scanner/`：`__init__.py:397 scan_skill_directory()`（可 `block` 抛 `SkillScanError`）、`323 _get_scanner()`、`123 compute_skill_content_hash()`、`143 is_skill_whitelisted()`、`97 _get_scan_mode()`（三级：env `QWENPAW_SKILL_SCAN_MODE` > config > 默认）；模式枚举 `block/warn/off`（`__init__.py:84`），超时 30s（`config/config.py:2930`）；规则是 8 组 YAML 签名 `security/skill_scanner/rules/signatures/{prompt_injection,command_injection,data_exfiltration,unauthorized_tool_use,obfuscation,hardcoded_secrets,social_engineering,supply_chain}.yaml`，策略模型 `scan_policy.py:157 ScanPolicy`（`_DEFAULT_POLICY_PATH = data/default_policy.yaml`）；判定 `models.py:169 ScanResult.is_safe`（无 CRITICAL/HIGH 即安全）。接入点是**统一的写入/导入落地点** `agents/skill_system/store.py:1361` 与 CLI `cli/skills_cmd.py:299`；`app/routers/skills.py` 有 12 处 `except SkillScanError` 转 `_scan_error_response()`（skills.py:236）。这是"内容威胁扫描"，仍**不是签名/发布者信任链**。

---

## 3. 工具注册中心、内置工具与权限绑定

**声明模型。** `src/qwenpaw/runtime/tool_registry.py:55 ToolDescriptor`，字段 `name/func/enabled_by_default/requires_modes/requires_skills/requires_features/requires_sandbox/async_execution/description/metadata/governance/ui`；`17 ToolGovernanceSpec(tool_type/target_param/pattern_param/policy_name/fail_without_sandbox/default_policy/policy_reason)`；`46 ToolUISpec`。四类 `requires_*` 门控语义写在 55–71 行 docstring。`216 tool_descriptor(...)` 装饰器把 descriptor 挂到 `fn._tool_descriptor` 并自动收集；`196 get_builtin_tool_funcs()` 用 `193 _BUILTIN_TOOLS_PREFIX = "qwenpaw.agents.tools."` 过滤；`89 ToolRegistry.filter()` 是唯一选择口（`denied` 优先、非空 `allowed` 收窄、`requires_*` 全部满足才入选）。

**批量注册约定。** `src/qwenpaw/agents/tools/__init__.py:1-45` 明确"新增内置工具只需两件事"：`@tool_descriptor(...)` + 在本文件 import 一次；`__all__` 由 `_build_all()` 自动生成，`discover_builtin_tool_funcs()` 返回全集。docstring 第 14–20 行声明 `execute_python_code` / `view_text_file` / `write_text_file` **故意不在此导出**（"qwenpaw 的 react_agent 不注册它们"，字面名仅留在 `security/tool_guard` 的向后兼容白名单里）。

**内置工具清单（31 个，取自 `agents/tools/__init__.py` 的 import 面）。** 文件 `read_file/write_file/edit_file/append_file`；检索 `grep_search/glob_search`；执行 `execute_shell_command`、`run_tool_batch`；网络 `web_search/web_fetch`；媒体 `view_image/view_video/desktop_screenshot/send_file_to_user`；时间 `get_current_time/set_user_timezone`；计量 `get_token_usage`；多 agent `list_agents/chat_with_agent/submit_to_agent/check_agent_task/spawn_subagent`；外部委派 `delegate_external_agent`；迁移 `migration_compat_{inspect,read_file,write_file,update,finalize}`（`migration_compatibility.py:36` 用 `_compat_tool()` 工厂批量生成，`_COMMON` 里带 `self_authorizing_request_opt_in: True`）；代码 `ast_search`；邮件 `activate_f1_exploration_mode`；浏览器 `browser`（双轨：`__init__.py:80-88` 按 `browser.experimental` 在 `agents/tools/browser.py` 与 `deprecated_browser/browser_control.py` 之间二选一）。`lsp` 不在其中——它由 `modes/coding/mixin.py:216 make_lsp_tool(available)` 在编码模式内动态构造（`agents/tools/lsp_tool.py:151`）。

**权限/审批绑定。** `src/qwenpaw/governance/tool_registry.py:30 ToolRegistry` 是"工具是什么"的静态单一来源：`46 register(..., sandbox_required=)`、`254 ALLOWED_TOOL_TYPES = {"file","network","shell","internal"}`、`255 ALLOWED_DEFAULT_POLICIES = {"allow","ask","deny",""}`、`249 snake_to_pascal()`（`read_file` → 规则名 `Read`）、`284 register_tool_governance()`、`388 _register_from_descriptors()`、`425 _register_non_descriptor_tools()`、`477 _collect_governance_gaps()`、`513 assert_no_governance_gaps()`（启动期治理缺口审计）。运行时判定层 `src/qwenpaw/security/tool_guard/engine.py:59 ToolGuardEngine.guard()`（第 213 行）：逐个 guardian 跑，异常只记 `guardians_failed` 不中断；`only_always_run=True` 只跑常驻 guardian；并在第 250–262 行对 `execute_shell_command` 做 **POSIX 反斜杠续行归一化**，防止把敏感 token 拆行绕过路径/正则检查。guardian 三种：`guardians/file_guardian.py`、`rule_guardian.py`、`shell_evasion_guardian.py`。工具侧门面 `src/qwenpaw/runtime/tool_guard.py:13 GuardedFunctionTool`（`131 _guarded_tool_check_permissions`、`319 _ask_user_approval`）；审批本体 `security/tool_guard/approval.py:13 ApprovalDecision`、`:21 ApprovalScope`。`default_policy` 驱动自动生成 `ToolName(**)` 用户规则（`runtime/tool_registry.py:25-26` 注释）。

**调用生命周期。** `src/qwenpaw/tool_calls/_coordinator.py:98 ToolCoordinator.execute()`（注意是**下划线前缀模块**，旧报告的 `tool_calls/ToolCoordinator` 是路径近似）；`_entry.py:14 ToolCallStatus{RUNNING,OFFLOADED,COMPLETED}`、`:20 ToolCallEntry`；`_middleware.py` `ToolCoordinatorMiddleware`；`_hooks.py` `ToolHookRegistry`；超时/卸载常量在 `_timeout_helper.py:16 OFFLOAD_TIMEOUT_RATIO = 0.5`、`:27 COORDINATOR_OWNED_EXEC_TIMEOUT_SECS = 24*3600`、`:30 arm_kill_deadline()`、`:77 cancellable_wait()`，全部由 `tool_calls/__init__.py` 再导出。

**插件工具。** `plugins/api.py:810 register_tool(..., enabled: bool = False)`：延迟到 startup hook 执行，顺序是 `_claim_tool_ownership`(api.py:60) → `_register_to_governance`(api.py:108) → 注入 `qwenpaw.agents.tools` 属性与 `__all__` → 写 `BuiltinToolConfig`（**默认 disabled，用户显式开启**）→ `_bridge_to_runtime`(api.py:141)。第 872–877 行注释把顺序理由写死："Ownership + governance first: fail closed before exposing the tool in toolkit/UI/runtime (avoids #6114-style visible-but-denied, and cross-plugin name collisions)"。

---

## 4. MCP 集成

**只做客户端。** 全树 `FastMCP` 只出现在 `packages/qwenpawmail-mcp/src/qwenpawmail_mcp/server.py:16` 与 `tests/fixtures/mcp/{stdio,http}_echo_server.py`，QwenPaw 主程序**不暴露 MCP server**。

**入口与校验。** `src/qwenpaw/drivers/handlers/mcp.py:56 MCPDriverHandler` 从 `card.endpoint` 取 `transport`（缺省 `"stdio"`，mcp.py:71）；`313 validate_mcp_endpoint()`：stdio 必须非空 `command`、`args` 为字符串列表、`cwd` 为字符串；HTTP 传输只允许 `{"streamable_http","sse"}` 且 `url` 必填。工具名净化 `_sanitize_tool_name()`(mcp.py:463)、`_sanitize_tool_namespace()`(477)、能力转换 `_mcp_tool_to_capability()`(385)。

**连接实现。** `src/qwenpaw/drivers/handlers/mcp_stateful_client.py:156 _MCPClientMixin`、`:860 StdIOStatefulClient`、`:967 HttpStatefulClient`（支持 `streamable_http` 与 `sse`，1059/1072 行分别调用 `streamable_http_client` 与 sse 通道）、`:121 _SessionGoneError`；熔断与指数退避状态在 191–198 行、逻辑在 306–341 行（half-open 探测）。**旧报告遗漏的一层**：`src/qwenpaw/drivers/handlers/mcp_streamable_http.py` 实现 MCP **2026-07-28** 版 Streamable-HTTP 的 `HttpStatelessClient`（每请求 `_meta`/headers）与 `HttpAutoClient`（先现代、失败回退一次到握手时代），见该文件 docstring 第 2–6 行。

**配置格式与凭据。** 不是 `mcpServers` JSON，而是 **DriverCard**：`src/qwenpaw/drivers/contracts.py:104 DriverCard{name, protocol, endpoint, credentials, config, enabled, policy}`，`132 validate_card()` 做身份/凭据/策略/binding 四段校验；卡片落在 `<workspace>/drivers`，技能侧用 `drivers/storage.py:card_paths_for_name()` 读取。密钥不落明文：`drivers/adapters/env_ref.py` 的 `env:` 引用 + `adapters/mcp_card_builder.py` 把 secret 分离成 `CredentialRef`；凭据存储层 `drivers/credentials/{store,providers,bindings,types}.py`。旧格式迁移 `drivers/adapters/mcp_legacy_config.py:49 CURRENT_MCP_MIGRATION_VERSION = 2`。

**管理与策略。** `src/qwenpaw/app/mcp/config_service.py:75 MCPConfigService`（`82 load_card`、`119 list_clients`、`129 list_tools`、`163 update_tool_whitelist`、`195 get_policy`、`257 update_policy`、`270 create_client`、`346 toggle_client`、`352 delete_client`），策略模型 `MCPAccessPolicy` 与 `POLICY_EFFECT_{ALLOW,ASK,DENY}`（config_service.py:41-43）、`434 mcp_access_policy_from_card()`、`464 driver_policy_from_mcp_access_update()`。HTTP 面 `src/qwenpaw/app/routers/mcp.py`（get/put/get/put/get/get/post/patch/get/put/delete 共 11 组路由）+ `mcp_oauth.py`。外部互操作：`harnesses/events.py:127 HarnessDiscoveredMCPServer`、`harnesses/codex/adapter.py:203`、`portability/providers/codex.py:733 _mcp_server()`。

**示例服务端。** `packages/qwenpawmail-mcp`：`server.py` 有 **22 个 `@mcp.tool` 装饰器**（257–970 行），但文件第 2 行 docstring 自称 "registers 23 tools"——一处文档漂移。`server.py:45 coerce_str_list()` 的注释解释了存在理由："MCP clients (LLMs) frequently serialize array arguments as JSON strings"，并规定 5 条归一化规则。`errors.py:41 RegistrationError(MailError)` 把"网易/QQ 邮箱注册需短信验证、无法自动化"显式建模为错误类型而非静默失败。

---

## 5. 市场与分发：两条独立管线

1. **技能管线**：搜索在 `src/qwenpaw/market/`（4 个 provider，各自 `available()` 门禁），安装落在 `agents/skill_system/hub.py` 的 zip→目录树；包格式是"含 `SKILL.md` 的目录"或 zip。
2. **插件管线**：`plugins/download_catalog.py` 拉 CDN 索引（`PLUGIN_DOWNLOAD_CDN`）+ `app/routers/plugins.py:1032 /market/search` 反代 AgentScope Platform；包格式是 zip，内部须有 `plugin.json`（定位 `app/routers/plugins.py:118 _find_plugin_dir()`）。

**签名与校验：没有发布者/签名信任链。** `sha256`（download_catalog.py:265）只被记录与展示。实际防护是四类：(a) 技能侧 `security/skill_scanner` 的内容威胁扫描（默认 `warn`，阻断要显式开 `block`）；(b) 插件侧 Zip Slip 校验（`_safe_extract_zip()`）与 500MB 上限；(c) 技能包 200MB / 4096 条目上限与路径段净化（`hub.py:734/911`）；(d) 工具归属冲突 fail-closed（`plugins/api.py:872-877`）。**另有一条真正的哈希校验链，但只服务于模型目录，不服务于插件/技能**：`providers/model_catalog.py:247 verify_catalog_hash()` 在 `update_model_catalog()`（:260）里核对 `QWENPAW_MODEL_CATALOG_SHA256`，能力目录同理（`providers/capability_baseline.py:24 CAPABILITY_SHA256_ENV`）。

---

## 6. modes / 多 agent / PawApp / hub

**modes。** `src/qwenpaw/modes/base.py:30 AgentMode` —— 一个模式是"命令 + 工具 + 钩子 + 提示词贡献者"的捆绑，`setup(workspace)` 是唯一注册入口（docstring 第 40–54 行明确分别写 `slash_command_registry`/`tool_registry`/`hook_registry`/`prompt_manager`）；`84 is_active()` 默认 `False`；`97 find_active_explicit_mode()` 用于跨模式查询；`112 ModeGatedHook` 把门禁内建（注释："it auto-skips when the owning mode's is_active(ctx) returns False"，即忘记门禁这类反复出现的 bug 已被基类吸收）。内置四个在 `src/qwenpaw/app/workspace/bootstrap_factory.py:143-152` 注册：`DefaultMode/CodingMode/MissionMode/GoalMode`，落地在 `modes/default/mode.py`、`modes/coding/{mixin,hooks}.py`、`modes/mission/*`、`modes/goal/*`。用户自定义走 `modes/custom_loop/`：`mode.py:66 DeclarativeLoopMode`、`mode.py:46 LoopModeActivationStore`（按 session 记激活态）、`mode.py:75 self.name = f"custom:{config.id}"`、`loader.py:16 load_custom_loop_modes()`，编译链 `loop/compiler.py:11 compile_loop_mode()`，配置模型 `config/config.py:1471 CustomLoopModeConfig`。插件侧入口 `plugins/api.py:1013 register_mode(mode_cls)` 会**为每个工作区新建独立实例**（`api.py:1282 _register_mode_cls_to_all_workspaces` / `1292 _register_mode_cls_to_workspace`），避免 gates/stop handler 跨工作区共享。三档门控来源：模式自身 `is_active`、`ToolDescriptor.requires_modes`、feature flag。

**多 agent。** 模式是单 agent 内的行为捆绑；多 agent 走另一条线：`agents/tools/agent_management.py` 的 `list_agents/chat_with_agent/submit_to_agent/check_agent_task/spawn_subagent` + `agents/tools/delegate_external_agent.py`，以及插件的 Markdown 角色定义（cloudpaw 的 `agents/<role>/<lang>/{SOUL.md,PROFILE.md}`）。

**PawApp。** `src/qwenpaw/pawapp/app.py:334 PawApp` 是开发者面向的 SDK：`370 enable_standard_capabilities()` 挂上 `121 _build_capability_router()` 的标准能力（`/chat`、`/chat/stream`、`/chat/history`、chat session 的增删改查与 pin/archive、`/storage/{keys,get,set,delete}`、toast/notify），另有 `route()`(388)、`tool()`(433)、`command()`(468)、`hook()`(485)、`on_install()`(502)。`plugins/api.py` 的 `PluginType.APP`（architecture.py:40-43 docstring）说明 PawApp "Loaded through the same pipeline as other plugins; surfaced only in the App Center"。运行时依赖管理在 `pawapp/dependency.py:43 DependencyHealth` 与 `pawapp/service.py:118 ManagedService`（带 `_validate_loopback_host` 的本地服务生命周期、健康检查、端口分配）。

**hub 是"同名不同物"。** `src/qwenpaw/hub/` 是多租户运行时装管平台：`hub/config.py:245 HubConfig`、`:187 DockerRuntimeConfig(image="docker.io/agentscope/qwenpaw:latest")`、`:143 ControlPlaneConfig`、`:62 RateLimitConfig`；持久层 `hub/registry.py:21 RuntimeRegistry`（SQLite，带 `tenant_id`/`owner_user_id`）；服务层 `hub/service.py:49 RuntimeService`（**不叫 `RuntimeOrchestratorService`**）；供给器 `hub/provisioner.py:26 RuntimeProvisioner` 抽象 + `local_provisioner.py:43 LocalProcessRuntimeProvisioner` + `docker_provisioner.py:41 DockerRuntimeProvisioner`，另有 `windows_reverse_tunnel.py`、`websocket_proxy.py`、`process_isolation.py`。技能侧的"hub"是 `agents/skill_system/hub.py`（技能下载器），两者除了名字毫无关系。

---

## 7. Office 文档能力的真实形态

官方 main 上 **Office 能力 = 内置技能的 Markdown 指令 + 技能目录内的 Python 脚本 + 外部 CLI（LibreOffice / Node 包 / pandoc / poppler），没有任何专属 Office 工具，也没有 `qwenpaw.office` 包**。

- 载体是 6 个技能目录：`agents/skills/{docx,pptx,xlsx}-{en,zh}/`；另外 `pdf-{en,zh}` 有自己的一组脚本（`check_fillable_fields.py`、`fill_fillable_fields.py`、`convert_pdf_to_images.py` 等 8 个）但与 OOXML 工具链无关。
- 每个技能在 `SKILL.md` 里显式列出外部依赖。以 `agents/skills/docx-zh/SKILL.md:15-22` 为例：`docx`（`npm install -g docx`）、LibreOffice `soffice`、`pandoc`、`pdftoppm`（poppler），并写明"在 Windows 上，依赖项必须已安装并在 `PATH` 中可用；如缺失，请报告依赖问题并停止（不要反复重试）"。技能正文同时规定调用方式："所有 `scripts/` 路径均相对于本技能目录。运行方式: `cd {this_skill_dir} && python scripts/...` 或使用 `execute_shell_command` 的 `cwd` 参数"（`docx-zh/SKILL.md:9-11`）。**Office 任务因此就是普通 shell 工具调用 + 提示词指令**。
- 脚本面：`docx-{en,zh}/scripts/` 下有 `accept_changes.py`、`comment.py`、`office/{unpack,pack,validate,soffice}.py`、`office/validators/{base,docx,pptx,redlining}.py`、`office/helpers/{merge_runs,simplify_redlines}.py`；`pptx-*` 额外有 `add_slide.py`、`clean.py`、`thumbnail.py`；`xlsx-*` 用 `recalc.py`（第 13 行 `from office.soffice import get_soffice_cmd, get_soffice_env`）。
- **没有共享的 LibreOffice 适配层。** `find . -name soffice.py` 只返回 6 个文件，全部位于各技能目录内（`skills/{docx,pptx,xlsx}-{en,zh}/scripts/office/soffice.py`），`validators/docx.py` 与 `office/` 整棵树在这 6 个目录里被**逐份复制**。适配器提供 `26 get_soffice_cmd()`（先 `shutil.which`，Windows 再枚举 `soffice.com/.exe` 与 `%PROGRAMFILES%\LibreOffice\program`）、`45 get_soffice_env()`（Linux 设 `58 SAL_USE_VCLPLUGIN=svp`，必要时 `62 env["LD_PRELOAD"] = str(shim)`）、`69 run_soffice()`；shim 的动机写在文件 docstring 第 1–3 行："Helper for running LibreOffice (soffice) in environments where AF_UNIX sockets may be blocked (e.g., sandboxed VMs)"，并在第 77 行注明"AF_UNIX / LD_PRELOAD shim is Linux-only; skip on Windows and macOS"。
- **另一条完全独立的路线**：`plugins/apps/qwenpaw-creator` vendor 了自己的渲染器 `plugins/apps/qwenpaw-creator/backend/vendor/media_toolkit/renderers/office.py` 与 `backend/services/document_reader.py`，并在 `plugin.json` 的 `meta.runtime_dependencies.libreoffice` 里声明 `required: false`、`path_env: "CREATOR_LIBREOFFICE_PATH"`、`required_for: "office document (doc/docx/ppt/pptx/vsdx) rendering in read_document"`、`fallback: "xlsx renders via the data renderer; other office formats return a readable install hint"`。
- 旧报告在这一点上把三种不同性质的东西混在一起：`src/qwenpaw/office/`（**本地未跟踪残留**）、`deploy/office-node/`（**本地未跟踪残留**，`git ls-files` 为空）、`test-tools/office-*-console`（**本地新增的联调控制台**）都不是官方 main 的组成部分；官方 main 的 `deploy/` 只有 `Dockerfile`、`config/`、`entrypoint.sh`，仓库根 `scripts/` 也**没有任何 office 脚本**（只有 install/pack/verify/github/review-bot 等）。

---

## 8. 官方 main 对"模型能力不足"的降级机制（对应旧报告 TL 章节）

官方 main **没有 TL 协议**（`git ls-tree origin/main src/qwenpaw/providers/ | grep tl_` 为空，`git cat-file -e origin/main:tl-provider.json` 失败）。它用**四条互相独立、以"探测—学习—修补"为核心的机制**来解决模型能力不足，且**不需要定制网关协议**：

**8.1 文本工具调用回填（关键机制）。** `src/qwenpaw/local_models/tag_parser.py` 是通用文本协议解析器：`315 text_contains_tool_call_tag()`、`320 parse_tool_calls_from_text()`，识别 `<tool_call>...</tool_call>`，内部先用 JSON（`{"name":..., "arguments":...}`，"arguments" 是字符串时二次 `json.loads`，tag_parser.py:252-257），失败再退到 XML 风格 `<function=f><parameter=p>v</parameter></function>`（`136 _parse_xml_tool_call()`），并再退到**无闭合标签的宽松格式**（`59 _XML_FUNC_LENIENT_RE` / `64 _XML_PARAM_LENIENT_RE`）。文件 docstring 第 4–7 行说明目标是"local models like Qwen3-Instruct embed in their raw text output"。消费点在 `src/qwenpaw/providers/openai_chat_model_compat.py:836 _parse_stream_response()`：当 `has_tool_use` 为假时（第 911 行），**依次扫描 thinking 块与 text 块**，把 `<tool_call>` 抠出来转成 `ToolCallBlock`（`think_call_*` / `text_call_*`，924–975 行），同时把原块文本清成标签前的内容、空块删除。也就是说：**只要上游是 OpenAI 兼容协议，模型把工具调用当正文吐出来也能被还原成原生 tool_call，无需改协议**。

**8.2 请求/响应形状的兼容修补。** 同一个文件的 `_sanitize_tool_call()`(52) 丢弃结构不可用的 tool_call chunk；`_SanitizedStream`(162) 包裹流式响应并 `_capture_extra_content()`(195) 透传 Gemini `thought_signature`；`_format_tools()`(820) 在转发前跑 `_sanitize_tool_schemas()`(641)，注释说明了动机："Some MCP servers declare parameters using JSON Schema boolean values (e.g. `additionalProperties: true`, `items: true`) which are valid per spec but rejected by strict providers such as DeepSeek V4"；`_apply_openai_compat_schema_passes()`(631) 做 boolean schema 剥离(310)、nullable 归一(396)、`$ref` 内联(521)、regex 简写展开(565)、pattern 字段展开(577)。另一条修补线是 `src/qwenpaw/agents/utils/tool_call_coerce.py` 的 `_coerce_tool_input()`，由 `agents/react_agent.py:766 _coerce_tool_call_input()` 在 `_execute_tool_call()` 前逐次按索引 schema 强制类型。

**8.3 试错学习的能力缓存。** `src/qwenpaw/providers/model_capability_cache.py:36 ModelCapabilityCache`（进程内、按 `provider_id:model` 建键），docstring 第 4–12 行说明语义："When the system discovers model capabilities through trial-and-error (e.g. a model requires `reasoning_content` on every assistant message, or rejects multimodal input despite being marked as supporting it)"，且"entries are only written after a confirmed failure-then-recovery cycle"，TTL 由 `QWENPAW_CAPABILITY_CACHE_TTL_SECONDS` 控制（0 = 不过期）。已知键写在 `model_capability_cache.py:47-57`：`needs_reasoning_content`、`rejects_media`、`rejects_audio`。`agents/react_agent.py:504-520` 用 `_capability_key()` / `_media_rejected()` / `_audio_rejected()` 读缓存来决定是否剥掉媒体块（媒体块回退逻辑见 react_agent.py:494-498），并在 610 行有 `_is_audio_fallback_error()` 与 955 行的音频回退重试。

**8.4 主动探测 + 自愈。** `providers/provider_manager.py:579 maybe_probe_multimodal()` 在模型能力未知时后台触发 `_auto_probe_multimodal()`(590)，探测结果写回缓存；第 604–616 行有明确的"自愈"注释："Heal a poisoned `rejects_media` cache entry: if the probe actually saw the image, force `rejects_media` to False … Without this, a stale entry written from an unrelated 400 (request too large, malformed block fields) would silently drop every future image."；切换模型时 `forget(..., "rejects_media")`（provider_manager.py:558-565）。探测数据模型 `providers/multimodal_prober.py`（`ProbeResult`、`_PROBE_VIDEO_B64` 等），"期望能力"基线与差异报告在 `providers/capability_baseline.py`（`ExpectedCapability`、`DiscrepancyLog`、`compare_probe_result()`，并被 `providers/provider_annotations.py` 消费）。

**结论（对照旧报告）**：官方 main 的策略是"**能用原生 tool calling 就用原生**；只在模型把工具调用写进正文时于客户端回填；再用 schema 修补 + 能力缓存/探测把兼容性问题局部化"。它**不要求**把整个 provider 协议重写成"一个 system 字符串 + 一个 user payload + 唯一 JSON 信封"，也不牺牲 role 数组、`tools`、`response_format`。旧报告的 TL 章节描述的是一套与公司内部 `chatbbc` 网关硬绑定的本地改造，**在官方 main 上不存在**。

---

## 9. 清晰之处与明显问题

### 清晰之处
1. **声明式优先且真的自动化**：工具 `@tool_descriptor` + 一行 import 即注册（`agents/tools/__init__.py:1-45`），`__all__` 自动生成；插件用 `meta.tools[].config_fields` 声明配置表单，UI 侧自动渲染（`plugins/tool/qwen-image/plugin.json`）。
2. **"什么类型"是一等字段**：`PluginType` 9 个成员 + 三条归一化路径（i18n / `entry_point` / 类型推断），老插件不炸，且 `general` 是显式兜底。
3. **热插拔的归属设计少见的严谨**：`_TOOL_PLUGIN_OWNERS` + "manifest 名字只是候选、绝不作为删除授权"（`loader.py:1499-1501`），加上 `register_tool` 的 fail-closed 顺序（`api.py:872-877`）与 `_collect_governance_gaps`。
4. **门控是一等公民**：`requires_modes` / `requires_skills` / `requires_features` / `requires_sandbox` 四类独立条件写在 `ToolDescriptor` docstring 里，技能/MCP/工具三方用 `requires.mcp` 与 `requires_skills` 显式交叉。
5. **诊断文化**：`module_isolation.py` 主动列 6 条已知限制；`capability_baseline` 专门做"探测结果 vs 官方文档"的差异记录；`react_agent.py` 的自愈注释直接写明"不这样做会永久丢图"；`_version_compat.py:10` 在模块头声明上界检查被临时禁用。
6. **技能安装有真正的安全闸门**：`security/skill_scanner` 的 8 组威胁签名 + 白名单 + 阻断历史 + 30s 超时，且接在 `store.py:1361` 这个所有写入路径的汇合点上。

### 明显问题（各举例）
1. **Office 能力完全外挂、且实现重复 6 份**：能力依赖用户 PATH 上凑齐 `soffice`/`pandoc`/`pdftoppm`/全局 npm 包，失败模式是 SKILL.md 明文要求的"报告依赖问题并停止"；`office/` 工具树（含 `validators/docx.py`、`helpers/`）在 6 个技能目录里逐份复制，改一处要改六处，没有任何共享包的约束。
2. **同名不同物的 hub**：`src/qwenpaw/hub/`（多租户 Docker 装管）与 `agents/skill_system/hub.py`（技能下载器）共用 "hub"，新人极易误判架构。
3. **抽象层重叠**：可注册工具/权限的地方至少有 `runtime/tool_registry.py`、`governance/tool_registry.py`、`security/tool_guard/`、`plugins/api.py`、`pawapp/app.py`、`modes/base.py` 六处；`PluginApi` 17 个 `register_*`；一个 MCP secret 要穿过 card builder → `env_ref` → `credentials/store` → policy rule → 迁移 v2 五层。
4. **版本门禁上界被静默禁用，且示例 manifest 已大面积漂移**：`_version_compat.py:67-77` 只检查下界，`max` 完全不生效；而示例里 `plugins/bundle/cloudpaw` 写 `max: "2.1.0"`、`plugins/tool/*` 写 `max: "2.1.0"`，当前版本已是 `2.2.2b1`。因为上界被关掉，这些漂移目前"不炸"，但一旦恢复检查，示例会大面积失效——这是被注释掉的安全网，不是设计。
5. **插件无签名/信任链**：`sha256` 只记录不校验（`download_catalog.py:265`），插件是**同进程 Python 代码**，`module_isolation` 自陈"只是裸导入命名空间隔离"并列出 6 条限制；`meta.permissions` 仅是声明。技能侧虽有 `skill_scanner`，但默认 `warn`（`config/config.py:2926-2928`），即**默认不阻断**。更细的一处矛盾：`security/skill_scanner/__init__.py:97 _get_scan_mode()` 的 docstring 写"default `warn`"，函数末行却在配置加载失败时回落 `"block"`——两处默认值语义相反且无注释说明。
6. **`PluginType` 枚举与示例 manifest 不一致**：`plugins/bundle/omp_workflows/plugin.json` 写 `"type": "bundle"`（不在枚举内），`plugins/bundle/{computer-use,qwenpaw-pet}` 与 `plugins/tool/{qwen-image,wan27}` 干脆没有 `type`。它们靠 `_infer_type_from_meta` 兜底，而这个兜底函数（`architecture.py:76`）**不会**推断出 `app`/`memory`/`bundle`——`memory` 插件之所以能工作，全靠 manifest 显式写了 `type`。
7. **文档与代码的小幅漂移**：`packages/qwenpawmail-mcp/src/qwenpawmail_mcp/server.py:2` 自称注册 23 个工具，实际是 22 个 `@mcp.tool` 装饰器（257–970 行）。
8. **插件目录发现过浅**：`discover_plugins()` 只扫一层且要求 `plugin.json`，所以仓库示例里 `plugins/middleware-demo/{tracing,thinking-log}-middleware` 这类嵌套插件不会自动发现，必须显式安装；批量/嵌套命名空间没有约定。

---

## 附录：旧报告逐条审计

见交付回复中的审计表。摘要：旧报告 §1–§6 的机制描述在官方 main 上**逐行成立**（`PluginApi` 17 个方法行号、`module_isolation` 的 #6683 与 6 条限制、`SkillRequirements`/`check_skill_dependencies`/`select_preload_skills`/`PreloadedSkillsContributor(priority=95)`、`ToolDescriptor` 与 `ToolGuardEngine.guard` 行号、MCP `MCPDriverHandler`/`StdioServerParameters`/`CURRENT_MCP_MIGRATION_VERSION=2`、`AgentMode` 与四内置模式，均与官方 main 完全一致），因为 `multisync` 对 `plugins/`、`agents/skill_system/`、`market/`、`modes/`、`drivers/`、`governance/`、`security/`、`agents/tools/`、`packages/` 的改动为 **0**。需要改写的是：§0/§7 关于 `src/qwenpaw/office/` 与 `deploy/office-node/` 的归因（未跟踪残留）、§7 的 `wps-zh` 与 `test-tools/office-*`、§8 整节、§9 第 4/6/7 条，以及 §0 的 `RuntimeOrchestratorService`（官方为 `RuntimeService`）。
