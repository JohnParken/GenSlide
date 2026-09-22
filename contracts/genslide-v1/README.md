# GenSlide assistant 契约源

本次对 v1 做协调式破坏性升级：公开执行请求使用 `mode=assistant`、`message`、
`requested_output=auto|text|document|presentation` 和可选 `requested_skill_id`。
旧 `operation`、大纲确认参数和客户端正文/snapshot 不属于新执行请求。
停止新请求并排空旧 action 后，同时升级 BFF、服务和调用方。

## Workspace 边界

三层架构中，本次只实现执行上下文和临时执行目录。持久用户创作空间、生产 BFF、
TDSQL 表和清理 worker 仍是后续集成，不由模拟器代替。见
[执行工作区](../../docs/architecture/execution-workspaces.md)。

执行上下文绑定当前身份、action、材料清单、session/lifecycle 版本、租约、截止和 Skill
版本。模型与 Skill 无权扩大这些授权。每轮可以直接 `reply`、`outline` 或 `deliverable`，
无需完成固定业务阶段；所有回合按可能生成文件预留资源。

## Public BFF adapter

浏览器使用既有登录/CSRF 保护访问 `POST /api/v1/sessions/{sid}/actions`，
携带稳定 `Idempotency-Key`，仅提交本轮消息、期望输出、选定 Skill/附件和版本条件。
BFF 补齐内部身份、action、epoch 和初始授权，转发执行服务。
`frontend/assistant_client.py` 提供可注入登录 transport 的示例，不内置服务令牌。

内部 claim 必须返回并验证 `lifecycle_version`。快照只能来自 BFF claim，使用严格 schema；
result 提交时再次检查会话仍有效、生命周期/基础版本和租约。已删除会话的旧回执不能重放。
本地临时目录不能作为快照或跨实例恢复来源。

## Internal request fingerprint

`execute.schema.json` and the packaged schema must exactly match `ExecuteRequest.model_json_schema()`.
Claim binds a SHA-256 of the complete request with defaults populated, excluding `authorization`:
canonical JSON, UTF-8, sorted keys, `ensure_ascii=False`, separators `(',', ':')`.
Never hash raw HTTP bytes. Retried action IDs must bind the same identity, versions, Skill/output
intent, message and attachment list. A request ID is tracing metadata, not an idempotency key.

Times are timezone-aware ISO-8601 UTC. All turns, including replies without files, submit a result.
Result must atomically persist snapshot, optional content, staged file publication and receipt.
The execution service never connects to TDSQL or private storage directly; its HTTP adapter is
`backend/genslide_agentscope/bff.py`.

The development mock is not evidence that a production BFF satisfies transaction or authorization
requirements. Production persistence and per-user public API acceptance remain deferred.
