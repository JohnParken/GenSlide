> ⚠️ **基线错误警示（后续已修正）**：本报告分析的是本地 `multisync` 分支 = `origin/main` + 4 个本地提交，
> **不是 QwenPaw 官方代码**。第 2.2/2.3/2.4/3 节描述的 `/v1` 多用户服务、Repository 契约、TDSQL/S3、
> worker/controller/sandbox 五进程、`deploy/server`、`docs/v1-api.md` 等**全部是本地新增**，官方 main 上不存在。
> 请以 `qwenpaw-main-server-arch.md` 为准。

# QwenPaw 服务端 / 应用 / CLI / 多用户 / 部署架构分析

> 目标仓库（只读）：`/Users/yangxuezhen/git/QwenPaw`
> 规模：`src/qwenpaw/` 下 1011 个 `.py`，合计 347,186 行；另有 `console/`（React 前端 + Tauri 壳）、`deploy/`、`test-tools/`、`e2e/`、`plugins/`、`packages/`、`website/`。
> 结论口径：本报告只描述当前代码；`cloud/`、`deploy/office-node/`、`src/qwenpaw/office/` 经 `git ls-files` 验证为 **0 个被跟踪文件**（仅剩 `__pycache__` / `node_modules` / `.pytest_cache` 残留），不计入架构。

---

## 0. 全局骨架

```
                    ┌──────────────────────────────────────────────┐
  浏览器 console ──▶ │ 个人模式 app  (qwenpaw app, :8088)            │ 单账号
  (React, vitest)   │ src/qwenpaw/app/_app.py:725 FastAPI          │ AuthMiddleware(可关)
                    │  /api/**  + /api/agents/{id}/** 镜像          │ config.json + envs.json
                    └──────────────────────────────────────────────┘
                    ┌──────────────────────────────────────────────┐
  native-console ─▶ │ 多用户 /v1  (qwenpaw serve api, :8090)        │ BFF：令牌+用户头
  (vanilla TS)      │ src/qwenpaw/server/api.py:46 create_api      │ TDSQL + S3
                    └───────┬──────────────────────────────────────┘
                            │ Repository Protocol (server/contracts.py)
              ┌─────────────┴───────────────┐
       Worker(:—)                      Controller(:8091)
   server/worker.py:Worker.serve   server/controller.py:437
   模型调用 / Agent 执行             Kubernetes Sandbox 生命周期
                                          │ 内部令牌
                                    Sandbox(:8092)
                              server/sandbox_app.py:19（无 DB/模型凭据）

                    ┌──────────────────────────────────────────────┐
  Hub 控制面 ─────▶ │ qwenpaw hub (:8088) hub/control_app.py:131    │ SQLite control.db
  (多用户/多实例)    │ /api/hub/** + 反向代理 /api/{path} + WS 代理  │ 每用户一个 personal runtime
                    └──────────────────────────────────────────────┘
```

---

## 1. 进程与部署形态（4 条并行路线）

| 形态 | 入口命令 | 启动链路 | 证据 |
| --- | --- | --- | --- |
| 本地单机 CLI/TUI | `qwenpaw`（无子命令）/ `qwenpaw app` | `LazyGroup` 懒加载 27 个子命令；裸调用进 TUI | `cli/main.py:60,131-188,219-222` |
| 单机/容器 App | `qwenpaw app [--host --port]` | `uvicorn.run("qwenpaw.app._app:app", workers=1)`，默认 `127.0.0.1:8088`，强制单 worker（`--workers` 弃用警告） | `cli/app_cmd.py:110-172` |
| Docker 单机 | `docker run …` | `deploy/entrypoint.sh` 用 `envsubst` 渲染 `deploy/config/supervisord.conf.template` → supervisord 起 `dbus / xvfb(:1) / xfce4 / app`，app 即 `qwenpaw app --host 0.0.0.0` | `deploy/Dockerfile`、`deploy/entrypoint.sh`、`docker-compose.yml` |
| 桌面 A：pywebview | `qwenpaw desktop` | `WebViewAPI`（`open_external_link/save_file`）+ `get_stable_port` | `cli/desktop_cmd.py:26-120` |
| 桌面 B：Tauri v2 | `console/src-tauri` 打包 | Rust 壳拉起 Python sidecar；`qwenpaw-backend` 由 PyInstaller 冻结，入口 `tauri/entry.py:main()`：`mp.freeze_support` → `reconcile_singleton_backend` → 复用端口文件 `desktop_port` → 导入 `qwenpaw.app._app:app` 直接跑 uvicorn，并打印 ready 前缀给 Rust | `console/src-tauri/tauri.conf.json`、`src/qwenpaw/tauri/entry.py`、`tauri/backend_guard.py`、`scripts/pack-tauri/qwenpaw.spec:276-318` |
| 多用户服务 `/v1` | `qwenpaw serve migrate\|api\|worker\|controller\|sandbox` | 5 个独立进程；`_components()` 强制 TDSQL（显式拒绝 `memory://`） | `cli/serve_cmd.py:79-219` |
| Hub 控制面 | `qwenpaw hub [--force-public]` | `run_hub_app` → `create_hub_app`，为每个用户 provision 一个 runtime（Docker 容器或本地隔离进程）并反向代理 | `cli/hub_cmd.py:14-105`、`hub/control_app.py:131-1460` |

`pip install '.[server]'` 提供 FastAPI/aiomysql/psycopg/redis/boto3；`deploy/server/Dockerfile` 的 `ENTRYPOINT ["qwenpaw","serve"]`、`CMD ["api"]`，K8s 清单为 `deploy/server/kubernetes.yaml`（ConfigMap `qwenpaw-platform`、SA/Role `qwenpaw-controller`、PVC `qwenpaw-workspaces`、Job `qwenpaw-migrate`、三个 Deployment、`NetworkPolicy` 在 364 行）。**端口约定**（`docs/multi-user-server.md`）：API 8090 / Controller 8091 / 本地 TL 与 Sandbox 8092 / TL Proxy 8089 / 个人 8088 / 联调前端 5179。

---

## 2. HTTP API 设计

### 2.1 个人后端（BFF 与执行未分离，单体应用）

- **装配**：`app/_app.py:725` 创建 `FastAPI(lifespan=…)`；中间件顺序 `AgentContextMiddleware → AuthMiddleware → RuntimeBoundaryMiddleware → CORS(可选)`（`:734-747`）。
- **路由组织**：`app/routers/__init__.py` 聚合 **33 个子路由**，统一挂 `/api`（`_app.py:874`），另再挂一份 **agent 作用域镜像** `/api/agents/{agentId}/…`（`routers/agent_scoped.py:67-107`，镜像 12 个子路由），文档称顶层 386 个操作、镜像 231 个（`docs/api-reference.md:1-16`）。仓库内 356 处路由装饰器（`app/routers/` + `app/_app.py`）。
- **静态资源**：`_resolve_console_static_dir()` 按 `QWENPAW_CONSOLE_STATIC_DIR` → `src/qwenpaw/console/` → `<repo>/console/dist` → `cwd/console/dist` 顺序探测，最后用 catch-all 做 SPA fallback（`_app.py:760-971`）。
- **鉴权**：`AuthMiddleware`（`app/auth.py`）为 **单账号** 设计（文件头注释明说 "Single-user design"），仅当 `QWENPAW_AUTH_ENABLED` 为真才启用；凭据存 `SECRET_DIR/auth.json`（加盐 SHA-256），token 7 天，`_PUBLIC_PATHS` 白名单放行 `/api/auth/*`、`/api/version`、`/api/frontend_plugin`；另有 `x-qwenpaw-runtime-token` 头用于 Hub 代理注入（`app/auth.py:35-70`）。登录限流为内存 `LoginRateLimiter`（IP + 账号双维度锁定，`app/rate_limiter.py:10-40`）。
- **文档默认关闭**：`DOCS_ENABLED = QWENPAW_OPENAPI_DOCS`（默认 False），即 `/docs`、`/openapi.json` 默认 404（`constant.py:264`）。`docs/api-reference.md` 是人工执行命令后生成的快照（文末"如何重新生成"），**仓库无生成脚本、无 CI 一致性校验**。
- **错误模型**：`app/exception_handlers.py:register_exception_handlers`（`_app.py:732`）统一注册。

### 2.2 多用户 `/v1`（BFF 与执行分离）

`server/api.py:46 create_api` 创建 **`docs_url=None, redoc_url=None`** 的 FastAPI，只有 8 类资源路由：

```
POST /v1/runs(202)                      GET  /v1/runs/{id}
POST /v1/runs/{id}/cancel               GET  /v1/runs/{id}/events  (SSE)
GET  /v1/sessions?limit<=100            GET  /v1/sessions/{id}/messages?after&limit
POST /v1/approvals/{id}/decision        POST /v1/files(201) / GET /v1/files/{id} / …/content / DELETE
DELETE /v1/memory(204)                  POST /v1/sessions/{id}/files
GET  /v1/assistant                      GET /health / GET /ready / GET /internal/metrics
```

- **鉴权/租户**：`principal()` 依赖用 `hmac.compare_digest` 比对 `Authorization: Bearer <service_token>`（错→401），要求 `X-QwenPaw-User`（缺/超 256 字符→400）；`POST /v1/runs` 还要求 `body.usrid == 用户头`，否则 403（`api.py:61-70,102-106`）。**注释明确"BFF-only API. The browser never authenticates directly to this service."**（`api.py:1`）。
- **错误模型**：`NotFound→404`、`Conflict→409` 两个异常处理器（`api.py:53-59`），单据/审批超时/归属错误由 `server/contracts.py` 的 `NotFound/Conflict/LeaseLost/ToolOutcomeUnknown` 表达。
- **流式**：`GET /v1/runs/{id}/events` 手写 `StreamingResponse`：每条事件 `id: <seq>` + `data: <json payload+persisted_at>`，终态时 `event: end`，空闲 1s 发 `: keepalive`；`Last-Event-ID` 非法→400；连接断开不取消运行（`api.py:179-242`）。服务端扇形分发由 `server/event_feed.py:EventFeeds` 完成（每 `(user,run)` 一个 DB 轮询任务、`deque(maxlen=256)`、多订阅者共享）；Redis 只做唤醒加速——`server/notifications.py:1` 注释 "database polling always remains authoritative"。
- **版本化**：没有 `/v2`、没有 Accept 协商；`/v1` 是唯一主接口，个人后端 `/api/console/chat` 被文档定位为"兼容接口"（`docs/multi-user-server.md`）。
- **契约与文档同步性**：`docs/v1-api.md` 的字段/状态码/SSE 事件与 `api.py` **逐条对得上**（`usrid/sessionid/channelid/request_id/message/debug/attachments`、`Field(max_length=100000)`、`attachments max_length=20`、`413/409/401/400`）。但 `/v1` 自身无 OpenAPI，同步靠人；Hub 的 `/api/hub/**`（20+ 端点）完全不在 `api-reference.md` 内。

### 2.3 一次 `/v1` 请求的端到端链路（代码级时序）

```
BFF/浏览器 ──POST /v1/runs──▶ api.py:submit()
   │  1. principal(): 校验 Bearer service_token + X-QwenPaw-User        (api.py:61)
   │  2. body.usrid != 用户头 → 403                                       (api.py:105)
   │  3. 逐个 repository.file(usrid, file_id) 校验附件归属                 (api.py:107)
   │  4. 若 model_protocol == "tl" 且有附件：S3 取字节 → extract_attachment
   │     → 单文件 10MiB / 合计 100000 字符限制                              (api.py:111-132)
   │  5. definition 变化才 put_definition(version, payload)（不可变快照）   (api.py:133-136)
   │  6. repository.submit(usrid, channelid, sessionid, request_id, …)     (api.py:137)
   └──▶ 202 + run 记录（idempotent：同 user 同 request_id 复用；载荷不同→409）

Worker 侧（独立进程）：
   Worker.serve → Semaphore(concurrency) → repository.claim(worker_id, lease, user_limit)
   → group.create_task(execute(run)) → runtime.runtime.Runtime → 模型调用          (worker.py:31-60)
   → 工具：RemoteTools/ToolGateway → POST controller:/invoke                        (tools.py:15-61)
   → Controller: acquire/ensure K8s Pod → POST sandbox:/invoke（NDJSON 流）          (controller.py:189-364)
   → 每个生命周期节点 repository.append(seq++) 写事件；Redis 唤醒订阅者              (event_feed.py)

SSE 订阅者：
   GET /v1/runs/{id}/events → feeds.subscribe → feed.after(cursor)
   → 命中 deque(maxlen=256) 直接返回；落后太多则回源 repository.events(after)       (event_feed.py:88-98)
```

配套的**故障语义**在 `server/contracts.py` 里显式建模：`LeaseLost`（epoch 失效，不能再写）、`ToolOutcomeUnknown`（工具结果未知，直接 `CancelledError` 停止推理，**不允许模型重试/重放**）；`docs/tdsql-storage.md` 补充"过期 worker 不能写、租约过期本身不释放会话、确认沙箱终止必须先于中断"。这是本仓库最成熟的一处设计：把"分布式不确定性"变成类型，而不是靠日志排查。

### 2.4 三层服务契约

`server/contracts.py` 是唯一的服务边界文件：`Repository`（Protocol，声明 migrate/submit/claim/heartbeat/append/finish/approval/file/memory 等），`ExecutionContext(user_id, session_id, run_id, epoch, worker_id)`，`current_execution` ContextVar，`LeaseLost`、`ToolOutcomeUnknown`。存储实现另有 `server/storage/CONTRACT.md`。

---

## 3. 多用户能力

| 关注点 | 实现位置 |
| --- | --- |
| 用户/租户身份 | `/v1`：`X-QwenPaw-User` 传入 `Repository` 所有方法，协议要求"他人资源一律当 NotFound"（`server/contracts.py:47-56`）；Hub：`hub/models.py:RuntimeSpec(tenant_id, owner_user_id)` + `HubAuthService`（`hub/auth.py:67`，SQLite 用户名/密码 + 签名 token）+ `HubAccessSecurity`（`hub/access_security.py`） |
| 会话映射 | 外部 `(usrid, channelid, sessionid)` ↔ 内部会话 UUID；`POST /v1/runs` 返回的 `session_id` 是内部 UUID（`docs/v1-api.md`） |
| 工作区隔离 | `/v1`：`ServerConfig.workspace_root=/workspaces` + PVC `qwenpaw-workspaces`，Controller 按 session 建 Pod（`server/controller.py:Kubernetes.manifest`）；个人模式：`app/workspace_registry.py:WorkspaceRegistry`（继承 `MultiAgentManager`）为每个 agent 建 `Workspace`（`app/workspace/workspace.py`） |
| 并发与配额 | `server/config.py:77-81`：`concurrency=10`、`per_user_concurrency=4`、`lease_seconds=60`、`approval_timeout=600`、`idle_seconds=900`；`Worker.serve` 用 `asyncio.Semaphore(concurrency)` 做准入（`server/worker.py:31-60`），`claim(worker_id, lease, user_limit)` 抢单 |
| 限流 | 个人登录：`app/rate_limiter.py:LoginRateLimiter`；Hub 代理：`hub/proxy_limits.py` |
| 存储 | `server/storage/__init__.py:create_repository`：`memory://`→`MemoryRepository`（SQLite `:memory:`，仅供测试），`mysql:// mariadb:// tdsql://`→`TDSQLRepository`；`serve` 显式拒绝 `memory://`（`cli/serve_cmd.py:85-88`）。表前缀 `qp_service_`（正则校验，`server/config.py:66-68`），迁移 `server/migrations/001_initial.sql … 006_session_queue.sql`，DDL 版本化 + checksum（`docs/tdsql-storage.md`）。隔离/锁语义：InnoDB、READ COMMITTED、行锁，按 user→session→run 顺序加锁，`session.active_run_id` 为串行点，worker 过期后不可写 |
| 对象存储 | `server/objects.py:S3Objects`（boto3，`uploads/<uuid>` key，presigned URL 300s），上传上限 `max_upload_bytes=20 MiB`（`server/config.py:89`） |
| 长期记忆 | `server/memory.py:MemoryService` + `004_memory_jobs.sql`（队列、per-user 写者互斥、lease、epoch）；`DELETE /v1/memory` 撤销待处理任务 |
| Hub 多用户 | 每个用户一个 personal runtime，Docker 或本地隔离进程；`hub/provisioner.py:RuntimeProvisioner` 抽象，`docker_provisioner.py`（`security_level="isolated-container-shared-kernel"`）与 `local_provisioner.py`（`isolated-local` / `isolated-local-shared-network` / `isolated-local-windows-appcontainer`）；凭据放 `hub/credentials.py:TenantCredentialVault`（vault key 在 `<root>/secrets/.vault_key`） |

**重要澄清**：仓库里存在三套互不相同的"多用户"——(a) `/v1` 服务（服务令牌 + 用户头，TDSQL 隔离，面向平台方）；(b) Hub（真实登录/注册/管理员/多 runtime，SQLite 控制面，面向"一台机器多人用"）；(c) 个人模式的 `AuthMiddleware`（单账号，仅防局域网裸奔）。它们共享 console 静态资源与部分 API 语义，但不共享用户表或令牌。

---

## 4. 前端 console

- **技术栈**（`console/package.json`）：React 18 + TypeScript 5.8 + Vite 6.4.1 + antd 5.29.3 + `zustand` 5（状态，`console/src/stores/*.ts`）+ `react-router-dom` 7 + `@agentscope-ai/chat|design|icons`（私有源，版本是时间戳 beta）+ lexical 0.48 + monaco 0.55 + mermaid/katex + `react-markdown`；测试 vitest 4 + testing-library + jsdom；i18next 多语言（`console/src/locales/`）。
- **构建**：`npm run build` = `tsc -b && vite build && verify:monaco-css && precompress && verify:initial-bundle`；产物 `console/dist` **不入库**（`.gitignore:39 src/qwenpaw/console/dist/`），由 `scripts/wheel_build.sh`、`deploy/Dockerfile`（console-builder 阶段）、CI 三处拷贝到 `src/qwenpaw/console/`。桌面用 `npm run build:tauri-bootstrap` → `dist-tauri`（`tauri.conf.json:build.frontendDist`）。
- **与后端通信**：`VITE_API_BASE_URL` 默认空 = 同源（`console/src/api/config.ts:1-12`）；dev server 把 `/api` 代理到 `http://localhost:8088`（`console/vite.config.ts:proxy`），生产由 Python 同进程托管静态资源 → **同源、无 CORS 依赖**。
- **流式渲染**：聊天走 `fetch("/api/console/chat")` + ReadableStream（**不是 EventSource**），三处调用点 `console/src/pages/Chat/index.tsx:528,3022,3847`；会话内再自研 `replayFastForward.ts`、`tlPreview.ts`、`approvalScope.ts` 等做重放/预览/审批作用域。
- **关键耦合事实**：产品前端只消费**个人后端** `/api/console/chat`（native `AgentRequest` 协议）。`/v1` 的持久化运行 + `tool/tool_output/approval/preview_*/terminal` 事件语义由**另一套前端**消费：`test-tools/native-console/src/service.ts:ServiceClient`（vanilla TS，`cursor` + `reconnectAttempts` + `onReconnect`），其 `vite.config.ts` 在**代理层**注入 `authorization: Bearer <token>` 与 `x-qwenpaw-user`，浏览器永不持有服务令牌。也就是说：多用户 UI 目前是"联调/验收前端"，不是产品前端；`docs/multi-user-tl.md` 中"前端默认选中服务聊天 /v1"指的就是它。
- **桌面 Tauri**：`console/src-tauri/`（`Cargo.toml` 两个 bin：`qwenpaw-desktop`、`qwenpaw-computer-use-helper`；`lib.rs`、`backend.rs`、`backend_download.rs`、`updates.rs`、`tray.rs`、`webview_recovery.rs`、`capabilities/default.json`），bundle `targets=["app","nsis"]`，`resources=["binaries/qwenpaw-backend","binaries/python-runtime","binaries/node-runtime"]` —— 即**自带 CPython 与 Node 运行时**。
- **耦合度评估**：前后端契约靠**手写 TS 类型 + 手写文档**同步；无 OpenAPI 代码生成，事件类型字符串在前后端各写一遍（`docs/streaming-tool-console.md` 的事件表 ↔ `native-console` 的手写解析）。这是主要的漂移面。

---

## 5. 配置体系

- **目录优先级**（`constant.py:82-113,270-279`）：`QWENPAW_WORKING_DIR` > 存在 `~/.copaw`（legacy）> `~/.qwenpaw`；`SECRET_DIR` 默认 `{WORKING_DIR}.secret`（可 `QWENPAW_SECRET_DIR` 覆盖）；`BACKUP_DIR` 默认 `{WORKING_DIR}.backups`。
- **`.env` 加载顺序**：仓库根 `.env` → `WORKING_DIR/.env`（`load_dotenv(..., override=False)`，即先加载者优先）；`QWENPAW_SERVER_MODE=1` 时**两者都跳过**（`constant.py:9,100`）——服务端配置因此与个人配置彻底分离。
- **个人配置**：`config/config.py`（**3956 行**，`Config`/`AgentProfileRef`/各 channel config）；`envs/store.py` 把环境变量持久化到 `SECRET_DIR/envs.json` 并注入 `os.environ`，`envs/registry.py:EnvVarSpec` 用 `mutability ∈ {hot_runtime, startup_only}` 标注"能否在 Console 热改"（`envs/registry.py:20-27`）。
- **密钥**：`security/secret_store.py` 用 Fernet（AES-128-CBC + HMAC-SHA256），master key 优先存 **OS keyring**（service `qwenpaw`，兼容 legacy `copaw`），回退 `SECRET_DIR/.master_key`（0600）；密文带 `ENC:` 前缀，读取时透明迁移；无 keyring 环境（Docker/Linux 服务器）自动跳过。
- **服务端配置**：`ServerConfig.from_env()` 只扫描 `QWENPAW_SERVER_<FIELD>`（`server/config.py:91-104`），敏感项一律 `SecretStr`（`database_url/service_token/internal_token/model_api_key`），两个 token <32 字符直接报错；`definition_path` 指向 `AssistantDefinition`（frozen、`extra="forbid"`），`version` 是不可变快照，`api.py:133-136` 只在 version 对应 payload 变化时 `put_definition`。**不读个人 profile**（模块 docstring 明说）。`.env` 不自动加载（`docs/multi-user-server.md`），`qwenpaw serve check --json` 只做离线校验、不连库、不输出凭据（`cli/serve_cmd.py:15-76`）。
- **Hub 配置**：`hub/config.py:HubConfig` + `HubConfigStore`（存 control.db），`RuntimeService.apply_config` 支持不重启热更新（`hub/service.py:87`）。
- **多环境 profile**：没有 `--profile` 概念，等价物是 ①`QWENPAW_SERVER_DEFINITION_PATH` 指向不同 `assistant*.json`（`deploy/server/` 有 `assistant.json`/`assistant.tl.json`/`assistant.tl.local.json` 三份）；②`test-tools/native-console` 的 `service:tl` / `live:start` npm 脚本按端口/令牌环境变量切换本地 profile。

---

## 6. 测试与质量保障

- **规模**：`tests/unit` 585 个测试文件、`tests/integration` 197、`tests/contract` 24、`tests/e2e` 1（`tests/e2e/test_hub_local_runtime.py`）；根目录另有独立 Playwright 框架 `e2e/`（Page Object：`e2e/pages`、`e2e/fixtures`、`e2e/tests`、`pytest.ini`），README 明确警告必须隔离 working dir。
- **标记与门槛**（`pyproject.toml [tool.pytest.ini_options]`）：`p0/p1/p2` 优先级 + `unit/contract/integration/e2e` 层级 + `slow`、`manual_real`；覆盖率 `fail_under = 50`、`source=["src/qwenpaw"]`。`Makefile` 提供 `test/test-unit/test-contract/test-integration/coverage-full/check-contracts/quick`（`quick` 会临时 `QWENPAW_WORKING_DIR`）。
- **两套"契约测试"含义不同**：
  1. `tests/contract/` = **子类契约框架**（`BaseContractTest → ChannelContractTest → TestDingTalkChannel…`），目的是"改基类不要打破其他渠道"，由 `scripts/check_channel_contracts.py` 检查（`Makefile:check-contracts`）。
  2. `tests/unit/server/test_storage_contract.py` = **存储契约**：`@pytest_asyncio.fixture(params=["memory","tdsql"])`，TDSQL 分支需 `QWENPAW_TEST_TDSQL_URL` + `QWENPAW_TEST_TDSQL_ALLOW_DDL=1`，否则 `pytest.skip`（不是"通过"）；每个测试只用随机前缀 `qp_test_<uuid>_` 建表并删除（`test_storage_contract.py:22-45,334`）。`docs/tdsql-storage.md` 记录本地结果 35 passed / 17 skipped，并明说**没有真实 TDSQL 实例**、11 个 TDSQL 用例是 skip。
  3. `tests/unit/server/test_service.py` 用 `MemoryRepository` + 确定性模型 + 真实 `Worker`（`create_api`+`Worker` 组合），跑的是同一套仓储状态机。
- **CI**（`.github/workflows/`，35 个 workflow）：
  - `tests.yml`（主门禁）：`spam-gate` → `changes`（`dorny/paths-filter` 替代 `paths:`）→ `approval-gate`（`maintainer-approved` environment）→ 三级矩阵 `unit / contract / integrated`（py3.11、3.13 on ubuntu + macos/win py3.11；integrated 分 shard）。集成层默认 PR 只跑 `integration and p0`，pre-merge 跑 `p0+p1`，并**强制所有 integration 测试带 p0/p1/p2 标记**，否则 `::error::` 失败（`tests.yml:399-445`）。另外直接跑 `pytest tests/e2e/test_hub_local_runtime.py`（`:180`）。覆盖率数据由各层单点采集（ubuntu+py3.13）后由 `coverage-report` 合并，不在报告 job 重跑 pytest。
  - `full-tests-nightly.yml`（`cron 17 17 * * *`）：全量 unit+contract+integration（含 p2）+ E2E Playwright + console vitest + 四层覆盖率。
  - 专项：`frontend-tests.yml`、`e2e-smoke.yml`、`e2e-integration.yml`（仅 dispatch）、`codeql.yml`、`pre-commit.yml`、`real-behavior-proof.yml`（`scripts/github/real_behavior_proof_check.py`）、`release-verify.yml`（被 release 流程复用为 verify 阶段）。
- **verify 脚本**：`scripts/verify/desktop_verify.py`（对已启动的桌面后端依次校验 `/api/version`、`/`、memory reindex、模型 key 写入、`PUT /api/models/active`，并用 Playwright 驱动真实 SPA 发一条问答断言回答含 "Everest"；`--ui-mode tauri-macos|tauri-windows`）、`scripts/verify/launch_tauri_macos.sh`、`launch_tauri_windows.ps1`；`scripts/pack/verify_desktop_artifacts.py`、`verify_github_release_assets.sh`。
- **缺口**：`docs/api-reference.md`（386/231 操作计数）无生成脚本、无 CI 校验；`docs/v1-api.md` 亦无机器校验；TDSQL 关键路径（InnoDB 锁、死锁、容量、100 任务/500 SSE 30 分钟压测）在文档中明确标为**未完成**（`scripts/server_load_test.py` 存在但需真实环境）。

---

## 7. 打包与分发

- **Python 包**：`pyproject.toml` setuptools；`packages.find = {where=["src","packages/qwenpawmail-mcp/src"]}`；`package-data` 显式收录 `console/**`、`agents/skills/**`、`agents/md_files/**`、`docs/*.md`、`server/migrations/*.sql`、`security/*/rules/**`、`providers/data/**` 等；脚本入口 `qwenpaw` / `copaw` / `qwenpawmail-mcp`；可选依赖按能力切分：`server`(fastapi/aiomysql/redis/boto3)、`hub`(docker)、`local`、`whisper`、`codex`、`qoder`、`sip`、`sip-livekit`、`qwenpaw-data`、`full`、`test`、`dev`。`scripts/wheel_build.sh` 先建 console、把 `website/public/docs/*.md` 拷进 `src/qwenpaw/docs/`，再 `python -m build`。
- **Docker**：`scripts/docker_build.sh` → `deploy/Dockerfile`（ARG `NODE_IMAGE`/`UV_IMAGE` 走 ACR 私有源；stage1 建 console，stage2 装 chromium+Xvfb+xfce4，`uv pip install . "setuptools<82"`，port 8088）。发布 `docker-release.yml`（`release-verify` 先启动健康检查 → 多架构 build/push 到 DockerHub + Aliyun ACR）。
- **PyPI**：`publish-pypi.yml`（build → 装 wheel 并启动健康检查 → publish）。
- **Tauri/桌面**：`scripts/pack-tauri/`：`qwenpaw.spec` 打两个 EXE（`qwenpaw-backend` 与 `qwenpaw` CLI，含 qoder/codex 二进制与 ReMe/whisper/agentscope 数据文件）、`stage_python_runtime.py`/`stage_node_runtime.py`（独立运行时）、`build_macos_pyinstaller.sh`/`build_pyinstaller.ps1`、`sign_macos_bundle.sh`、`generate_update_manifest.py`、`sync_tauri_version.mjs`、`finalize_tauri_bootstrap.mjs`。另有非 Tauri 的 `scripts/pack/desktop.nsi` + `build_macos.sh`/`build_win.ps1`。发布链：`desktop-build.yml → desktop-release.yml → desktop-promote.yml/desktop-publish.yml`。
- **插件/独立包**：`plugins/{apps,bundle,tool,channel,memory,middleware-demo}`（如 `apps/qwenpaw-creator`、`bundle/chrome|cloudpaw|computer-use|qwenpaw-pet`、`tool/gpt-image2|qwen-image|wan27`），由 `scripts/pack/generate_plugin_metadata.py` + `merge_plugin_index.py` 生成索引并 `plugins-release.yml` 上传 OSS/CDN（Creator 走独立 `creator-release.yml`）。`packages/qwenpawmail-mcp` 是独立 hatchling 包 + 独立 `[project.scripts]`。

---

## 8. 外围模块（market / pawapp / services / tunnel / backup / portability / envs / local_models）

| 包 | 一句话职责 | 关键类/函数 | 对外出口 |
| --- | --- | --- | --- |
| `market/` | 技能市场**聚合搜索**（多 provider 并发分页） | `service.py:search_market/*`、`providers/{aliyun,clawhub,modelscope,qwenpaw}.py`、`providers/base.py:MarketProvider`（Protocol）、`categories.py:resolve` | `app/routers/market.py:79-89` → `GET /api/market/providers|categories`、`POST /api/market/search` |
| `hub/` | 多用户/多实例**控制面**（登录、runtime provision、反向代理） | `control_app.py:create_hub_app`、`service.py:RuntimeService`、`provisioner.py:RuntimeProvisioner`、`docker_provisioner.py`、`local_provisioner.py`、`database.py`（SQLite）、`websocket_proxy.py:relay_websocket` | `qwenpaw hub` → `/api/hub/**`（20+）、`/api/{path}` HTTP 代理、`/api/{path}` WS 代理 |
| `pawapp/` | **面向开发者的 App SDK**（把插件能力包成"小应用"） | `app.py:PawApp`（`@app.route/@app.tool/@app.command/@app.hook` + `include_router`）、`deps.py:get_ctx/get_scoped_ctx`、`dependency.py:DependencyRegistry/DependencySpec/DependencyHealth`、`agent.py:ManagedAgentProfile` | `app/routers/pawapps.py`（5 端点） |
| `services/` | 工作区与文件命名服务 | `workspace_manager/workspace_manager.py:WorkspaceManager`、`workspace_manager/sandbox.py:Sandbox`（`check_path/check_tool`）、`workspace_files.py`、`project_directory.py`、`fs_name_rules.py` | 被 `app/routers/workspace.py`、`project_directory.py` 调用 |
| `tunnel/` | 把本地端口暴露成公网 URL（本地模式的远程访问） | `cloudflare.py:CloudflareTunnelDriver`（`cloudflared tunnel --url`，正则抓 `*.trycloudflare.com`）、`binary_manager.py` | 需要外部 `cloudflared` 二进制 |
| `backup/` | 备份/恢复编排（单并发 + 进度订阅） | `manager.py:BackupManager`（`start_job/get_job/subscribe/reserve_restore`）、`models.py:BackupScope/BackupMeta/RestoreBackupRequest`、`_ops/{create,restore,storage}.py`、`_utils/safe_swap.py` | `app/routers/backup.py`（12 端点） |
| `portability/` | 从**其他 AI Agent 产品**迁移数据（Codex / Qoder） | `importer.py`、`planner.py`、`models.py:{MigrationPlan,ImportSelection,ImportAssetState}`、`providers/{codex,qoder}.py` + `codex_sessions/schedules`、`codex_plugin_adapter.py`、`qoder_plugin_adapter.py`、`transaction_journal.py`、`adaptation_*`（兼容性改写闭环） | `app/routers/portability_imports.py` |
| `envs/` | Console 可管理环境变量的**元数据与加密持久化** | `registry.py:EnvVarSpec(mutability=hot_runtime\|startup_only)`、`store.py`（`envs.json` + `os.environ` 双写、Fernet 加密） | `app/routers/envs.py`（6 端点） |
| `local_models/` | 本机 llama.cpp 模型运行时 | `manager.py:LocalModelManager`（单例）、`llamacpp.py:LlamaCppBackend`（下载/启停/进度队列）、`download_manager.py`、`tag_parser.py` | `app/routers/local_models.py`（14 端点） |

`tauri/` 已在上文（侧车入口 `entry.py`、`cli_entry.py`、`backend_guard.py:reconcile_singleton_backend`、`env.py`、`sidecar_logging.py`）说明，共 5 个文件 723 行，是**薄适配层**——真正的桌面复杂度在 Rust 侧 `console/src-tauri/`。

---

## 9. 关键文件索引（按阅读顺序）

```
docs/multi-user-server.md      多用户基线：形态、端口、启动顺序、验收门槛
docs/v1-api.md                 /v1 契约（字段/状态码/SSE）——与 server/api.py 逐条对应
docs/tdsql-storage.md          存储选择、隔离与锁语义、契约测试与未完成项
docs/streaming-tool-console.md 事件契约（tool/tool_output/approval/preview/terminal）+ 历史重建
src/qwenpaw/cli/serve_cmd.py   serve check|migrate|api|worker|controller|sandbox（5 进程入口）
src/qwenpaw/server/api.py      BFF：鉴权、/v1 路由、SSE、S3 上传
src/qwenpaw/server/contracts.py  Repository Protocol + ExecutionContext + 故障类型
src/qwenpaw/server/event_feed.py 每 run 一个轮询任务 + 256 条环形缓冲的扇形分发
src/qwenpaw/server/storage/    数据库/仓储/表结构（memory 与 TDSQL 共用状态机）
src/qwenpaw/server/{worker,controller,sandbox_app}.py  执行/沙箱编排/沙箱工具服务
src/qwenpaw/app/_app.py        个人模式应用装配（中间件、33 路由、agent 镜像、SPA 托管）
src/qwenpaw/app/routers/console.py   POST /api/console/chat（个人 SSE）+ stop/reconnect
src/qwenpaw/hub/control_app.py Hub 控制面全部端点 + HTTP/WS 反向代理
console/src/api/config.ts, vite.config.ts   前端 → 后端的唯一耦合点（同源 + dev 代理）
test-tools/native-console/src/service.ts    /v1 客户端（cursor + Last-Event-ID 重连）
deploy/Dockerfile, deploy/entrypoint.sh, deploy/server/kubernetes.yaml   三种部署产物
scripts/wheel_build.sh, scripts/docker_build.sh, scripts/pack-tauri/     三种打包链
.github/workflows/tests.yml    三级矩阵 + p0/p1/p2 强制标记 + 覆盖率合并
```

---

## 10. 清晰边界与明显问题

### 10.1 边界清晰之处（可直接借鉴）

1. **单一服务契约文件**：`server/contracts.py` 把 Repository 能力、`ExecutionContext`、`LeaseLost`、`ToolOutcomeUnknown` 集中；存储再单独有 `server/storage/CONTRACT.md`。API/Worker/MemoryService/Controller 都只依赖该协议，不碰连接池或 SQL（`docs/tdsql-storage.md`）。
2. **"浏览器永不持有服务令牌"**：`api.py` 文件头 + 双头校验 + Hub 在**代理层**注入 `X-QwenPaw-Runtime-Token`（`hub/control_app.py:1258-1300,1423-1470`）。
3. **Sandbox 是最小权限进程**：`serve sandbox` 只读 `QWENPAW_SANDBOX_TOKEN` + `QWENPAW_SANDBOX_ROOT`，工具白名单硬编码 `{shell,read_file,write_file,list_files,browser,mcp,publish_file}`（`server/sandbox_app.py:19-32`），不挂数据库/模型凭据；Worker 不执行用户命令。
4. **事件先持久化再投递**：`seq` 单调 + `Last-Event-ID` 重连 + `event: end` + keepalive；Redis 只是加速器，DB 轮询为权威。
5. **生成式文档 + 分层契约测试 + 强制测试分级**：`api-reference.md` 从真实 OpenAPI 生成；`tests/contract` 子类契约框架；CI 用脚本拒绝没有 p0/p1/p2 标记的 integration 测试。

### 10.2 明显问题（具体举例）

1. **整份重复实现留在包里**：`server/repository.py`（`PostgresRepository`，1332 行，`qp_server_*`）与 `server/storage/repository.py`（`SQLRepository`，1499 行，`qp_service_*`）并存；`serve` 只选后者（`cli/serve_cmd.py:88-91` + `storage/__init__.py`），前者仅被 `tests/unit/server/test_repository.py` 引用且默认 `skipif QWENPAW_TEST_DATABASE_URL`。`server/migrations/` 里 `002_pgvector.sql` 属旧线、`003/004` 同时含 Postgres 触发器函数，与 MySQL 线混放。
2. **巨型模块**：`app/routers/workspace.py` 2317 行、`agents.py` 2128、`skills.py` 2023、`config.py` 1433、`providers.py` 1222、`plugins.py` 1122；`src/qwenpaw/config/config.py` 3956 行一个文件承载全部配置模型；`agents/tools/deprecated_browser/browser_control.py` 5629 行（名字写着 deprecated 仍在包内并计入 package-data）。
3. **双前端 × 双聊天协议并行维护**：产品 `console/`（React，`/api/console/chat`，native `AgentRequest`）与 `test-tools/native-console/`（vanilla TS，`/v1`，`tool/tool_output/approval/preview_*/terminal`）各自实现 SSE 解析、重连、工具卡片、审批 UI；事件字段在两处手写。契约（`docs/streaming-tool-console.md`）只对后者生效——这是最大的漂移风险。
4. **文档与实现无机器校验**：`/v1` 关闭 OpenAPI（`docs_url=None`），个人后端默认也 404（`DOCS_ENABLED` 默认 False）；`api-reference.md` 的"386 顶层 / 231 镜像"只靠人工跑命令更新，仓库无生成脚本（`grep -r api-reference scripts/ .github/` 无命中），Hub 的 20+ `/api/hub/*` 端点完全不在任何契约文档里。
5. **单进程约束与扩展路径割裂**：`qwenpaw app` 强制 `workers=1`（明确弃用 `--workers`），要纵向扩展只能选 `/v1` 或 Hub；而 Hub 用 **SQLite**（`hub/database.py:connect_hub_database`，WAL, `busy_timeout=5000`）当控制面数据库，且文件头写着 schema 是"unpublished generation"，不兼容时要求"back up and recreate control.db"。
6. **目录遗留物混淆真实边界**：`src/qwenpaw/office/`、`cloud/`、`deploy/office-node/` 均为 0 个 git 跟踪文件（只有 `__pycache__`、`.pytest_cache`、`node_modules`，其中 `deploy/office-node/node_modules` 含 `docx`/`pptxgenjs` 却无任何代码引用）；阅读架构时容易被误导为"存在云/Office 服务"。
7. **桌面三条路线并存**：`qwenpaw desktop`（pywebview）、Tauri（自带 CPython + Node 运行时 + `backend_download.rs` + `updates.rs` + `tray.rs` + `webview_recovery.rs`）、`scripts/pack/desktop.nsi` 传统 NSIS，加 6 个 desktop/release workflow，维护面远大于收益。
8. **迁移顺序靠流程而非工具保证**：`deploy/server/kubernetes.yaml` 单文件同时声明迁移 Job 与三个 Deployment，`kubectl apply -f` 不保证迁移先行，必须由发布脚本先把工作负载保持为 0（`docs/multi-user-server.md` 明确警告）。这类"约定型正确性"在小团队里极容易破。

---

## 11. 一句话总结

QwenPaw 的服务端是一套**"个人单体 + 多用户四方拆进程 + Hub 多实例托管"三层叠加**的架构：核心抽象（`Repository` 协议、`ExecutionContext` 租约、事件持久化优先、Sandbox 最小权限）质量很高、且 `docs/v1-api.md` 与 `server/api.py` 基本逐条对齐；但周边沉淀了大量历史层（整套 Postgres 仓储、双前端双协议、三套桌面/安装链、目录级死代码），使得"读懂需要多久"与"实际运行需要多少代码"之间的差距非常大。对小项目而言，值得抄的是**协议与边界**，不值得抄的是**并行维护的路线数量**。

