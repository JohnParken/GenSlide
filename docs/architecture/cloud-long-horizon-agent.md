# 通用云端长程任务助手（Cloud Long-Horizon Agent Platform）设计与执行规划

> **文档定位**：本文档为 GenSlide 项目从“垂直单轮写作微服务”演进升级为“通用云端长程任务助手”的核心架构蓝图与开发指南。系统汲取了 QwenPaw 官方 `main` 分支在提示词工程、8 阶段生命周期、拓扑 Hook 编排、长程规划门控等方面的工业级成熟经验，同时继承了 GenSlide 既有的高并发租约安全、POSIX 文件锁、工作区配额隔离以及取消收尾屏蔽保护等微服务核心资产。

---

## 1. 演进背景与核心定位

### 1.1 演进背景
GenSlide 最初设计为一个面向多用户的专业写作与文档/幻灯片生成执行服务，采用单轮或固定步骤的有界决策。但在面对复杂的现实业务需求时，纯文本生成的局限性逐渐暴露：
- **目标漂移（Goal Drift）**：多轮长程交互后，模型极易遗忘用户最初的宏观目标，注意力发散。
- **解析脆弱性（502 报错）**：新一代推理模型（如 DeepSeek-R1、Qwen-QwQ 等）经常携带 `<think>` 思考链或微小格式瑕疵（单引号、尾随逗号、未闭合括号），导致基于硬编码正则与标准 `json.loads` 的网关频繁崩溃。
- **缺乏自主行动与长程规划能力**：面对“调研数据 $\to$ 整理报告 $\to$ 导出交付物”这类开放式复杂长程任务，缺乏自主拆解步骤、按序执行、循环反思以及安全停机判定的通用机制。

### 1.2 全新系统定位
**通用云端长程任务助手（Cloud Long-Horizon Agent Platform）**：
- 具备自主任务规划、多步目标跟踪与里程碑状态机的云端 Agent 平台；
- 原有的文档排版、PPTX 渲染、Markdown 解析等成熟能力作为原生技能（Skills）或工具（Tools）无缝接入；
- 面向高并发云端环境，提供无状态弹性伸缩、硬性租约隔离、自愈容错以及全生命周期的可观测性。

---

## 2. 全新系统架构全景

```text
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        通用云端长程任务助手 (Unified Cloud Agent)                        │
└────────────────────────────────────────────────────────────────────────────────────────┘
                                           │
  ┌────────────────────────────────────────▼────────────────────────────────────────┐
  │ 1. 外部控制面与调度层 (BFF & Lease Admission Layer)                               │
  │    - RESTful / SSE 流式接口 (支持高保真事件流与长时间心跳保活)                      │
  │    - 基于 POSIX 锁的并发租约控制、工作区磁盘配额隔离与定时垃圾回收                  │
  │    - 跨轮断点续跑管理 (Session Versioning & Snapshot State)                         │
  └────────────────────────────────────────┬────────────────────────────────────────┘
                                           │
  ┌────────────────────────────────────────▼────────────────────────────────────────┐
  │ 2. 标准 8 阶段请求生命周期内核 (8-Phase Runtime Engine)                           │
  │    PRE_DISPATCH ──► POST_DISPATCH ──► PRE_BUILD ──► POST_BUILD ──►                   │
  │    PRE_EXECUTE  ──► POST_RESPONSE ──► ON_ERROR  ──► FINALLY                      │
  │    (支持 Hook 拓扑编排、短路阻断 Short-Circuit、粘性跳过 SKIP_AGENT、取消收尾保护)   │
  └──────────────────┬──────────────────────────────────────────┬───────────────────┘
                     │                                          │
  ┌──────────────────▼──────────────────┐    ┌──────────────────▼──────────────────┐
  │ 3. 优先级提示词流水线               │    │ 4. 长程通用 Agent 核心引擎           │
  │    (Prompt Contributors Pipeline)   │    │    (ReActAgent & Planning Ledger)   │
  │   - P100: 安全防护与不可覆盖契约    │    │                                     │
  │   - P80 : 输出格式与结构约束契约    │    │   - 任务账本 (GoalLedger & TaskItem)│
  │   - P60 : 长程目标与 Todo 清单注入  │    │   - ReAct 循环 (思考-行动-观察-反思)│
  │   - P40 : 混合技能提示词 (Skills)   │    │   - 停机判定门控 (StopGates & Rubric│
  │   - P20 : 动态工作区事实与上下文    │    │   - 取消传播与优雅退出保障          │
  └──────────────────┬──────────────────┘    └──────────────────┬──────────────────┘
                     │                                          │
  ┌──────────────────▼──────────────────────────────────────────▼───────────────────┐
  │ 5. 工具调度协调器与混合技能系统 (Tool Coordinator & Hybrid Skills)               │
  │    - @tool_descriptor 工具声明与参数强校验                                      │
  │    - 四级超时防护体系 / 前后台双 Deadline (长任务自动转后台脱机运行)             │
  │    - 混合技能规范 (YAML Frontmatter 依赖声明 + Markdown 指令 + 附带脚本)         │
  │    - 应用级沙箱隔离 (限制工作目录、命令白名单、防止危险提权)                     │
  └────────────────────────────────────────┬────────────────────────────────────────┘
                                           │
  ┌────────────────────────────────────────▼────────────────────────────────────────┐
  │ 6. 自愈式模型网关与能力缓存 (Self-Healing Gateway & Capability Cache)            │
  │    - 思考链标签剥离 (<think> 识别与析出，避免污染业务结构)                       │
  │    - 四级自愈式 JSON 容错解析器 (彻底消灭模型格式微瑕疵导致的 502)               │
  │    - 模型特征与缺陷动态试错缓存 (自动降级不支持的参数与严格 Schema)              │
  └─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 已落地核心架构模块详解

### 3.1 自愈式模型网关（`backend/genslide_agentscope/gateway/`）

- **`sanitizer.py`（思考链剥离与文本清洗）**：
  - 自动识别并剥离成对或截断未闭合的 `<think>...</think>` 标签，将思考过程沉淀为 `SanitizedOutput.thought`，保持主业务输出纯净。
  - 自动剥离 Markdown 代码块标记（如 ````json ... ````），提取出核心有效负载。
- **`repair.py`（四级防御自愈式 JSON 解析器）**：
  - **Level 1**：快速直通解析（`json.loads`）。
  - **Level 2**：信封提取（剥离模型附带的闲聊开场白与结尾问候，提取最外层 `{ ... }` 或 `[ ... ]`）。
  - **Level 3**：启发式语法自愈（将 Python 风格 `True`/`False`/`None` 转换为 JSON 规范、清除尾随逗号、栈平衡算法自动补全因 Token 截断缺失的闭合括号）。
  - **Level 4**：极限依赖自愈（对接 `json_repair` 引擎处理复杂破损）。
- **`capability_cache.py`（模型能力试错缓存）**：
  - 动态记录特定模型的能力限制（如是否支持 `response_format`、是否抗拒 `strict_json_schema`、是否支持工具调用）。
  - 支持通过异常报错模式自动感知降级，避免同类报错再次发生。

### 3.2 优先级提示词流水线（`backend/genslide_agentscope/prompts/`）

将原本硬编码的大段提示词解耦为可插拔、可排序的 Pipeline：
- **`PromptContext`**：不可变上下文载体，包含 `goal`、`milestones`、`session_id`、`user_message` 等。
- **`PromptContributor` 协议**：定义 `name`、`priority (0~100)`、`contribute(ctx)`。
- **拓扑优先级规范**：
  - **P100 `ProtectedSecurityContributor`**：最高优先级安全契约，确立不可动摇的权限边界、防越狱/注入防护、不可信数据隔离。
  - **P80 `ExecutionContractContributor`**：结构契约，强约束 JSON 输出，支持绑定 Pydantic 模型动态渲染 JSON Schema。
  - **P60 `GoalLedgerContributor`**：长程任务目标与 Todo 清单动态注入器，渲染当前聚焦的任务步骤。
- **`PromptPipeline`**：按优先级自动降序拓扑排序，组装生成结构清晰的系统提示词。

### 3.3 标准 8 阶段生命周期内核（`backend/genslide_agentscope/runtime/`）

- **8 阶段枚举（`Phase`）**：
  1. `PRE_DISPATCH`：入参校验、租约认领。
  2. `POST_DISPATCH`：执行上下文装配与路由。
  3. `PRE_AGENT_BUILD`：工作区临时目录隔离与配额检查。
  4. `POST_AGENT_BUILD`：组装依赖、Hook 链与可用工具集。
  5. `PRE_EXECUTE`：驱动提示词流水线拼装、执行前检查。
  6. `POST_RESPONSE`：结果校验、任务账本同步、快照保存。
  7. `ON_ERROR`：全局异常归一化、超时降级与回滚。
  8. `FINALLY`：释放 POSIX 租约与准入槽位、清理临时工作区。
- **拓扑 Hook 机制（`hooks.py`）**：
  - 支持三态流控：`CONTINUE`、`SHORT_CIRCUIT`（阶段截断）、`SKIP_AGENT`（跳过模型直接处理）。
  - 基于 Kahn 算法构建有向无环图（DAG），严格满足 `before`/`after` 依赖时序，破平采用 `priority`，循环依赖自动抛出 `HookCycleError`。
- **生命周期协调器（`engine.py`）**：
  - `RuntimeEngine` 串联前置阶段 $\to$ 核心执行器 $\to$ 后置阶段。
  - **取消收尾保护（`_shielded_cleanup`）**：无论执行在何处被用户中断，`FINALLY` 阶段均在 `asyncio.shield` 保护下必然执行，确保锁与资源安全释放。

### 3.4 长程规划与通用 ReAct 循环（`backend/genslide_agentscope/planning/` & `runtime/react_agent.py`）

- **任务账本（`planning/ledger.py`）**：
  - `TaskStatus`：`PENDING`、`IN_PROGRESS`、`COMPLETED`、`BLOCKED`。
  - `GoalLedger`：维护宏观任务 `goal` 与子任务列表 `tasks`，提供 `start_task`、`complete_task`、`progress_ratio` 等原子操作，并能一键导出供提示词流水线渲染。
- **停机判定门控（`planning/gates.py`）**：
  - `MaxIterationsGate`：硬性迭代步数上限熔断（防止 Token 耗尽）。
  - `DoomLoopGate`：连续 3 次相同调用与失败特征检测，死循环自动阻断。
  - `CompletionRubricGate`：全量子任务完成判定，并带有 `allow_summary_turn` 宽限机制（允许 Agent 在完成任务后给出一次最终面向用户的总结答复）。
  - `CompositeGate`：组合多个门控，任一触发即可安全停机。
- **通用 ReAct 循环（`runtime/react_agent.py`）**：
  - 标准推进：`Thought` $\to$ `Action` $\to$ `Observation` $\to$ `Reflection`。
  - 动作分发：支持 `call_tool`（外部工具调用）、`update_task`（更新任务账本）、`final_reply`（输出最终结果）。
  - 与 `RuntimeEngine` 8 阶段无缝挂接作为标准执行器。

---

## 4. 全阶段执行路线图与状态矩阵

整个演进计划共分为 5 个清晰阶段：

| 阶段 | 核心目标 | 包含模块 | 当前状态 | 验收指标与测试情况 |
| :---: | :--- | :--- | :---: | :--- |
| **Phase 1** | **基座加固：自愈式模型网关与提示词流水线** | `gateway/`, `prompts/` | **已完成<br>(fca38f64)** | 1. 剥离 `<think>` 标签并提取思考链。<br>2. 畸变 JSON 自愈修复率达 100%。<br>3. 优先级提示词流水线稳定输出。<br>4. 新增 23 项测试，全量 158 项 100% 通过。 |
| **Phase 2** | **内核重塑：标准 8 阶段请求生命周期内核** | `runtime/phases.py`, `runtime/hooks.py`, `runtime/engine.py` | **已完成<br>(f441ea30)** | 1. 8 阶段生命周期确定性时序流转。<br>2. Hook DAG 拓扑排序与破平、成环报错。<br>3. 短路与取消防护安全执行。<br>4. 新增 8 项测试，全量 166 项 100% 通过。 |
| **Phase 3** | **长程编排：任务账本、停机门控与 ReAct 循环** | `planning/ledger.py`, `planning/gates.py`, `runtime/react_agent.py` | **已完成<br>(28c6291e)** | 1. 任务账本状态流转与进度计算。<br>2. 步数超限、死循环与全量达成门控生效。<br>3. ReActAgent 多步调用与挂接到 8 阶段引擎。<br>4. 新增 8 项测试，全量 174 项 100% 通过。 |
| **Phase 4** | **能力拓展：工具调度协调器与混合型技能生态** | `tools/coordinator.py`, `tools/sandbox.py`, `skills/hybrid/` | **进行中 / 待推进** | 1. `@tool_descriptor` 装饰器与参数校验。<br>2. 4 级超时防护与前后台双 Deadline 脱机任务。<br>3. 应用级受限沙箱与命令白名单过滤。<br>4. 混合技能加载（指令 + 脚本执行）。 |
| **Phase 5** | **服务治理：检查点快照与端到端流式可观测性** | `checkpoints/`, `observability/`, `api/sse.py` | **规划中** | 1. 任务里程碑自动打点创建 Checkpoint。<br>2. 模拟崩溃后基于检查点无损恢复长任务。<br>3. SSE 流式通道集成心跳（Heartbeat）防 504。<br>4. Token 消耗打点与全链路时延追踪。 |

---

## 5. 开发者使用与代码实战指南

### 5.1 示例 1：创建长程任务账本并由 ReActAgent 执行

```python
import asyncio
from genslide_agentscope.planning import GoalLedger
from genslide_agentscope.runtime import HookContext, ReActAgent
from genslide_agentscope.model import Model

async def run_mission():
    # 1. 初始化宏观任务账本与子任务清单
    ledger = GoalLedger(goal="调研新能源汽车最新财报并输出分析摘要")
    t1 = ledger.add_task("获取关键财务指标")
    t2 = ledger.add_task("进行同行横向对比")
    t3 = ledger.add_task("输出总结汇报")

    # 2. 定义可供调用的工具处理器
    async def custom_tool_handler(tool_name: str, params: dict):
        if tool_name == "fetch_metrics":
            return {"revenue": "300B", "growth": "25%"}
        return "Tool not found"

    # 3. 装配 ReActAgent
    agent = ReActAgent(
        model=Model(),
        tool_handler=custom_tool_handler,
    )

    ctx = HookContext(session_id="session_001")

    # 4. 执行多步长程循环
    result = await agent.execute_turn(ctx, ledger=ledger)

    print("执行状态:", result.status)
    print("总迭代步数:", result.iterations)
    print("最终答复:\n", result.final_reply)
    print("任务账本完成比例:", ledger.progress_ratio())

if __name__ == "__main__":
    asyncio.run(run_mission())
```

### 5.2 示例 2：扩展自定义的提示词贡献者（PromptContributor）

```python
from genslide_agentscope.prompts import PromptContext, PromptContributor, create_default_pipeline

class CustomCompanyStyleContributor(PromptContributor):
    name = "company_style"
    priority = 75  # 位于 P80 结构契约与 P60 目标账本之间

    def contribute(self, ctx: PromptContext) -> str | None:
        return (
            "## 企业风格准则\n"
            "- 回答必须保持客观、严谨，采用商业书面语。\n"
            "- 凡涉及金额的指标，必须带上货币单位。"
        )

# 注册到提示词流水线中
pipeline = create_default_pipeline()
pipeline.register(CustomCompanyStyleContributor())

# 构建系统提示词
ctx = PromptContext(goal="生成季度财务汇报")
system_prompt = pipeline.build_system_prompt(ctx)
```

### 5.3 示例 3：注册生命周期 Hook 拦截与审计

```python
from genslide_agentscope.runtime import HookBase, HookContext, HookResult, Phase, HookAction, RuntimeEngine

class AuditLoggingHook(HookBase):
    name = "audit_logging"
    phase = Phase.POST_RESPONSE
    priority = 50

    async def run(self, ctx: HookContext) -> HookResult:
        print(f"[审计日志] Action {ctx.action_id} 执行完成，阶段: {ctx.phase.value}")
        return HookResult(action=HookAction.CONTINUE)

class FastCacheHook(HookBase):
    name = "fast_cache"
    phase = Phase.PRE_DISPATCH
    priority = 90  # 高优先级优先检查缓存

    async def run(self, ctx: HookContext) -> HookResult:
        if getattr(ctx.request, "message", "") == "ping":
            # 命中极速响应，短路截断，跳过模型推理
            return HookResult(action=HookAction.SHORT_CIRCUIT, response={"reply": "pong"})
        return HookResult(action=HookAction.CONTINUE)

engine = RuntimeEngine()
engine.register_hook(AuditLoggingHook())
engine.register_hook(FastCacheHook())
```

---

## 6. 总结与后续推进建议

通过 Phase 1 $\sim$ Phase 3 的扎实建设，GenSlide 已成功构建起**“模型自愈网关 + 优先级 Prompt 流水线 + 标准 8 阶段生命周期 + 规划状态机与通用 ReAct 循环”**的工业级长程任务基座。

后续开发者在推进 **Phase 4（工具协调器与混合技能）** 与 **Phase 5（检查点与流式可观测性）** 时，应严格遵循本架构的分层原则，保持单元测试 100% 覆盖与 0 回归，将 GenSlide 打造为兼具专业交付品质与通用长程编排能力的云端智能体平台。
