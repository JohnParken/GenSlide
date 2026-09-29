# QwenPaw 官方 main 分支：服务端 / 应用 / CLI / 部署 / 前端架构分析

> 基线：`/tmp/qpmain`（`git archive origin/main` 的干净导出）。**本报告所有结论只以该目录为证据**，路径均相对 `/tmp/qpmain`。
> 规模：`src/qwenpaw/` 下 978 个 `.py`，334,695 行；另有 `console/`（React + Tauri 壳）、`deploy/`（3 个文件）、`scripts/`、`tests/`（778 个测试文件）、`e2e/`、`.github/workflows/`（35 个）、`plugins/`、`packages/`、`website/`。
> 重要前提：本地仓库 `/Users/yangxuezhen/git/QwenPaw` 当前检出 `multisync` = `origin/main` + 4 个本地提交（503 个新增文件、41 个修改文件）。`src/qwenpaw/server/`、`src/qwenpaw/cli/serve_cmd.py`、`src/qwenpaw/providers/tl_*.py`、`test-tools/`、`deploy/server/`、`cloud/`、`deploy/office-node/`、`src/qwenpaw/office/`、`docs/` 下除 `docs/design/environment-management-redesign.md` 之外的文档，全部**只存在于本地提交**，在 main 上不存在。

---

## 1. 官方有几种进程/部署形态

| # | 形态 | 入口 | 启动链路（符号证据） |
| --- | --- | --- | --- |
| 1 | 终端 TUI | 裸 `qwenpaw [项目目录]` | `cli/main.py:131-187` `LazyGroup`；`cli/main.py:218-220` `ctx.invoked_subcommand is None` → `cli/tui/launch.py:run_tui` |
| 2 | 网页/单体服务（主形态） | `qwenpaw app` | `cli/app_cmd.py:82-94` 默认 `127.0.0.1:8088`；`:160-172` `uvicorn.run("qwenpaw.app._app:app", workers=1)`；应用装配 `app/_app.py:725 app = FastAPI(...)` |
| 3 | 桌面 A：pywebview | `qwenpaw desktop` | `cli/desktop_cmd.py:36 WebViewAPI`、`cli/desktop_cmd.py:29 import webview`；靠 `utils/port.get_stable_port` 选空闲端口 |
| 4 | 桌面 B：Tauri v2 | `console/src-tauri` 拉起 Python sidecar | `tauri/entry.py:382 main()`；`:386 mp.freeze_support()`；`:320 reconcile_singleton_backend(WORKING_DIR)`；`:334 port_file = WORKING_DIR/"desktop_port"`；`:341-367 uvicorn.Config/Server` 复用端口；`:367 fastapi_app.state.uvicorn_server = server`（供 `app/_app.py:839 /api/desktop/shutdown` 优雅停机） |
| 5 | **Hub 多用户控制面** | `qwenpaw hub [--force-public]` | `cli/hub_cmd.py:64 hub_cmd`（默认同为 `127.0.0.1:8088`，`:72-76` 非 loopback 直接拒绝，`:87-92` 缺 `docker` 时报 `qwenpaw[hub]`）；`hub/control_app.py:131 create_hub_app`、`:1544 run_hub_app` |
| 6 | Docker 单机 | `/entrypoint.sh` | `deploy/entrypoint.sh` 先 `qwenpaw init --defaults --accept-security`，再 `envsubst` 渲染 `deploy/config/supervisord.conf.template` → `supervisord` 起 `dbus / xvfb(:1) / xfce4 / app`，app 即 `qwenpaw app --host 0.0.0.0 --port ${QWENPAW_PORT}`；`docker-compose.yml` 只映射 `127.0.0.1:8088:8088` |
| 7 | ACP stdio agent | `qwenpaw acp` | `cli/acp_cmd.py:12-55`，把 QwenPaw 作为 ACP agent 跑在 stdio（编辑器集成），不是网络服务 |

**结论**：官方 main 的服务化形态只有一种真形态——**`qwenpaw app`（单进程、单 worker、无状态协调层）**；`qwenpaw hub` 不另起业务进程，而是**托管 N 个完整的 `qwenpaw app` 实例**并把同一份 console 反代给它们。`cli/main.py:135-186` 的 26 个懒加载子命令键（23 个唯一命令）中没有任何 `serve`/`worker`/`controller`/`sandbox`/`migrate`。

---

## 2. ASCII 架构图

```
                        ┌────────────────────────────────────────────────┐
  浏览器 / TUI ────────▶ │ 个人应用   qwenpaw app   (127.0.0.1:8088)        │
  console/ React SPA     │ app/_app.py:725 FastAPI  (workers=1)            │
  dev: vite :5173 ──proxy│ /api/** = routers/__init__.py 33 个子路由        │
        → :8088          │  + /api/agents/{agentId}/** 镜像(12 个子路由)    │
                        │  + /console/** 与 SPA catch-all(:949)           │
                        │ AuthMiddleware(:736) 单账号，可关                 │
                        │ AgentContext → Auth → RuntimeBoundary → CORS     │
                        └───────────────▲────────────────────────────────┘
                                        │ httpx 反代 + 注入
                                        │ X-QwenPaw-Runtime-Token
                        ┌───────────────┴────────────────────────────────┐
 Hub 多用户控制面 ─────▶ │ qwenpaw hub  (默认 127.0.0.1:8088)              │
 (复用同一份 console)    │ hub/control_app.py:131 create_hub_app           │
                        │ /api/hub/** + /api/auth/**  (20+ 端点)          │
                        │ /api/{path} 反代(:1258) + WS 反代(:1423)         │
                        │ SQLite control.db(hub-v1) + Fernet 凭据库        │
                        └───────┬──────────────────────┬─────────────────┘
                                │ RuntimeProvisioner   │ 每用户 1 个 runtime
                  ┌─────────────┴────────┐   ┌─────────┴──────────────────┐
                  │ docker_provisioner   │   │ local_provisioner          │
                  │ isolated-container-  │   │ isolated-local /           │
                  │ shared-kernel        │   │ -shared-network /          │
                  │ (cpu/mem/pids 限额)   │   │ windows-appcontainer       │
                  └──────────────────────┘   └────────────────────────────┘
                                │
                  每个 runtime = 一个完整 `qwenpaw app`
                  （独立 WORKING_DIR / SECRET_DIR / BACKUP_DIR）

  旁路：qwenpaw desktop(pywebview) / Tauri sidecar(PyInstaller+CPython+Node)
        docker: entrypoint → supervisord → dbus+xvfb+xfce4+app
```

---

## 3. HTTP API 设计

- **路由组织**：`app/routers/__init__.py` 聚合 **33 个子路由**，`app/_app.py:874 app.include_router(api_router, prefix="/api")`；另有 `app/routers/agent_scoped.py:87 APIRouter(prefix="/agents/{agentId}")` 把 12 个子路由镜像到 `/api/agents/{agentId}/**`（`:98-110`）。全仓 `@router.*` / `@app.*` 路由装饰器 **355 处**。语音渠道例外：`app/_app.py:915 app.include_router(voice_router, tags=["voice"])` 挂在**根**路径（`/voice/incoming`、`/voice/ws`）。
- **鉴权：单账号，不是多用户**。`app/auth.py:13-16` 文件头即写明 `Single-user design: only one account can be registered`；凭据落 `app/auth.py:51 AUTH_FILE = SECRET_DIR/"auth.json"`（加盐 SHA-256），`app/auth.py:54 TOKEN_EXPIRY_SECONDS = 7*24*3600`。仅当 `QWENPAW_AUTH_ENABLED` 为真才启用（`app/auth.py:5-7`）。白名单 `_PUBLIC_PATHS`（`app/auth.py:57-68`）= `/api/auth/{login,status,register}`、`/api/desktop/shutdown`、`/api/version`、`/api/settings/{language,upload-limit}`、`/api/frontend_plugin`。另有一条**内部**通道：`app/auth.py:38-39 _RUNTIME_TOKEN_ENV="QWENPAW_RUNTIME_INTERNAL_TOKEN"` / `_RUNTIME_TOKEN_HEADER="x-qwenpaw-runtime-token"`，专供 Hub 反代注入——这是 main 上唯一与"多用户"沾边的鉴权胶水。登录限流为纯内存 `app/rate_limiter.py:8 LoginRateLimiter`（账号 5 次锁 15 分钟、IP 每分钟 50 次失败/200 次总量/30 个用户名）。
- **流式：SSE（`text/event-stream`）**。main 上有 **8 个** router 返回 `StreamingResponse(media_type="text/event-stream")`：`app/routers/console.py:329,468`、`backup.py:142`、`skills_stream.py:252`、`portability_imports.py:222`、`workspace.py:1088`、`tool_calls.py:301`、`project_directory.py:335`。前端用 `fetch` + `ReadableStream` 手写解析（不用 `EventSource`）：唯一生产调用点 `console/src/pages/Chat/index.tsx:519,2951,3751`，读取方式见 `:545-551 res.body.getReader()`。
- **错误模型**：`app/_app.py:731 register_exception_handlers(app)`；`app/exception_handlers.py:31 request_validation_error_handler`（→422）、`:40 agent_config_conflict_handler`（→409），其余走 FastAPI `HTTPException`。
- **版本化：无**。没有 `/v1`、没有 `Accept` 协商、没有 `/v2`。`/api` 前缀本身不带版本。OpenAPI 由 `constant.py:264 DOCS_ENABLED = EnvVarLoader.get_bool("QWENPAW_OPENAPI_DOCS", False)` 控制，默认**全部 404**（`app/_app.py:727-729`）。
- **文档与契约同步性**：main 的 `docs/` 目录**只有** `docs/design/environment-management-redesign.md` 一个文件。**没有** `api-reference.md`、**没有** `v1-api.md`、**没有** OpenAPI 快照，`scripts/` 与 `.github/` 中也搜不到任何 `api-reference`/`openapi` 生成或校验步骤。用户可见的 API 文档只存在于 `website/public/docs/`（80 个 md，构建时由 `scripts/wheel_build.sh` 拷入 `src/qwenpaw/docs/`），**与实现之间没有任何机器校验**。

---

## 4. 多用户能力

**官方 main 不含 `/v1` 多用户服务端，但含真正的多用户托管控制面 Hub。**

- 结论的第一层：`src/qwenpaw/server/`（api/worker/controller/sandbox/contracts/repository/storage/migrations/objects/timeline/event_feed/notifications/memory）整个目录在 main 上不存在；`/v1`、`Repository` 协议、TDSQL、S3、五进程拆分均无。main 的持久化是**文件系统**（`WORKING_DIR` 下 `config.json`、`chats.json`、`agent.json`、`envs.json` 等）+ Hub 的 SQLite 控制面。
- 结论的第二层：Hub 是**真多用户**，且是 main 上唯一的多用户实现。
  - 身份：`hub/auth.py:31 HubUser`（`user_id/username/role/disabled/token_version/preferences`，`:47 is_admin`），`hub/auth.py:67 HubAuthService` 提供 `register/create_user/authenticate/create_token/verify_token/list_users_page/update_user/change_password`，PBKDF2 `hub/auth.py:26 _PASSWORD_ITERATIONS = 600_000`，token 7 天 HMAC（`:27 _TOKEN_TTL_SECONDS`），带 `token_version` 可批量失效。
  - 多租户模型：`hub/models.py:27 RuntimeSpec(runtime_id, tenant_id, owner_user_id, provisioner)` + `:37 RuntimeRecord(... working_dir, secret_dir, backup_dir, log_file, state, desired_state, start_policy)`；`:19 RuntimeStartPolicy.OWNER_ALLOWED | ADMIN_ONLY` 区分"用户可自启"与"仅管理员"。
  - **隔离方式 = 每用户一个独立 runtime，而不是共享进程内的行级隔离**。`hub/provisioner.py:31 RuntimeProvisioner`（ABC：`preflight/start/stop/status/close`）有两种实现：
    - `hub/docker_provisioner.py:44-45` `name="docker"`、`security_level="isolated-container-shared-kernel"`，配额由 `hub/config.py:187 DockerRuntimeConfig` 给（`cpu_limit=2.0`、`memory_limit_mb=4096`、`pids_limit=1024`、`shm_size_mb=512`）；
    - `hub/local_provisioner.py:46-64` `name="local"`，`security_level` 依平台取 `isolated-local` / `isolated-local-shared-network` / `isolated-local-windows-appcontainer`；实际边界由 `hub/process_isolation.py:135 name="linux-bubblewrap"`、`:236 name="macos-seatbelt"`、`:472 name="unsupported"`、`hub/windows_process_isolation.py:42 name="windows-appcontainer"` 提供。`preflight()` 会真实探测边界，探测不通过就拒绝启动（安全优先，不做降级）。
  - 租户凭据：`hub/credentials.py:56 TenantCredentialVault`（Fernet 加密，`:59 __init__(database_path, key_path)`），`:242 get_or_create_system_secret`、`:261 get_or_create_runtime_secret`、`:295 get_runtime_secret` 生成 `QWENPAW_RUNTIME_INTERNAL_TOKEN`，正是第 3 节那条内部头。
  - 容量与限流：`hub/config.py:237 RuntimeCapacityConfig.max_running_runtimes`；`hub/access_security.py:20 HubAccessSecurity`（IP 黑名单、可信代理、`:62 RateLimitConfig` 登录/注册双限流）；`hub/proxy_limits.py:20 limited_request_stream` 限制反代请求体大小与空闲时长，`hub/config.py:109 RuntimeProxyConfig`（`max_request_size_mb=1024` 等）。
  - 控制面端点：`hub/control_app.py` 20+ 个 `/api/hub/**`（`:408 healthz`、`:592-712 admin/users|settings`、`:726-798 credentials`、`:798-903 images`、`:903-1139 runtimes`、`:1139-1161 admin/overview|audit`），加 `/api/auth/{register,login,verify,status}`（`:476-538`）与 `/api/hub/me*`（`:538-592`）。
  - 反代：`:1258 @app.api_route("/api/{path:path}", methods=[...])`（HTTP）与 `:1423 @app.websocket("/api/{path:path}")` → `hub/websocket_proxy.py:69 relay_websocket`；`:1484 @app.get("/{path:path}")` 把 Hub 自己的 console SPA 吐出去（`hub/static_files.py:88 resolve_console_static_dir`）。
- 因此 main 上"多用户"= **Hub：一个控制面 + 每用户一个隔离 runtime + 同一份 console**。它是"一台机器多人用/多实例托管"，**不是**"一个共享服务端进程对外提供多租户 `/v1` API"。官方 README 亦确认：`README_zh.md:65` v2.2.0 条目写明"新增**可自托管的多用户 QwenPaw Hub**"。

---

## 5. 配置体系与密钥管理

- **目录优先级**（`constant.py:82-101`）：`QWENPAW_WORKING_DIR` > 已存在 `~/.copaw`（legacy）> `~/.qwenpaw`；`constant.py:103-107 SECRET_DIR` 默认 `{WORKING_DIR}.secret`（`QWENPAW_SECRET_DIR` 可覆盖）；`constant.py:270-274 BACKUP_DIR` 默认 `{WORKING_DIR}.backups`。
- **`.env` 加载**：main **无条件**加载仓库根 `.env`（`constant.py:8-10`）与 `WORKING_DIR/.env`（`constant.py:99-101`），均为 `load_dotenv(override=False)`（先加载者优先）。**main 上没有 `QWENPAW_SERVER_MODE` 这个开关**。
- **个人配置**：`config/config.py`（3956 行）承载 `Config` 与各渠道配置模型。
- **环境变量管理**：`envs/registry.py:19 EnvVarSpec(key, default, mutability, value_type, readonly_reason_code)`，`envs/registry.py:12 EnvMutability = Literal["hot_runtime", "startup_only"]`，`:30 editable` 只有 `hot_runtime` 才允许 Console 持久化——这是"能否热改"的类型化声明；落地在 `envs/store.py`（`SECRET_DIR/envs.json` + `os.environ` 双写）。设计取舍见 `docs/design/environment-management-redesign.md`（Provider 的 API Key/Base URL 仍归 `ProviderManager`，**不进** `envs.json`、**不写** `os.environ`）。
- **密钥**：`security/secret_store.py`（499 行）用 Fernet（AES-128-CBC + HMAC-SHA256，`:5`）；master key 优先 OS keyring（`:168 _try_keyring_get`、`:209 _try_keyring_set`，兼容 legacy CoPaw 条目 `:190`），回退 `SECRET_DIR/.master_key`（0600，`:244-282`）；`:93 _should_skip_keyring()` 在容器/无桌面 keyring 环境主动跳过以免 D-Bus 卡死。
- **Hub 配置**：`hub/config.py:245 HubConfig`（`control_plane` / `runtime` / `capacity`），存储与热更新由 `hub/config.py:265 HubConfigStore` 与 `hub/service.py:87 RuntimeService.apply_config` 完成——**改配置不重启 Hub**。

---

## 6. 前端 console

- **技术栈**（`console/package.json`）：React 18 + TypeScript + Vite + `antd 5.29.3` + `@agentscope-ai/{chat,design,icons}` + `react-router-dom ^7.13.0` + lexical 0.48 + `monaco-editor 0.55.1` + `mermaid ^11` + katex + `i18next ^25`；测试 vitest + jsdom；另有 `3d-force-graph`、`@dnd-kit/*`、`@ant-design/plots` 等重依赖。
- **构建**：`console/package.json` 的 `build = tsc -b && vite build && verify:monaco-css && precompress && verify:initial-bundle`；产物 `console/dist` **不入库**（`.gitignore:39-40` 忽略 `src/qwenpaw/console/dist/` 与 `src/qwenpaw/console/`），由 `scripts/wheel_build.sh`（`npm ci && npm run build` → 拷到 `src/qwenpaw/console/`）、`deploy/Dockerfile` 的 `console-builder` 阶段、CI 三处注入。桌面另有 `build:tauri-bootstrap` → `dist-tauri`，对应 `console/src-tauri/tauri.conf.json:6 frontendDist: "../dist-tauri"`。
- **与后端通信：同源 + 反代，无 CORS 依赖**。`console/src/api/config.ts:17-21 getApiUrl()` = `VITE_API_BASE_URL`（默认空） + `/api` + path；`console/vite.config.ts:30-34` 注释明说"Empty = same-origin"，`:58-61` dev 代理 `/api → http://localhost:8088`。生产由 Python 进程同源托管静态资源（`app/_app.py:755 _resolve_console_static_dir`，探测顺序 `QWENPAW_CONSOLE_STATIC_DIR` → 包内 `src/qwenpaw/console` → `<repo>/console/dist` → `cwd/console/dist`，`/assets` mount 于 `:934`，SPA catch-all 于 `:949`）。Hub 侧复用同一份 dist（`hub/static_files.py:88`）。
- **前端只有一套**。`console/` 是唯一前端；Hub 与个人模式共用它，靠 `VITE_API_BASE_URL`/同源与后端地址解耦。main 上**不存在**第二套前端，也不存在双聊天协议。

---

## 7. 测试与 CI

- **目录**：`tests/{unit,integration,contract,e2e}` + `fixtures`；测试文件数 = unit **559** / integration **194** / contract **24** / e2e **1**，合计 **778**。根目录另有独立 Playwright 框架 `e2e/`（`pages`、`fixtures`、`mocks`、`config`、`utils`、`pytest.ini`）。
- **标记与门槛**（`pyproject.toml [tool.pytest.ini_options]`）：`p0`（PR 冒烟）/`p1`（夜间回归）/`p2`（错误路径与契约）+ 层级 `unit/contract/integration/e2e` + `slow`、`manual_real`。覆盖率 `[tool.coverage.report] fail_under = 50`、`source = ["src/qwenpaw"]`，且注释说明是"30→45→50"分阶段抬升。
- **`tests/contract/` 的语义**：子类契约框架（"改渠道基类不要打破其它渠道"），由 `scripts/check_channel_contracts.py` 检查，在 `contract-tests` job 中执行（`tests.yml:249-252`）。
- **CI：35 个 workflow**，主门禁 `.github/workflows/tests.yml`（742 行），job 序列 `spam-gate(:42) → changes(:52, dorny/paths-filter) → approval-gate(:75, environment: maintainer-approved) → unit-tests(:89) / contract-tests(:195) / integrated-tests(:285) → coverage-report(:517) → test-summary(:680)`；矩阵为 py3.11+3.13（ubuntu）与 py3.11（macos/win），integrated 再按 `p0/p1/p2/fallback` 分片。**强制分级门禁**在 `tests.yml:439-452`：fallback 分片若收集到任何无 `p0/p1/p2` 标记的 integration 测试，就 `::error::` 并 `exit 1`。覆盖率数据由 unit/contract/integration 三层各自采集后**只在 `coverage-report` 合并渲染**（`tests.yml:35-38`），`:699` 注释说明 coverage-report 是"observed, not enforced"。另有 `full-tests-nightly.yml`、`frontend-tests.yml`、`e2e-smoke.yml`、`codeql.yml`、`pre-commit.yml`、`real-behavior-proof.yml`、`release-verify.yml`、`desktop-*`（4 个）、`publish-pypi.yml`、`docker-release.yml`、`plugins-release.yml` 等。`tests.yml:173-180` 还会单独跑 `pytest tests/e2e/test_hub_local_runtime.py -v`（py3.11）——即 Hub 的本地 runtime 端到端验收进了主门禁。

---

## 8. 外围模块（pawapp / services / tunnel / backup / portability / envs / local_models）

这一层全部是 `qwenpaw app` 进程内的**同进程库**，不是独立服务；它们通过 `app/routers/*` 暴露能力。

| 包 | 规模 | 职责与关键符号 | 对外出口 |
| --- | --- | --- | --- |
| `pawapp/` | 8 文件 / 3426 行 | 面向开发者的**小程序 SDK**：`pawapp/app.py:334 PawApp`，装饰器 `:388 route` / `:433 tool` / `:468 command` / `:485 hook`，生命周期 `:502 on_install` / `:507 on_launch` / `:512 on_terminate` / `:517 on_uninstall`，`:524 include_router`、`:531 skill_provider`、`:547 prompt_section`、`:370 enable_standard_capabilities` | `app/routers/pawapps.py`（5 端点：`:142,156,169,222,235`） |
| `services/` | 7 文件 / 1414 行 | 工作区与文件命名：`services/workspace_manager/workspace_manager.py:24 WorkspaceManager`、`services/workspace_manager/sandbox.py:24 Sandbox`、`workspace_files.py`、`project_directory.py`、`fs_name_rules.py` | 被 `app/routers/workspace.py`、`app/routers/project_directory.py` 调用 |
| `tunnel/` | 3 文件 / 411 行 | 把本地端口暴露成公网 URL：`tunnel/cloudflare.py:34 CloudflareTunnelDriver`（跑 `cloudflared tunnel --url`，`:21 _URL_RE` 抓 `*.trycloudflare.com`）、`tunnel/binary_manager.py` 负责二进制下载 | 本地模式的远程访问入口；依赖外部 `cloudflared` |
| `backup/` | 20 文件 / 3716 行 | 备份/恢复编排：`backup/manager.py:53 BackupManager`、`:32 BackupOperationConflict`、`backup/orchestration.py`、`backup/models.py`、`backup/_ops/{create,restore,storage}.py`、`backup/_utils/safe_swap.py`，并含**签名/信任链** `backup/_utils/signing/{digest,key,resign,trust}.py` | `app/routers/backup.py`（含 `:142` SSE 进度流） |
| `portability/` | 34 文件 / 12606 行 | 从**其他 AI Agent 产品**迁移数据：`portability/importer.py`、`planner.py`、`import_planning.py`、`import_jobs.py`、`selection.py`、`transaction_journal.py`、`skill_transfer.py`，以及 `providers/{base,codex,qoder}.py` 和 `codex_sessions/schedules`、`qoder_sessions/schedules`；还有 `adaptation_*` + `compatibility*` 的兼容性改写闭环 | `app/routers/portability_imports.py`（含 `:222` SSE） |
| `envs/` | 3 文件 / 605 行 | Console 可管理环境变量的元数据与加密持久化：`envs/registry.py:19 EnvVarSpec`（`mutability ∈ hot_runtime|startup_only`，`:35 ENV_VAR_SPECS`）、`envs/store.py`（`SECRET_DIR/envs.json` + `os.environ` 双写） | `app/routers/envs.py` |
| `local_models/` | 6 文件 / 2819 行 | 本机 llama.cpp 运行时：`local_models/manager.py:41 LocalModelManager`、`local_models/model_manager.py:23 LocalModelConfig`、`local_models/llamacpp.py`（下载/启停/进度）、`local_models/download_manager.py`、`local_models/tag_parser.py` | `app/routers/local_models.py` |
| `market/` | 10 文件 / 1282 行 | 技能/插件市场**聚合搜索**：`market/service.py`、`market/categories.py`、`market/schema.py`、`market/providers/{base,aliyun,clawhub,modelscope,qwenpaw}.py`（`providers/base.py` 定义 provider 契约） | `app/routers/market.py` |
| `tauri/` | 6 文件 / 723 行 | 桌面侧车**薄适配层**：`tauri/entry.py:382 main()`、`tauri/cli_entry.py`、`tauri/backend_guard.py`（`reconcile_singleton_backend` 保证单实例）、`tauri/env.py`、`tauri/sidecar_logging.py` | 由 `console/src-tauri` 的 Rust 壳拉起；真正复杂度在 Rust 侧 |

---

## 9. 打包与分发

- **Python 包**：`pyproject.toml` setuptools，`packages.find.where = ["src","packages/qwenpawmail-mcp/src"]`；`[project.scripts] qwenpaw / copaw / qwenpawmail-mcp`；可选依赖按能力切 `test / dev / local / hub(docker) / whisper / codex / qoder / qwenpaw-data / full / sip / sip-livekit`。`package-data` 内的 `console/**`、`docs/*.md`、`security/*/rules/**`、`providers/data/**`、`browser/control_link/injected/*.js`、`app/channels/yuanbao/proto/**` 是关键"打包即产物"声明。注意 main 的 package-data 里**没有**任何 `server/migrations/*.sql`。
- **wheel/sdist**：`scripts/wheel_build.sh` 三件事——建 console、把 `website/public/docs/*.md` 拷进 `src/qwenpaw/docs/`、`python -m build`。
- **Docker**：`scripts/docker_build.sh` → `deploy/Dockerfile`（stage1 建 console；stage2 装 `supervisor + xfce4 + xvfb + chromium`，`uv pip install . "setuptools<82"`，`EXPOSE 8088`）；发布走 `docker-release.yml`。**镜像里跑的是 xfce4 桌面 + Chromium**，因为 browser/computer-use 能力要真实显示环境。
- **桌面**：`console/src-tauri/tauri.conf.json`（`productName="QwenPaw Desktop"`、`bundle.targets=["app","nsis"]`、`resources` 内嵌 `qwenpaw-backend` / `python-runtime` / `node-runtime`）配 `scripts/pack-tauri/`；发布链 `desktop-build → desktop-release → desktop-promote/desktop-publish`。另有非 Tauri 的 `scripts/pack/` 传统安装链与 `qwenpaw desktop`（pywebview）。
- **插件/独立包**：`plugins/`（1127 个文件）+ `scripts/pack/generate_plugin_metadata.py` 一类脚本 + `plugins-release.yml`；`packages/qwenpawmail-mcp` 是独立包（独立 `[project.scripts]`）。

---

## 10. 分层边界与明显问题

**边界清晰之处**

1. **Hub 的能力靠 ABC 收口**：`hub/provisioner.py:31 RuntimeProvisioner` 只暴露 `preflight/start/stop/status/close`，`hub/service.py` 不碰容器/进程细节；`preflight()` 让"边界是否真的可强制"成为启动前可判定的事实，而非运行期祈祷。
2. **密钥不落明文**：`security/secret_store.py` 的 Fernet + keyring 优先 + 容器内主动跳过 keyring；`hub/credentials.py` 每租户/每 runtime 独立派生 `QWENPAW_RUNTIME_INTERNAL_TOKEN`。
3. **`envs/registry.py` 把"能否热改"变成类型**（`mutability: hot_runtime | startup_only`），比散落的 `if os.getenv(...)` 可审计得多。
4. **单一前端 + 同源**：`console/src/api/config.ts` + `vite.config.ts` 是前后端唯一耦合点，生产同源托管，Hub 复用同一份 dist，没有第二套 UI 的同步负担。
5. **CI 强制测试分级**：`tests.yml:439-452` 用脚本拒绝无优先级标记的 integration 测试，避免"新测试悄悄落在三条分片之外"。

**明显问题（具体举例）**

1. **巨型模块**：`src/qwenpaw/agents/tools/deprecated_browser/browser_control.py` **5629 行**（名字写着 deprecated，仍在包内且计入 package-data）；`src/qwenpaw/config/config.py` **3956 行**单个文件承载全部配置模型；`app/routers/workspace.py` 2317、`app/routers/agents.py` 2128、`app/routers/skills.py` 2023；渠道侧 `app/channels/dingtalk/channel.py` 3845、`matrix/channel.py` 3525。
2. **Hub 用 SQLite 当控制面，且 schema 明确"未发布"**：`hub/database.py:11 _SCHEMA_GENERATION = "hub-v1"`，`:38-41` 不兼容时直接 `RuntimeError("Unsupported pre-release Hub database schema. Back up and recreate control.db ...")`。`hub/control_app.py` 单文件 1598 行承载全部控制面端点 + HTTP 反代 + WS 反代 + SPA 托管，是"能跑但难改"的典型。
3. **多用户没有任何仓库内文档**：Hub 是 main 的核心卖点（`README_zh.md:65`），但 `docs/` 只有 `docs/design/environment-management-redesign.md`；`grep -rn "hub" README_zh.md` 除新闻条目外无部署/运维章节。Hub 的 20+ 端点、`security_level` 语义、`--force-public` 的安全前提全靠读源码。
4. **文档与实现零机器校验**：`DOCS_ENABLED` 默认 False（`constant.py:264`）→ `/openapi.json` 默认 404；仓库内**没有** API 参考文档，用户文档在 `website/public/docs/`（80 个 md）与代码分仓同步，且没有任何 CI 步骤比对二者。
5. **单进程硬约束与扩展路径割裂**：`cli/app_cmd.py:165 workers=1`（`--workers` 明示弃用），要横向扩展只能上 Hub；而 Hub 的控制面 DB 是 SQLite（`hub/database.py:24 connect_hub_database`，WAL + `busy_timeout=5000`），且默认拒绝非 loopback 绑定（`cli/hub_cmd.py:72-76`），`--force-public` 之后的安全责任完全交给部署者。
6. **两套并行的鉴权边界**：`app/auth.py:735 _should_skip_auth` 只在 `path.startswith("/api/")` 时生效（`:746`），而 `app/_app.py:915` 的 `voice_router` 挂在根路径（`app/routers/voice.py:85 "/voice/incoming"`、`:125 "/voice/ws"`、`:164 "/voice/status-callback"`），因此**完全绕过** `AuthMiddleware`，改由 Twilio 签名校验 `app/routers/voice.py:42 _validate_twilio_signature` 与 WS query token（`:140-143`）负责。这是有意的（Twilio 无法携带 bearer token），但意味着"哪些路径受保护"分散在中间件白名单、路径前缀判断和路由自带依赖三处，无法从 `_PUBLIC_PATHS` 一处读懂。
7. **platform 分裂的隔离实现**：`hub/process_isolation.py`（`linux-bubblewrap` / `macos-seatbelt` / `unsupported`）+ `hub/windows_process_isolation.py` + `hub/windows_reverse_tunnel.py` + `hub/windows_runtime_bridge.py` 共四份平台特化，占 `hub/` 7475 行中的约 1100 行，三平台行为等价性没有集中测试。
8. **import 期副作用**：`app/_app.py:787 _CONSOLE_STATIC_DIR = _resolve_console_static_dir()` 在模块导入时就决定静态目录并 `logger.info`，`:878-894` 又用 `# pylint: disable=wrong-import-position` 在文件尾部追加 import 与路由注册——应用装配顺序高度依赖"文件里代码写在哪儿"。

---

## 11. 一句话总结

官方 main 的服务端是**"个人单体应用（`qwenpaw app`，单进程/单账号）+ Hub 多用户托管控制面（`qwenpaw hub`，每用户一个隔离 runtime，同源复用同一份 React console）"**两层结构：没有 `/v1`、没有共享多租户服务端、没有仓储协议与分布式租约，可扩展性靠"复制完整实例 + 边界 preflight"而不是"共享进程内的行级隔离"。真正值得借鉴的是 `hub/provisioner.py` 的边界契约、`security/secret_store.py` 的密钥链、`envs/registry.py` 的可热改类型，以及 CI 里"未分级测试直接失败"的门禁；需要警惕的是 5629 行的 deprecated 模块、Hub 的 SQLite 控制面与"零仓库内文档 + 零机器校验"的文档状态。

---

### 附：与旧报告的逐条审计

见最终回复中的审计表；核心分界线是 `/Users/yangxuezhen/git/QwenPaw` 的 `multisync = origin/main + 4 个本地提交`——旧报告中所有依赖 `src/qwenpaw/server/`、`/v1`、`test-tools/`、`deploy/server/`、`docs/{v1-api,api-reference,tdsql-storage,multi-user-server,multi-user-tl,streaming-tool-console,tl-provider}.md`、`src/qwenpaw/providers/tl_*.py`、`src/qwenpaw/cli/serve_cmd.py` 的结论，在 main 上均无对应实体。
