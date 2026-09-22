# GenSlide AgentScope 创作助手 API

独立安装、独立部署，不 import 根目录工作台代码。
框架版本：`agentscope==2.0.7.post1`；推荐 Python 3.12（支持 3.11–3.12）。
无 MySQL/TDSQL、Redis、Streamlit 或对象存储连接要求。

## 已实现的入口

- `POST /v1/actions/{action_id}/execute`：一次请求完成操作；默认 JSON，`Accept: text/event-stream` 接收 SSE。
- 单一回合：`mode=assistant`、自然语言 `message`、`requested_output` 和可选 `requested_skill_id`，旧 operation 协议已移除。
- 三种目标：`writing` 返回完整正文，`document` 生成可编辑 DOCX，`presentation` 生成 PPTX（封面、正文页、结束页）。
- 结果为 `reply / outline / deliverable`；用户可直接出稿，不需要先列纲或确认。
- 每轮先给出结构化决策，再按固定版本的完整 Skill 创作。局部修改由代码合并，保留未修改章节。
- 只读取本轮授权 TXT/Markdown/PDF/DOCX 附件；快照仅从 BFF claim 恢复，包含有界当前稿，拒绝客户端正文或快照。
- 内置 5 个只读 Skill，支持完整正文和输出元数据；不执行脚本或访问任意本地目录。

## Workspace 边界与配置

本次实现不可变执行上下文和临时执行目录；用户持久创作空间、真实 TDSQL 数据库和生产
BFF 在后续接入。`mock_bff` 仅模拟可信快照和删除屏障，进程重启即丢失所有数据。

临时目录有 inputs/scratch/outputs 分区，持有跨进程锁，正常退出清理，周期回收失效残留。
使用 Linux/macOS；Windows 上在 Linux/WSL 中运行服务。配置：

| 环境变量 | 默认 |
| --- | --- |
| GENSLIDE_WORKSPACE_ROOT | 系统临时目录下的专属 genslide-workspaces |
| GENSLIDE_WORKSPACE_MAX_BYTES | 256 MiB，整个管理根目录 |
| GENSLIDE_WORKSPACE_MIN_FREE_BYTES | 128 MiB |
| GENSLIDE_WORKSPACE_STALE_SECONDS | 3600 秒；活跃锁持有期间不回收 |
| GENSLIDE_MAX_SNAPSHOT_BYTES | 2 MiB，正文与轻量记忆单独计量 |

工作目录用量每 0.5 秒检查，部署还须设置临时存储硬限额。API lifespan 每 60 秒以内扫描
残留目录。详情见 [三层工作区](../../docs/architecture/execution-workspaces.md)。

## 安装与启动

在本目录执行：

```sh
uv sync --frozen --python 3.12
uv run --locked pytest -q
```

生产环境必须配置：

| 环境变量 | 含义 |
| --- | --- |
| GENSLIDE_ENV | `production`；只有显式 `development` / `test` 才放宽开发限制 |
| GENSLIDE_SERVICE_TOKEN | 至少 32 字符，内部入站及 BFF 出站 Bearer 认证；通过 Secret 注入 |
| GENSLIDE_BFF_URL | BFF 内部基址，如 `http://bff/internal/genslide/v1` |
| MODEL_BASE_URL | OpenAI-compatible 的 `.../v1` 基址，或 TL 服务根地址 |
| MODEL_API_KEY | 模型服务凭据，不进入日志或请求响应 |
| MODEL_NAME | 目标模型名；TL 模式也需配置一个可追溯名称 |
| MODEL_PROVIDER | `tl`（默认）或 `openai`；模型 provider 主选择变量 |
| MODEL_PROTOCOL | `openai` 或 `tl`；兼容别名，与 `MODEL_PROVIDER` 同时设置时必须一致 |

```sh
uv run --locked uvicorn genslide_agentscope.api:create_app --factory \
  --host 0.0.0.0 --port 8000 --workers 1 --timeout-graceful-shutdown 45 --no-access-log
```

不得增加 ASGI workers；一期状态仅在单进程内。禁止把服务直接暴露到公网。
内部明文 HTTP 只适用于已隔离的可信网络；跨网络须 TLS/mTLS，由平台配置。
TL 模式通过 `/chatbbc/init_session`、`/chatbbc/chat` 两步协议调用，无轮询、无自动重试。
可用 `TL_APP_ID`、`TL_TR_CODE`、`TL_TR_VERSION`、`TL_SYSTEM_VARIABLE` 对齐现有网关。
本地可配合 [`services/tl-proxy`](../tl-proxy/README.md) 联调：

```dotenv
# 未设置 MODEL_PROVIDER/MODEL_PROTOCOL 时默认使用 TL
MODEL_PROVIDER=tl
MODEL_BASE_URL=http://127.0.0.1:8089
MODEL_API_KEY=local-proxy-key
MODEL_NAME=qwen3.8-flash
```

如需显式使用 OpenAI-compatible provider，设置 `MODEL_PROVIDER=openai`。

`MODEL_PROTOCOL=tl` 仅作为兼容别名；若与 `MODEL_PROVIDER` 同时设置，两者必须一致。
对 BFF 的 SSE 是业务阶段进度，不是模型原始 token/推理流。

## Skill 自动发现

启动时仅扫描包内 `genslide_agentscope/skills/<子目录>/SKILL.md`，
按 Markdown frontmatter 的 `name` 注册，不依赖硬编码清单，不递归加载辅助文件。
JSON skill 不再受支持，遗留 JSON 文件不会被注册；仅含 JSON 的目录会因无有效 skill 而启动失败。
也可设置 `GENSLIDE_SKILLS_DIR=/absolute/path/to/skills` 指向可信、只读的部署目录；
指定目录会**替代**包内目录，不自动合并或覆盖。管理员可通过受服务认证保护的
`POST /v1/skills/reload` 重载；当前回合使用开始时的注册表副本，不受重载影响。

未指定 ID 时，模型根据目录描述和本轮意图选择；同类编辑优先沿用当前稿 Skill。
显式 `requested_skill_id` 优先。继续使用同一 Skill 时检查版本/hash，缺失或变更返回
`SKILL_VERSION_UNAVAILABLE`；显式重新选择可使用新版本，不需要大纲确认。

无效 YAML、重复 name、未知字段、空指令、符号链接会阻止启动，不静默忽略。
目录至少 1 个、最多 128 个 skill；单文件最多 64 KiB。
正文作为完整创作指令，仅支持现有三类目标；不会加载 Python、scripts、
用户上传文件或任意工具。目录是服务管理员管理的可信配置，不接受请求指定加载路径。
多个 Pod 需要同一批 skill 时，各自部署相同目录副本即可，运行时之间无需互相 import。

### SKILL.md 格式（唯一支持格式）

内置示例：`genslide_agentscope/skills/business-report/SKILL.md`。
请求使用 `requested_skill_id="business-report", requested_output="text"`。

```markdown
---
name: business-report
description: 引导用户完善需求并生成商业报告。
metadata:
  version: "1"
  supported_outputs: "text,document,presentation"
  default_output: "document"
  priority: "100"
---

# 商业报告

根据用户意图讨论、规划或直接写作。仅在缺少关键信息时追问。
局部修改保留未要求修改的章节，不编造数据。
```

`name` 和非空 `description` 必填；`metadata` 可省略，值均须为字符串。
`metadata.version` 默认 `"1"`；`supported_outputs` 限定输出能力，`default_output` 必须在其中，
`priority` 为 -1000 到 1000 的整数文本。旧 `target_kind` 元数据仅用于兼容既有 Skill。
description 参与自动选择；完整正文非空，仍受整个文件 64 KiB 限制。
可选的 `license`、`compatibility`、`allowed-tools` 仅接收字符串元信息，
其中 allowed-tools **不授予任何工具执行能力**。YAML 安全加载，拒绝重复键、锚点、
别名和自定义标签；完整文件内容哈希用于绑定大纲。
`scripts/`、`references/`、`assets/` 不执行、不自动加载。
这是 SKILL.md 指令格式兼容，不是其他 Agent 产品完整执行环境的兼容。

## BFF 契约

精确请求 schema 见 `contracts/execute.schema.json` 与 `genslide_agentscope/domain.py`；线上 OpenAPI/Swagger 路由默认关闭。
内部 HTTP 契约及解析见 `bff.py`，可运行参考见 `mock_bff.py`。
BFF 必须先原子完成用户额度、会话版本、幂等及执行授权，再转发请求。
相同幂等操作固定同一 action_id，不得改换目标实例重试。

执行请求至少包含：
`api_contract_version="1"`、`engine="agentscope"`、
`tenant_id`、`user_id`、`session_id`、`runtime_epoch`、
`action_id`、`authorization`、`expected_session_version`、`expected_lifecycle_version`、
`mode="assistant"`、`requested_output`、`message`、`current_file_ids`。
用户偏好指定 Skill 可传 `requested_skill_id`。身份与 action 授权由 BFF 提供。
正文/快照只能来自 claim，不能由客户端直接回传。

BFF 内部路由（相对基址）：

| 路由 | 要求 |
| --- | --- |
| POST /actions/{id}/claim | 初始授权＋完整请求指纹，返回唯一实例令牌、版本、epoch、初始化标志与期限 |
| POST /actions/{id}/renew | 默认每 10 秒续租，不超过总截止时间；失败停止新计算 |
| PUT /actions/{id}/result | 原子保存正文/大纲结果、适用文件引用及回执，推进会话版本；无文件也提交 |
| GET /actions/{id} | 只读查询权威状态与结果 |
| POST /actions/{id}/settle | 核对已提交结果或关闭符合原因的操作，拒绝迟到提交；不重新生成 |
| POST /actions/{id}/files/{file_id}/download | 仅授权的本轮附件，BFF 返回字节与安全文件名，不返回任意远程地址给服务抓取 |
| POST /actions/{id}/files | multipart 上传，暂存成功不等于完整交接；还须 result 提交 |

文件路由是本实现提供的契约，需要与真实 BFF 对齐。BFF 可在内部使用长期 URL / 对象存储，
但本包只调用 BFF，避免接受不受控下载 URL。旧接口不同可替换本包的 BFF 适配器。
BFF 同步等待生成时仍须能处理续租、文件和提交回调。全局用户 AI 上限 2 由 BFF 保证。

SSE 包含 `accepted/progress/completed/error`、action_id 及 sequence。
只有 BFF 完整提交后发送 completed；断线后前端向 BFF 查原 action，不自动重发生成。
已初始化会话缺少可信快照返回 `CONTEXT_LOST`；未知快照版本返回 `SNAPSHOT_VERSION_UNSUPPORTED`，
不能静默忘记当前稿继续。有效 claim 快照允许跨 Pod 恢复。
取消与提交竞态以 BFF 原子结果为准，已提交不可回退为失败。

## 本地联调（仅模拟）

单独终端设置 `GENSLIDE_ENV=development`、`GENSLIDE_ALLOW_MOCK=1`、
`GENSLIDE_SERVICE_TOKEN`（与 API 一致），运行：

```sh
uv run --locked uvicorn genslide_agentscope.mock_bff:create_mock_bff --factory --host 127.0.0.1 --port 8010
```

模拟 BFF 的 `POST /dev/begin` 接收完整执行请求（authorization 可先填占位值），返回
`request`（含真实模拟授权）；将该 request 转发给 API。每轮先 begin，再 execute。
`POST /dev/files` 接收 multipart file、tenant_id、user_id、session_id，返回本轮 file_id。
所有请求均需同一服务 Bearer token。模拟数据仅在进程内，最多 1000 操作/100 文件；
它没有生产持久化、历史恢复或高可用能力，production 模式禁止启动。
`tests/test_e2e.py` 在进程内跑完整 BFF HTTP＋框架＋解析/渲染子进程，不调用真实模型。

## 资源与部署

- 每轮预留生成槽（默认每 Pod 两个）、两个 CPU 子进程和两个文件传输槽；满载快速拒绝，无后台任务队列。
- 每轮总上限 30 分钟；单次模型 HTTP 默认 120 秒；BFF 默认 10 秒，核对 5 秒。
- 运行时空闲有效期由 BFF 决定（4 小时）；本地过期条目在后续准入时清理。
- 默认最多 100 缓存会话、单会话轻量状态 32 KiB、正文快照 2 MiB；不限制会话只能执行 32 轮。
- 附件单文件 20 MiB、本轮下载合计 64 MiB，PDF 最多 200 页，DOCX 解压体积 50 MiB；解析文本总上限 10 万字符。模型输入另设 60,000 UTF-8 字节上限，超限明确拒绝，不静默截断。
- 解析/渲染在可终止子进程内进行；临时目录请求私有，请求结束清理。进程/Pod 被杀仍可能丢失内存草稿；BFF 已交接结果不受影响。
- 失败操作可能使候选检查点所在会话失效；BFF 应提示重建，而非自动恢复生成。
- `/healthz`、`/readyz` 为基础探针；`/internal/metrics` 需要服务认证，导出 Prometheus 格式的生成/规划并发、会话容量和占用指标，不带用户标签。网关仍需配置请求体限制、读取超时与 SSE 长连接策略。

`Dockerfile` 是独立构建上下文；默认 Python 3.12 slim，可通过 BASE_IMAGE 替换为含
`/usr/local/bin/python` 的企业批准镜像。openEuler 节点、CPU 架构、字体与目标 WPS
必须在实际环境验证，不能以本机测试替代。容器以 UID/GID 10001 运行，不需要运行时 root。
`deploy/kubernetes.yaml` 提供 headless Service＋StatefulSet 模板；默认 2 副本，无 Viewer。BFF 必须把运行时固定到具体 Pod DNS（如
`genslide-agentscope-0.genslide-agentscope.<namespace>.svc:8000`），不能轮询 headless 地址。
StatefulSet 固定名称不意味着内存持久化；该 Pod 重启后仍须重建运行时。
模板中的镜像、Secret、网络隔离及探针需按实际平台配置，尚未部署到真实集群。

## 上线前仍需完成

真实 BFF 原子幂等/租约/结果交接与文件接口联调；目标模型兼容性和内容质量；
WPS 打开/编辑/保存与字体/排版；openEuler 非 root 镜像运行、持续容量压测、
网关 SSE 缓冲/超时、亲和路由、故障切换、监控、灰度及回滚。
本机模拟测试通过不代表这些生产验收已经完成。
