# 本地启动与测试指南

本指南对应当前目录。推荐顺序：**安装依赖 → 离线回归 → 配置模型 → 分终端启动 → 健康检查 → 在线冒烟 → UI 验证 → 停止**。
所有示例使用合成材料，勿上传客户资料或生产凭据。

## 1. 环境准备

- Linux 或 macOS；Windows 请在 WSL2/Linux 中执行下列 Bash 命令。后端使用 `fcntl`，不能原生运行在 Windows。
- Python 3.12、uv；代理可选，需要 Node.js `^22.22.1` 或 `>=23.0.0` 和 npm。
- 首次安装依赖需要访问软件源；离线回归不需要模型 API key、数据库、Redis 或真实 BFF。
- 为临时卷预留空间，默认要求至少 128 MiB 可用；请留出更多余量供依赖和渲染使用。

打开终端并进入仓库根目录（替换路径）：

```sh
cd /path/to/GenSlide
pwd
uv --version
uv python install 3.12
uv sync --frozen --python 3.12
(cd backend && uv sync --frozen --python 3.12)
```

根 `.venv` 给前端和脚本使用；`backend/.venv` 给执行服务使用。不要混装，两者的 protobuf 依赖不同。
不需要先激活虚拟环境，后续 `uv run --locked` 会使用对应目录的环境。

## 2. 先运行不调用模型的回归

始终从仓库根目录执行：

```sh
uv run --locked pytest -q frontend/tests
(cd backend && uv run --locked pytest -q)
```

也可用 `make test`、`make test-service`。预期退出码为 0，输出 `passed`。
后端测试会使用 fake 模型、内存 mock BFF、真实解析/渲染子进程，不需要先启动任何服务。
涉及 writing/presentation 的能力测试可使用临时测试 Skill，不表示内置目录仍有这些同名 Skill。

重点检查：

```sh
(cd backend && uv run --locked pytest -q tests/test_cancellation_safety.py tests/test_execution_boundaries.py tests/test_workspace_limits.py tests/test_schema.py)
```

可选 TL 代理检查，同样无需真实模型密钥：

```sh
(cd test-tools/tl-proxy && npm ci && npm run typecheck && npm run build && npm test)
```

代理测试会监听本地随机端口、运行假上游。如果报 `listen EPERM`，说明当前沙箱/主机禁止本地监听；
换到允许监听的终端运行，不能把这类未执行成功的测试视为通过。

## 3. 为在线联调配置公共环境

下文每个新终端先进入同一个仓库根目录，并执行本节环境变量。Python 服务不会自动读取根 `.env`。
开发令牌仅限本机，不要复制到生产；三端令牌必须一致。

```sh
export GENSLIDE_ENV=development
export GENSLIDE_ALLOW_MOCK=1
export GENSLIDE_SERVICE_TOKEN=local-development-token-at-least-32-characters
export GENSLIDE_BFF_URL=http://127.0.0.1:8010/internal/genslide/v1
export GENSLIDE_DEMO_BFF_URL=http://127.0.0.1:8010
export GENSLIDE_SERVICE_URL=http://127.0.0.1:8002
unset GENSLIDE_SKILLS_DIR MODEL_PROTOCOL
```

首次验证使用默认工作区根目录即可；需要指定独立卷时再设置 `GENSLIDE_WORKSPACE_ROOT` 为绝对真实路径。
不要指向已有业务资料目录、符号链接目录或宽泛的 `/tmp` 根目录。

## 4. 选择模型接入方式（二选一）

从这里开始，真正的创作请求会连接模型上游并可能计费。本指南不要求运行历史质量评估脚本。

### A. 使用本地 TL 代理

终端 A，在仓库根目录执行：

```sh
export TL_PROXY_HOST=127.0.0.1
export TL_PROXY_PORT=8089
export UPSTREAM_PROVIDER=qwen
export UPSTREAM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
export UPSTREAM_MODEL='填写账户可用的模型名'
export UPSTREAM_API_KEY='填写真实上游密钥'
export AUTH_MODE=local
export LOG_LEVEL=info
cd test-tools/tl-proxy
npm start
```

第 2 节已执行 `npm ci`；`npm start` 会先构建。看到 `Listening on` 才算代理开始监听。
模型名请按账户实际配置，示例不是对模型可用性的保证。密钥勿提交到 Git 或共享终端历史。
默认 debug 日志可能记录提示词和正文，联调建议显式设为 info，仍只用合成材料。

后端所在终端再设置：

```sh
export MODEL_PROVIDER=tl
export MODEL_BASE_URL=http://127.0.0.1:8089
export MODEL_API_KEY=local-proxy-key
export MODEL_NAME='与代理上游一致的模型名'
```

这里 `MODEL_API_KEY` 是本机代理模式的占位值，真实密钥只放在代理的 `UPSTREAM_API_KEY`。
若代理使用 bearer 认证，则应改为代理 `TEST_ACCESS_TOKEN` 对应值。

### B. 直接使用 OpenAI-compatible 服务

跳过终端 A 和 Node.js，后端所在终端设置：

```sh
export MODEL_PROVIDER=openai
export MODEL_BASE_URL='https://你的模型服务/v1'
export MODEL_API_KEY='真实模型服务密钥'
export MODEL_NAME='账户可用的模型名'
```

使用供应商实际支持的基址，不要再套用 TL 根地址或本地占位密钥。

## 5. 分终端启动三个本地组件

每个新终端先从仓库根目录执行第 3 节；终端 C 还需配置第 4 节选择的模型变量。
下列命令保持前台，报错会直接显示；不要在同一个终端依次等待这三个常驻命令退出。

终端 B：模拟 BFF。

```sh
cd backend
uv run --locked uvicorn genslide_agentscope.mock_bff:create_mock_bff --factory --host 127.0.0.1 --port 8010
```

终端 C：AgentScope 执行服务。

```sh
cd backend
uv run --locked uvicorn genslide_agentscope.api:create_app --factory --host 127.0.0.1 --port 8002 --workers 1
```

终端 D：当前开发 UI（保持在仓库根目录，不进入 backend）。

```sh
uv run --locked streamlit run frontend/assistant_demo.py --server.address 127.0.0.1 --server.port 8501
```

打开 `http://127.0.0.1:8501`。这里不是 `frontend/legacy/service_chat.py`，旧工作台不作为本轮验收入口。

## 6. 健康检查与在线冒烟

终端 E，从仓库根目录先配置第 3 节：

```sh
curl --fail http://127.0.0.1:8002/healthz
curl --fail http://127.0.0.1:8002/readyz
curl --fail http://127.0.0.1:8002/v1/skills
```

预期分别返回 ok、ready 和 Skill 列表，当前默认 ID 是 `document`、`official-document-skill`。
`/readyz` 仅表示应用就绪，不测试真实 BFF/模型连通。代理和 mock 没有同名健康路由，
访问它们的 `/healthz` 得到 404/405 不说明启动失败。

接着运行实际模型冒烟（会生成 Word 文件）：

```sh
uv run --locked python scripts/testing/test_agentscope_flow.py
```

预期本轮 `effect=deliverable`，正文非空，有文件引用且文件可经 mock BFF 下载。
脚本输出成功后退出码为 0；每次重跑使用新会话/action，可能重新计费。

## 7. 在页面验证日常流程

1. 选择 `document`，指定 Skill 为 `document`，发送“只讨论一份项目总结应该包含哪些内容，先不要出稿”。应返回讨论，不生成文件。
2. 发送“用合成示例直接写一份项目总结并生成 Word，缺少信息用明确假设，不需要先确认大纲”。应显示正文及下载项。
3. 使用页面显示的准确章节名，发送“只改第二章对应的章节，其他内容保持不变”。检查未要求修改的章节。
4. 下载产物，用 Word/WPS 打开检查内容。浏览器点击下载成功不能替代文件实际可读性检查。
5. 请求失败时用“重试原请求”，不要手工变更 action 后把它当幂等重试。

刷新页面可能丢失页面会话，重启 mock 一定丢失模拟数据，当前未实现用户持久创作空间。
跨 Pod 快照恢复、删除屏障和取消竞争优先通过后端测试验证，不把 UI 刷新误当生产恢复验收。

附件测试需在同一 `ChatState.session` 下调用 `ChatClient.upload(..., session_id=state.session)`，
然后将返回的 file_id 传入 `turn(current_file_ids=[...])`；随便填其他会话的 file_id 不会授权访问。
当前简化 UI 只有 file_id 输入，不提供完整上传界面。下载/解析主链路已有 `test_execution_boundaries.py` 回归。

## 8. 停止与一键脚本

手动启动推荐在 A/B/C/D 各终端按 Ctrl+C，只停止自己启动的进程。
待流程熟悉后，TL 默认端口环境可从根目录用：

```sh
make dev-agentscope
make test-flow
make stop-agentscope
```

一键脚本位于 `scripts/dev/`，日志写入 `.logs/`；预先完成依赖安装与代理上游配置。
脚本使用固定端口和默认 TL 配置，会覆盖部分 MODEL 变量，不适用于直接接入模式。
它按端口复用服务，停止时也按端口找进程；有其他应用占用这些端口时不要使用启停脚本。
不要仅凭脚本末尾的地址清单认定所有组件健康，仍应执行第 6 节。

## 9. 常见问题

| 现象 | 检查方向 |
| --- | --- |
| `No module named genslide_agentscope` | 后端在 backend 环境运行；前端测试从仓库根目录运行，确认不是旧 tests 路径 |
| `fcntl` 导入失败 | 改用 Linux/macOS/WSL2，不是 pip 缺依赖 |
| protobuf 安装冲突 | 根环境与 backend 环境分开，不合并 requirements |
| mock 禁止启动 | 设置 development、ALLOW_MOCK=1 和一致的服务令牌 |
| 401/403 | 检查 BFF/API/UI 令牌一致、附件会话归属，不打印真实凭据 |
| `SKILL_NOT_FOUND` | 看 /v1/skills；旧 writing/business-report/presentation ID 已从内置目录移除 |
| `SKILL_TARGET_MISMATCH` | 指定 Skill 的 supported_outputs 与本轮目标不兼容 |
| 413 | 文件/累计文本/快照超限；不要只增加下载上限而忽略其他独立限制 |
| 507 | 工作区总量、单请求额度或剩余磁盘空间不足；详见后端 README |
| 地址占用或服务无响应 | 独立终端看错误；确认端口不是另一应用，勿直接杀陌生进程 |
| 模型超时或 502 | 核对 provider、基址、密钥、模型名和代理日志；健康接口不会验证上游 |
| `listen EPERM` | 运行环境禁止监听；代理网络测试需允许本地端口绑定 |

测试通过只说明对应代码路径已验证，不代表已部署生产 BFF/TDSQL、具备硬磁盘隔离或完成真实模型质量验收。

## 本次整理的验证记录（2026-09-24）

- 后端 135 项、前端 46 项、TL 代理 23 项测试通过；代理测试在允许回环监听的环境中完成。
- TL 代理 typecheck/build、Bash 脚本语法、Makefile 命令路径、Markdown 本地链接检查通过。
- 后端 sdist/wheel 离线构建、根 uv 锁文件离线校验通过。
- 未调用真实模型，未验证生产 BFF/TDSQL，未运行原生 Windows 脚本。
- 测试数量是本次记录，后续新增用例以实际输出为准。
