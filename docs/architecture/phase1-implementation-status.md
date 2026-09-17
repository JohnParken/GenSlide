# 一期实现记录与上线前清单

日期：2026-09-16。状态：**核心功能代码与本地验证已完成，可开始 BFF 联调；尚未完成生产上线验收。**

## 交付入口

- [LangGraph 独立服务](../../services/genslide-langgraph/README.md)：固定 LangGraph 0.6.11。
- [AgentScope 独立服务](../../services/genslide-agentscope/README.md)：固定 AgentScope 2.0.7.post1。
- [TL 测试代理](../../services/tl-proxy/README.md)：chatbbc 两段式协议转换和公网模型联调。
- [BFF 契约源](../../contracts/genslide-v1/README.md) 与两包各自的请求 JSON Schema。
- [完整目标方案](synchronous-multiuser-content-platform.md)：P0–P8 是目标验收范围，不代表所有生产条件已在本机签收。

两包各有 pyproject、uv.lock、源码、内置 skill、测试、Dockerfile、Kubernetes 模板和说明；不依赖根目录旧模块或另一服务包。根目录旧 Streamlit/PPT 工程保留，不改作生产 API 入口。原有 TL 代理已作为独立服务迁移到 `services/tl-proxy`。

## 已实现内容

| 模块（每包独立副本） | 职责与边界 |
| --- | --- |
| api.py / domain.py | 严格输入 schema、内部服务认证、请求体上限、JSON/SSE、脱敏错误、健康探针及受保护的容量指标 |
| workflow.py | 澄清、建纲、修订、解释、确认、生成六个操作；逐步提问、建议选择、结构化需求更新；确认前不生成正文或文件 |
| skills.py / skills/*/SKILL.md | 写作、DOCX、PPT 三类只读指令，版本及哈希校验；无 scripts、动态工具或任意代码执行 |
| engine.py | LangGraph 原生图＋InMemorySaver；AgentScope 请求私有 AgentState＋白名单会话条目；BFF 提交后才发布本地状态 |
| model.py / tl_provider.py / tl_transport.py | 默认加载 TLProvider，`MODEL_PROVIDER=openai` 可显式切换 OpenAI-compatible；TL 使用 ChatBBC 两步协议；AgentScope 真正调用原生 Agent/模型适配器；有界输入输出、超时、取消与零自动重试 |
| bff.py | claim、renew、result、查询、settle、附件下载和产物上传；BFF 是唯一权威状态服务 |
| execution.py / config.py | Pod 准入、会话互斥、租约及截止时间、断连取消、提交竞态核对、请求私有目录、有界线程和可终止子进程 |
| content_io.py | 本轮 TXT/Markdown/PDF/DOCX 解析，DOCX 与带封面/内容页/备注/结束页的 PPTX 输出；大小、页数和解压体积保护 |
| mock_bff.py | 可独立启动的开发模拟服务，包含授权、幂等、文件和回执；显式开关启用，production 禁止启动，不提供持久化保证 |
| tests / contracts / CI | 模型模拟、真实框架调用、BFF HTTP 联调、故障与权限测试；两包 schema、skill、业务副本及公共直接依赖一致性检查 |

写作是一期必交付：引导 → 大纲 → 确认 → 完整正文 → BFF 无文件交接。纯写作不调用 PPT/DOCX 渲染；明确选择 document 才生成 DOCX。正文只保存到 BFF，不自动进入下一轮上下文。

## 已执行验证

本机 Python 3.12.12；未向真实模型发送数据，未使用真实 BFF 或真实用户数据。

- 两包完整 pytest 套件通过：LangGraph 45 项、AgentScope 46 项，覆盖引导写作、大纲版本与确认、未接受建议、材料隔离、三种产物和实际渲染子进程；长文 DOCX 不受 PPT 单页字符限制。
- 真实 LangGraph 检查点中未出现本轮材料、文件 ID 或请求授权；AgentScope 指定版本的原生 Agent 调用与取消通过模拟模型验证。
- 两个独立运行时向同一模拟 BFF 重复 claim，只有一个获得执行权；另一 Pod 缺失已初始化上下文时明确报错，不调用模型。
- 提交应答丢失不重发生成；提交和断连并发时保留权威成功。覆盖 claim 期间取消、SSE 断连、租约失败、容量拒绝与敏感字段脱敏。
- 真实测试子进程在取消/超时后被终止，容量释放；子进程不继承 BFF/模型凭据或旧根目录 PYTHONPATH。
- OpenAI-compatible 适配边界、TL 协议和 AgentScope 经 TL 调用的测试通过；这不是目标模型内容质量验收。
- 两包 sdist/wheel 构建通过；各自安装到仓库之外的全新环境，在不安装另一框架的情况下，API、状态、skill 资源及 DOCX 子进程生成冒烟通过；依赖检查无冲突。
- 根目录旧模型插件测试和 TL 代理集成测试共 10 项通过；未修改旧业务代码。
- `tools/check_service_parity.py` 检查副本一致性；CI 配置分别验证两个包，不用混合环境掩盖依赖问题。

LangGraph 依赖导入存在一条关于未来 allowed_objects 默认值变化的 PendingDeprecationWarning；当前测试正常，后续升级需回归序列化策略，不能直接更新锁版本。

## 当前实现的明确取舍

- 一期无 GenSlide 数据库或 Redis；BFF 仍须可靠持久化权威记录与文件，模拟 BFF 不可替代。
- BFF 绑定 engine 和具体 Pod；Pod 重启、错路由或不可确认的失败可能要求重建 runtime_epoch，没有自动恢复或自动切换引擎。
- 默认 100 个会话、32 KiB 单会话状态；每运行时最多 32 次已提交操作，限制原生检查点历史增长，超限明确重建。状态空闲有效期仍由 BFF 管理为 4 小时，过期物理清理在后续准入时执行。
- 附件按本轮清单下载；需要材料时重新上传。产生记忆的模型调用不接收附件正文；材料只进入本轮解释/正文生成，不从附件提炼跨轮需求。
- 模型规范化或推测出的需求不能直接当作已确认值：不属于用户原文/已有明确需求的值转为待确认建议。复杂自然语言歧义仍需要澄清；BFF 建议使用问题/建议 ID 和明确确认按钮。
- 流程由操作枚举和服务端校验控制，不是任意自主工具循环；模型仅产生当前阶段的有界结构化输出。完整正文当前为单次有界生成，不承诺无限篇幅或旧正文持续编辑。
- 解析没有 OCR；DOCX/PPTX 是可编辑结构化输出，不承诺任意原文件版式保持。超大材料/模型上下文明确拒绝，不截断后假装处理完整。
- SSE 提供业务阶段进度与心跳，不输出原始推理/token。HTTP 连接断开时 BFF 必须向下游传播取消，否则 GenSlide 只能看到 BFF 仍在线。
- 文件接口采用 BFF 字节代理；现有 BFF 若仅提供 file_id 下载 URL，应在 BFF 增加代理或评审适配，不能放开任意 URL 抓取。
- Kubernetes 使用 headless Service＋StatefulSet 模板帮助绑定 Pod DNS，不提供内存持久化。运行用户为 python（UID/GID 10001），需与平台策略对齐；模板尚未部署到真实集群。

## 上线前必须完成（尚未签收）

1. **BFF 联调**：确认 JSON 字段、完整请求指纹、用户额度、授权/令牌、版本、续租、原子 result/settle 和实际文件接口；验证 BFF 同步等待时回调不阻塞。BFF 负责未交接临时文件清理及回执留存。
2. **目标模型**：确认 URL/协议/凭据通过 Secret 配置；用合成样例验收 JSON 输出、逐步引导质量、完整写作、引用事实、篇幅及 PPT 内容密度，再接真实用户数据。
3. **文档视觉验收**：目标 WPS 的打开、编辑、保存，中文字体、分页、长标题和长段落。已完成结构/可编辑性测试，尚未做目标 WPS 视觉签收。
4. **openEuler 与镜像**：本环境无 Docker，未实际构建运行镜像；验证平台 CPU 架构、基础镜像、Python、非 root、只读根目录、临时卷及 Linux 资源限制。企业镜像需支持模板中的 Python 路径和构建期 useradd。
5. **网关与容量**：SSE 禁用缓冲、合理长连接超时、用户关闭页面传播取消、固定 Pod 路由、时钟同步、4 小时 TTL、至少一小时混合负载及模型 RPM/TPM。初始保持每 Pod 两个生成并发，不凭副本数承诺在线用户数。
6. **运维发布**：内部网络隔离/TLS、Secret 轮换、指标抓取和告警、日志保留/脱敏、灰度及回滚。健康/容量指标已提供，真实平台监控接入和演练未完成。

建议下一步先接真实 BFF 测试环境和合成模型样例；上述验收完成前不要将“本地测试通过”标记为 P8 生产上线完成。

## Main-agent work

2026-09-17 Markdown-only 调整：移除 JSON skill 加载，六份内置 JSON 文件迁移为
两包各自 writing/document/presentation/SKILL.md，原 ID 和阶段指引保留。
遗留 JSON 文件不注册，JSON-only 目录按空目录拒绝启动；业务 API JSON 协议不受影响。
主代理修改发现器、内置资源、README 和一致性检查，并验证 wheel 无 JSON skill、
四个 Markdown skill 可正常发现。子代理 `luna__md_only_tests`（Luna Medium）一次调用
负责两包测试迁移及 JSON 不再注册的回归用例。

2026-09-17 SKILL.md 支持：两包新增安全 YAML frontmatter＋Markdown 正文解析，
扫描直接子目录的 SKILL.md；后续按用户要求移除 JSON 兼容，重复 name 拒绝启动。
可选 metadata.version/target_kind；缺省类型可用于三类产物，服务端流程约束不变。
新增 business-report 示例、直接 PyYAML 依赖及锁记录；wheel 中的资源发现已验证。
不执行脚本或加载辅助资源；启动后文件变更须重启，仍接受一期内存丢失限制。
主代理完成解析器、依赖、示例、文档、打包检查及目标约束/文件符号链接补充测试；
`luna__skill_md_tests`（Luna Medium）一次调用负责两包 Markdown 加载与工作流测试。

2026-09-17 补充：两包 skill 改为启动时目录发现和按 ID 注册，支持
`GENSLIDE_SKILLS_DIR` 替代内置目录，无需代码清单。保留指令白名单、目标类型、
版本/哈希及大纲绑定；不执行脚本、不热加载。当前使用和 Markdown 示例见各包 README。
本次完整回归 LangGraph 56 项、AgentScope 57 项通过，副本一致性检查通过。
主代理实现发现器、草稿继承、动态资源一致性检查及文档；新增子代理
`luna__skill_discovery_tests`（Luna Medium）一次调用负责两包发现与流程测试，
主代理复核并补充删除后快照不变及重新加载测试。原生产验收待办保持不变。

确定两包契约、安全和状态边界；实现领域/引导/skill/框架与模型接入、模拟 BFF、依赖及部署；集成并修正取消竞态、SSE 结束通知、SDK TL formatter、内容边界及资源限制；补充端到端/独立安装/旧工程回归与交付说明。

## Subagent execution record

| 调用 | 子代理 | 范围与结果 |
| --- | --- | --- |
| 1 | explorer__sdk_contract / Luna Explorer | 只读核实固定版本 SDK、状态和取消 API；隔离安装成功 |
| 2 | luna__content_io / Luna Medium | 两包独立解析/渲染与测试 |
| 3 | worker__request_runtime / Luna Worker | 两包 API、BFF 客户端、配置、请求生命周期和故障测试；主代理复核集成 |
| 4 | luna__content_io / followup | 补充大小/解压/PDF 页数/Unicode 及 PPT 段落保护；两包测试通过 |
| 5 | luna__workflow_tests / Luna Medium | 引导流程和 SDK 冒烟/取消测试；通过 |
| 6 | explorer__sdk_contract / followup | 只读定位旧 TL 两步协议及 SDK 适配边界 |
| 7 | luna__tl_transport / Luna Medium | 两包独立异步 TL 传输与协议测试；通过 |
