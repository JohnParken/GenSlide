# GenSlide AgentScope 一期 API

独立安装、独立部署，不 import 根目录旧工程或另一服务包。
框架版本：`agentscope==2.0.7.post1`；推荐 Python 3.12（支持 3.11–3.12）。
无 MySQL/TDSQL、Redis、Streamlit 或对象存储连接要求。

## 已实现的入口

- `POST /v1/actions/{action_id}/execute`：一次请求完成操作；默认 JSON，`Accept: text/event-stream` 接收 SSE。
- 六个操作：`clarify`、`create_outline`、`revise_outline`、`explain_outline`、`confirm_outline`、`generate`。
- 三种目标：`writing` 返回完整正文，`document` 生成可编辑 DOCX，`presentation` 生成 PPTX（封面、正文页、结束页）。
- 逐步提问、建议选择、自由输入、大纲修订及明确确认。确认不调用模型；生成必须是确认后的新 action。
- 仅本轮 TXT/Markdown/PDF/DOCX 附件；后续需要原材料须重新上传。本地跨轮只保存需求、引导状态和大纲，不保存正文或文件。
- 内置三类只读 skill，只装配阶段指令，无 scripts、动态工具或任意代码执行。

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
| MODEL_PROTOCOL | `openai`（默认）或 `tl` |

```sh
uv run --locked uvicorn genslide_agentscope.api:create_app --factory \
  --host 0.0.0.0 --port 8000 --workers 1 --timeout-graceful-shutdown 45 --no-access-log
```

不得增加 ASGI workers；一期状态仅在单进程内。禁止把服务直接暴露到公网。
内部明文 HTTP 只适用于已隔离的可信网络；跨网络须 TLS/mTLS，由平台配置。
TL 模式通过 `/chatbbc/init_session`、`/chatbbc/chat` 两步协议调用，无轮询、无自动重试。
可用 `TL_APP_ID`、`TL_TR_CODE`、`TL_TR_VERSION`、`TL_SYSTEM_VARIABLE` 对齐现有网关。
对 BFF 的 SSE 是业务阶段进度，不是模型原始 token/推理流。

## BFF 契约

精确请求 schema 见 `contracts/execute.schema.json` 与 `src/genslide_agentscope/domain.py`；线上 OpenAPI/Swagger 路由默认关闭。
内部 HTTP 契约及解析见 `bff.py`，可运行参考见 `mock_bff.py`。
BFF 必须先原子完成用户额度、会话版本、幂等及执行授权，再转发请求。
相同幂等操作固定同一 action_id，且不得跨 engine 重试。

执行请求至少包含：
`api_contract_version="1"`、`engine="agentscope"`、
`tenant_id`、`user_id`、`session_id`、`runtime_epoch`、
`action_id`、`authorization`、`expected_session_version`、
`operation`、`target_kind`、`message`、`current_file_ids`。
修订/解释/确认/生成另外带 `draft_id` 与 `expected_outline_version`。
确认/生成的 message 必须为空，修改意见先通过修订提交。
问题答案放 `answers`，接受建议放 `accepted_proposal_ids`；
结构化需求可通过 `requirement_updates` 更新，材料依赖通过 `requires_materials` 明确调整。

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
`CONTEXT_LOST/CONTEXT_STALE/CONTEXT_CAPACITY` 要求显式重建运行时，不静默忘记大纲继续。
取消与提交竞态以 BFF 原子结果为准，已提交不可回退为失败。

## 本地联调（仅模拟）

单独终端设置 `GENSLIDE_ENV=development`、`GENSLIDE_ALLOW_MOCK=1`、
`GENSLIDE_SERVICE_TOKEN`（与 API 一致），运行：

```sh
uv run --locked uvicorn genslide_agentscope.mock_bff:create_mock_bff --factory --host 127.0.0.1 --port 8001
```

模拟 BFF 的 `POST /dev/begin` 接收完整执行请求（authorization 可先填占位值），返回
`request`（含真实模拟授权）；将该 request 转发给 API。每轮先 begin，再 execute。
`POST /dev/files` 接收 multipart file、tenant_id、user_id、session_id，返回本轮 file_id。
所有请求均需同一服务 Bearer token。模拟数据仅在进程内，最多 1000 操作/100 文件；
它没有生产持久化、历史恢复或高可用能力，production 模式禁止启动。
`tests/test_e2e.py` 在进程内跑完整 BFF HTTP＋框架＋解析/渲染子进程，不调用真实模型。

## 资源与部署

- 默认每 Pod 两个生成槽、两个规划槽、两个 CPU 子进程和两个文件传输槽；满载快速拒绝，无后台任务队列。
- 生成总上限 30 分钟，规划 180 秒；单次模型 HTTP 默认 120 秒；BFF 默认 10 秒，核对 5 秒。
- 运行时空闲有效期由 BFF 决定（4 小时）；本地过期条目在后续准入时清理。
- 默认最多 100 会话、单会话 32 KiB 白名单状态；两版均最多 32 次已提交操作/运行时，超过后显式重建，以限制 LangGraph 检查点历史增长。
- 附件单文件 20 MiB、本轮下载合计 64 MiB，PDF 最多 200 页，DOCX 解压体积 50 MiB；解析文本总上限 10 万字符。模型输入另设 60,000 UTF-8 字节上限，超限明确拒绝，不静默截断。
- 解析/渲染在可终止子进程内进行；临时目录请求私有，请求结束清理。进程/Pod 被杀仍可能丢失内存草稿；BFF 已交接结果不受影响。
- 失败操作可能使候选检查点所在会话失效；BFF 应提示重建，而非自动恢复生成。
- `/healthz`、`/readyz` 为基础探针；`/internal/metrics` 需要服务认证，导出 Prometheus 格式的生成/规划并发、会话容量和占用指标，不带用户标签。网关仍需配置请求体限制、读取超时与 SSE 长连接策略。

`Dockerfile` 是独立构建上下文；默认 Python 3.12 slim，可通过 BASE_IMAGE 替换为含
`/usr/local/bin/python` 的企业批准镜像。openEuler 节点、CPU 架构、字体与目标 WPS
必须在实际环境验证，不能以本机测试替代。容器以 UID/GID 10001 运行，不需要运行时 root。
`deploy/kubernetes.yaml` 提供 headless Service＋StatefulSet 模板；两包各 2 副本，共 4 个 API Pod，
无 Viewer。BFF 必须把运行时固定到具体 Pod DNS（如
`genslide-agentscope-0.genslide-agentscope.<namespace>.svc:8000`），不能轮询 headless 地址。
StatefulSet 固定名称不意味着内存持久化；该 Pod 重启后仍须重建运行时。
模板中的镜像、Secret、网络隔离及探针需按实际平台配置，尚未部署到真实集群。

## 上线前仍需完成

真实 BFF 原子幂等/租约/结果交接与文件接口联调；目标模型兼容性和内容质量；
WPS 打开/编辑/保存与字体/排版；openEuler 非 root 镜像运行、持续容量压测、
网关 SSE 缓冲/超时、亲和路由、故障切换、监控、灰度及回滚。
本机模拟测试通过不代表这些生产验收已经完成。
