# 验收矩阵

以下是未来服务实现的验收要求，不表示运行时已实现。默认 npm test 使用 fake provider 或本地模拟 HTTP 上游，不访问真实模型；端口用 0，日志捕获和生成物隔离。

| 测试 | 输入/操作 | 必须观察到的结果 |
| --- | --- | --- |
| 技术栈与独立性 | 构建 TS 源码、仓库外安装 tgz、运行编译 CLI | Node.js 可独立启动，无原仓库源码、别名、UI/DOM 依赖 |
| 原生 init | system_prompt 和其他变量 | 原 TL 成功信封含 session_id；模型调用数为 0 |
| 变量校验 | 重复、非字符串、错别名、空 system | 400；不创建可用会话或触达模型 |
| legacy | 缺失变量、[]、name-only；LEGACY_ROLE_TEXT 开关 | 按开关处理；name 不改变模型 |
| 自定义变量 | 配置 sys_prompt | 仅该变量成为 system；非法配置失败 |
| 原样消息 | 中文、换行、system: 标记、引号 | native 路径恰好 system + user 且逐字符串相等，不做角色提升 |
| 任意正文 | 纯文本、Markdown、XML、JSON、未闭合 JSON、代码块、空白 | stream:true/false 均原样返回，不解析/修复/剥离内容格式 |
| 普通提示词 | 不含 JSON 的日常提示词 | 正常调用，不触发 JSON 关键词校验、不追加格式指令 |
| JSON 提示词 | 提示词明确要求 JSON | 上游仍不发送 response_format；仅原样传提示词，由模型遵循 |
| 工具描述正文 | 在 system/txt 写工具名称、tool_calls 字样、调用格式 | 作为普通字符串通过；代理不执行或校验其语义 |
| 原生工具入参 | 根级或 data 级携带不支持的工具控制字段 | 400；不发往模型；字段名仅出现在正文字符串时不能误拦 |
| 上游参数 | 捕获两个 adapter 的实际 HTTP 请求 | 无 response_format、tools、tool_choice、functions、function_call、parallel_tool_calls；模型/key/URL 固定 |
| 意外原生工具响应 | 非空 tool_calls/function_call，含/不含 content，流式增量 | 发头前 502，发头后 error；不转换、不忽略、不发成功 done；null/空数组占位不误拦 |
| 会话隔离 | 两个 init、多次/并发 chat、过期 session | 提示词不串、历史不累积、成功不删 session；未知/过期 404 |
| local 默认 | 无组织认证配置，本地客户端调用 | loopback 测试正常启动和运行；appId 可为空，不假称已鉴权 |
| 可选 bearer | 启用共享测试凭证，传错误/缺失 token | 401；不能通过伪造 appId 绕过认证 |
| 文件 | 缺失、[]、全空占位项、有真实引用 | 前三种兼容；真实附件明确 400，且不下载 |
| stream 类型 | true、false、缺省、字符串/数值/null | 前三种按协议；非法类型 400 |
| 非流式响应 | 返回任意字符串 content，包括空字符串 | 原 envelope 中 data.txt 为相同字符串；不返回 choices/tool_calls |
| SSE 编码 | 中文、emoji、换行、引号、空格增量 | TL chunk.content 拼接等于原文，成功仅一个 done/finished:true |
| 实时首增量 | 上游发一条 delta 后等门闩，客户端读首 TL chunk 后才释放尾帧 | 默认 debug 下也必须在释放门闩前收到 chunk；有界超时检测整包缓冲 |
| 增量解码 | UTF-8/CRLF/JSON 事件跨分包，一包多帧，注释/多行 data | 正确按事件增量解码，缓冲有界 |
| 控制帧 | role-only、reasoning-only、usage-only | 不进入正文；debug 记录原始上游事件 |
| 成功终止 | stop + [DONE]，包含非空/空白/空正文 | 正常 done；不以正文是否 JSON、是否调用工具决定成功 |
| 流中失败 | 仅 EOF、提前 [DONE]、length、畸形事件、中途 error、非空原生工具字段 | error 后关闭且无 done/重试；发头前错误使用非 2xx JSON |
| 协议不符 | stream:true 得到普通 JSON 或 provider 参数错误 | 不降级为回放、不改模型重试 |
| 字节计量 | 合法中文/反斜杠正文、很多短帧/空 control、单事件接近上限 | 正文 UTF-8、上下游 wire 和单事件分别限额；不把 framing 开销当正文，不允许无限无正文流 |
| 长思考 | 上游持续 reasoning 字节但暂不发 content | proxy 上游空闲计时不超时；客户端只受约定绝对deadline约束，不要求伪正文保活 |
| 请求边界 | 无效 JSON、null/数组外层、超限、非法 metadata | 明确 400/413，不因正文非 JSON 而误判 |
| 路由 | 查询字符串、未知路径、GET、OPTIONS | 正常路由、404/405、Allow 和配置的 CORS |
| 取消/超时 | 客户端断开、body 超时、drain 时断开 | abort 上游并清理，无挂起/重复结束/重试 |
| 容量 | session/请求并发超限、TTL | 有界资源和明确错误 |
| debug 默认 | 不设 LOG_LEVEL，运行 init/chat JSON/SSE | 捕获四个 direction 的 header/body/event；完整 system、txt、模型正文可见 |
| 凭证掩码 | 合成上游 key、测试 token、cookie 和普通正文标记 | 凭证不出现在日志；普通正文标记必须完整保留 |
| 日志时序 | 上游暂停生成，检查已产生的双向 SSE 日志 | 已到达/发出的事件即时可见，不等整流结束 |
| 日志背压/错误 | 慢 sink、关闭 sink，合法长正文 | 有界队列、保持顺序、不静默丢帧或截断；失败明确可见 |
| 日志关联 | 并发请求、错误响应、init 到 chat | requestId/sessionId/direction/sequence 可区分；provider 原始错误可在 debug 追踪，客户端仍得简洁错误 |
| 关闭 debug | 显式 LOG_LEVEL=info | 报文正文不再打印，协议与实时行为不改变 |
| 生命周期 | 端口 0、重复 close、启动失败、SIGTERM | 异步结果可见，排空请求和日志有期限，无残留句柄 |
| 共享入口（使用时） | 经 ingress 重做首增量测试 | 未被入口缓冲；不是本地测试前置 |

## QwenPaw 联调补充

- 采用 standalone skill 的 `examples/tool-roundtrip.json`：fake upstream 输出调用正文 → proxy 原样 chunk/done → standalone 生成本地 tool_call → 无副作用工具返回 → 新 init/chat 的 txt 携带匹配结果 → final 回答。抓取请求证明两个网络边界均未使用原生工具字段。
- 请求正文的 type=tool_calls 只是字符串内容，不应被字段防火墙误拦。proxy 不校验工具名或 arguments。
- malformed tool JSON 仍是合法模型正文；上游 stop+[DONE] 时 proxy 必须正常 done，由客户端决定语法纠错。
- standalone 开启临时预览时，fake upstream 在 final.content 中途暂停，验证已收到原 TL chunk、未收到 done；Console 可展示草稿而本地工具尚未提交。客户端 clear/update 事件不进入 TL 或上游报文，预览开关不改变 proxy 正文及分段转发规则。
- 两个模型实例分开路由，无需在 TL wire 加 model 字段；公司直连不依赖任何公网 key。

## 兼容范围

对比旧 TlProxyServer 与新门面的成功 TL 信封、重组后的正文和 done 语义；忽略随机 ID、分块边界。旧代理强制 JSON、缓冲回放和 tool_call 转正文不再是兼容目标。新差异单独记录：默认 debug、任意正文、原生工具字段拒绝、可选测试认证和错误映射等。

源码可用时，可用当前 TlAiClient 加无副作用 fake tool 测试其原有工具正文路径。这个客户端会整段读取并校验其工具内容，不能用它拒绝普通文本来断言代理错误；任意格式验收使用直接 HTTP/SSE 测试客户端。代理实时与 UI 逐字显示也是不同验收项。

## 建议验证流程

未来代理 package.json 应提供以下脚本；当前 skill 包没有运行时命令：

```sh
npm ci
npm run typecheck
npm test
npm run build
npm pack --dry-run
npm pack
```

再在原仓库外临时目录安装 tgz，使用本地 fake upstream 验证 CLI 默认 debug、任意正文与实时输出。真实账号测试使用单独 test:live，以合成提示词验证 DeepSeek/Qwen，单独报告通过项和待配置项。只把实际执行过的检查标为通过。
