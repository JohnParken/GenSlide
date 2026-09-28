# 当前系统与代码导航

生产目标为用户 → 真实 BFF → AgentScope 执行服务。本仓库只有执行服务和开发 mock。
开发 UI 的 ChatClient 先向 mock `/dev/begin` 取得授权，再调用执行 API；不能把这种服务认证模式搬到生产浏览器。

执行过程：claim 可信上下文 → 固定 Skill → 决策/创作 → 按 effect 渲染 → 暂存上传 → BFF 原子提交结果和快照。
`reply / outline / deliverable` 是结果类别，不是用户必须依次通过的流程。

## 后端模块（backend/genslide_agentscope）

| 模块 | 职责 |
| --- | --- |
| `api.py`、`config.py` | 内部 API、认证入口、生命周期与配置 |
| `domain.py` | 严格请求/结果模型、可信快照 |
| `execution.py` | 准入、租约、版本、取消收尾、文件校验与提交 |
| `workspace.py` | 执行实例临时目录、容量、锁与残留回收 |
| `engine.py`、`workflow.py`、`authoring.py` | 回合决策、正文生成和局部修改 |
| `skills.py`、`skills/` | 可信 Skill 注册和版本/hash；不执行脚本 |
| `model.py`、`tl_provider.py`、`tl_transport.py` | 模型及 TL 协议适配 |
| `attachment_policy.py`、`content_io.py` | 统一附件策略、解析与渲染 |
| `bff.py` | 生产 BFF 内部 HTTP 契约适配 |
| `mock_bff.py` | 显式启用的内存开发模拟器 |

保留后端包路径和部署上下文；前端旧实现隔离到 `frontend/legacy`。
当前内置目录只有 document 和 official-document-skill。目标输出必须与加载 Skill 兼容，
测试中临时注册 writing/presentation 的样例不等于生产自带这些 Skill。

## 状态与边界

BFF 裁决身份、版本、生命周期、action、文件权限和提交；TDSQL MariaDB 10.3 是生产 BFF 的存储设计目标。
本地缓存不权威。当前稿正文允许进入有界快照；原始附件全文、工具输出和临时路径不能作为隐式跨轮状态。
执行上下文和临时目录已实现，用户持久创作空间待后续建设。

重复取消仍须等待写入与子进程终止，再清理目录和释放槽位。过期缓存删除不持有全局准入锁，
同会话在删除期间禁止重入。应用层磁盘轮询检查不是 OS 硬配额，见[工作区说明](execution-workspaces.md)。

## 测试与契约

前端测试放在 `frontend/tests/`，后端在 `backend/tests/`，代理在 `test-tools/tl-proxy/test/`，
分别使用根 uv、后端 uv、npm 环境。
仓库级 `contracts/genslide-v1/execute.schema.json` 与后端 `backend/contracts/execute.schema.json`
必须和领域模型生成结果一致，schema 测试守护副本一致性。
mock/fake 测试不等于真实模型评估，也不等于 TDSQL/生产 BFF 验收。
