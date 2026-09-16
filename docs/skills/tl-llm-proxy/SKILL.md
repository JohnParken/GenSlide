---
name: tl-llm-proxy
description: 使用 TypeScript 和 Node.js 实现独立 TL 测试代理，连接 DeepSeek、qwen3.8-flash 并模拟公司 chatbbc init_session/chat 两段式接口；保持正文透明实时转发及默认四向 debug 报文，不使用原生工具调用。
---

# TL 两段式模型代理

本技能由原 `tl-proxy-standalone` 改名并明确职责：**proxy 对接外部模型，向 QwenPaw 暴露与公司接口相同的两段式协议**。这是独立 TypeScript/Node.js 服务的实现规格，不包含已实现或部署的服务器。

```text
QwenPaw TL standalone provider
          ↓ 固定 init_session → chat
TL proxy：保存系统变量，转换 JSON/SSE，实时转发
          ↓ /chat/completions，仅 system + user 正文
DeepSeek / qwen3.8-flash
```

QwenPaw 的 `tl-llm-standalone` skill 负责客户端/provider、system_prompt 工具契约及本地 AgentScope 适配。直连公司接口时它无需本 proxy。这里的“独立服务”不表示它承担 standalone 客户端的职责。

## 固定约束

- 公司报文格式和无法使用原生 tool_calls 是不可改变的事实。即使公网模型支持原生工具，也不得通过它让测试虚假通过。
- 保持 `POST /chatbbc/init_session`、`POST /chatbbc/chat`、元数据及原成功信封。init 绑定 `prompt_variables`；chat.txt 原样映射为本轮 user 正文。有系统变量时不解释 `system:` 等角色标记。
- `data.session_id`、字符串 `data.txt`、`event: chunk` 的 `{"content":"..."}`、`event: done` 的 `{"finished":true}` 保持不变。
- 不发送 response_format、tools、tool_choice、functions、function_call、parallel_tool_calls，不在 TL 外层增加 tool_calls/model/usage。原生工具控制字段拒绝；同名词出现在正文字符串中完全允许。
- 输出格式由客户端系统提示词决定。纯文本、Markdown、XML、JSON（包括不完整 JSON）都原样转发；不注入格式指令、解析/修复工具 JSON 或执行工具。
- 非空原生 tool_calls/function_call 响应或增量是协议不匹配，明确报错；不转换为正文、不静默忽略。
- stream:true（含缺省）调用上游流式输出，收到 delta.content 立即发 chunk；stream:false 返回完整 data.txt。失败不发 done，不重试已输出请求。
- 工具模式的逐字显示由 standalone 的临时预览层完成；不为 UI 修改模型正文、增加 TL 预览事件或提前发 done。proxy 只保证已有 content 尽快转发。
- LOG_LEVEL 默认 debug，打印四向实际请求/响应和逐事件正文；仅定向掩码凭证。该默认值是本地合成数据测试要求，不继承给内网客户端。
- 会话只绑定提示词、模型路由与生命周期，不自动累计历史。QwenPaw 历史已在 txt 的用户数据中；不拆解或提升其中角色。files:[]/空占位兼容，真实附件不下载、不假装支持。
- 每实例由服务端固定一个模型、endpoint 和 key。用不同端口测试 DeepSeek/Qwen；不能从 TL 的 name、txt 或新增 model 字段改变路由。

## 使用流程

1. 读 [报文契约](references/tl-contract.md)，区分固定成功格式与模拟器新增的校验/错误策略。
2. 按 [实现方案](references/implementation-plan.md) 构建 TS/Node 服务。默认 loopback，无需公司 SSO；共享测试可使用独立凭证。
3. 按 [模型接入](references/providers.md) 设置实际账号与精确模型。思考参数由 adapter 处理，不能跨模型盲目套用。
4. 用 [验收矩阵](references/acceptance.md) 检查透明转发、实时性、日志及原生工具隔离；再与 QwenPaw standalone 联调。
5. 只有需要迁移旧 PageAgent 代码时才读 [历史实现差异](references/current-state.md)。其中的旧源码观察不是当前 QwenPaw 状态。

## 交付

实际实现时交付独立源码、锁文件、build/start 入口、.env.example、测试、制品和启动说明。不得依赖 PageAgent 或 QwenPaw 私有源码路径、UI/DOM、工作区别名。代理测试可以独立使用 HTTP fake upstream，不要求先实现 standalone。

真实账号、endpoint 缺失不阻止离线实现；分别报告 skill 更新、服务实现、离线验收和真实联调状态。公司真实错误码、附件、认证或历史策略未确认时，不自行修改固定 wire 事实来适配。
