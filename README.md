# GenSlide — 通用云端长程任务助手与创作平台

基于 AgentScope 构建的工业级云端长程任务助手与智能内容服务。
系统已完成核心架构升级，支持**自愈式模型网关、优先级提示词流水线（P100/P80/P60）、标准 8 阶段请求生命周期编排内核，以及带长程任务账本（GoalLedger）和防死循环门控（StopGates）的通用 ReAct 循环**。同时完整保留高效的结构化文档与幻灯片原生渲染能力。

## 从这里开始

- [通用云端长程任务助手架构总案与全阶段计划](docs/architecture/cloud-long-horizon-agent.md)：核心系统定位演进、五阶段执行计划、模块设计与实战指南。
- [当前架构与代码导航](docs/architecture/current-system.md)：模块职责、状态与边界。
- [本地启动与测试指南](docs/development/local-testing.md)：环境准备、离线测试、逐步启动、在线联调和排错。
- [后端配置与契约](backend/README.md)、[前端说明](frontend/README.md)、[脚本说明](scripts/README.md)。
- [文档索引](docs/README.md)：当前规范与历史记录分开查阅。

## 目录结构

```text
GenSlide/
├── backend/                    # 独立后端 Python 环境
│   ├── genslide_agentscope/     # 执行运行时、创作、模型/BFF 适配与内置 Skill
│   ├── tests/                  # 后端单元、契约与 HTTP 集成测试
│   ├── contracts/              # 后端 schema 副本，测试守护一致性
│   └── deploy/                 # Kubernetes 示例；Dockerfile 在 backend 根目录
├── frontend/
│   ├── assistant_demo.py       # 默认 Streamlit 开发演示入口
│   ├── assistant_client.py     # 用户 BFF transport 适配示例
│   ├── service_chat_client.py  # 本地 mock BFF/执行服务协调
│   ├── legacy/                # 旧工作台、本地 agent、专家团；非默认执行路径
│   └── tests/                 # 当前前端与旧工作台回归测试
├── test-tools/tl-proxy/        # 可选 Node.js TL 联调代理及其测试
├── scripts/
│   ├── dev/                   # 本地启动/停止脚本
│   └── testing/               # 在线冒烟、旧工作台质量评估
├── contracts/genslide-v1/     # 仓库级执行请求契约
├── docs/
│   ├── development/           # 本地操作指南
│   ├── architecture/          # 当前架构与未来 BFF/TDSQL 设计
│   ├── archive/               # 已取代的方案与历史实施记录
│   └── skills/                # TL 协议参考资料，不是后端自动加载的 Skill
└── pyproject.toml / uv.lock    # 前端与开发脚本环境
```

## 快速验证（无需模型密钥）

仓库根目录执行，推荐 Python 3.12：

```sh
uv sync --frozen --python 3.12
uv run --locked pytest -q
(cd backend && uv sync --frozen --python 3.12 && uv run --locked pytest -q)
```

根 pytest 仅收集 `frontend/tests`；后端使用独立锁定环境。
两套环境不要混装：现有 Streamlit 与 AgentScope 的 protobuf 依赖范围不兼容。

## 实现边界

开发链路：`assistant_demo → mock BFF + AgentScope API → TL proxy（可选）→ 模型`。
演示客户端协调 begin/execute；生产应由真实 BFF 鉴权和转发，不是浏览器持有服务令牌直连。

- 已实现执行上下文与每轮临时目录，当前稿从 BFF claim 的可信快照恢复。
- 后端不直接连接数据库或对象存储。TDSQL MariaDB 10.3 是生产 BFF 权威元数据的设计目标。
- 本仓库没有生产 BFF、Vue 应用、持久用户创作空间或已验证的 TDSQL 服务。
- mock BFF 重启丢失数据；旧工作台本地记忆和下载不是生产方案。
- 后端使用 POSIX 文件锁；Windows 请用 WSL2/Linux，不支持原生 Windows 完整运行。

目录调整保留 `genslide_agentscope` 包名、后端镜像构建上下文及 HTTP API。

## License

[MIT](LICENSE.md)
