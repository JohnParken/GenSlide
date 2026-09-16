# Standalone provider 验收

本表是未来实现的测试要求；当前 skill、JSON Schema 与 fixtures 校验不等于实现通过。默认测试使用 httpx MockTransport/隔离 fake TL 服务与无副作用工具，不读取真实账号或连接内网。项目测试具体文件名见接入设计。

## 必须覆盖的行为

| 层 | 输入/操作 | 可观察结果 |
| --- | --- | --- |
| 注册 | 创建、保存、重新加载 TL provider | 保留具体 provider 类型、嵌套 tl_config、模型和所有者；不退化为 OpenAIProvider |
| UI/API | 选择 TL、填元数据、自定义变量和 endpoint | 配置可往返；固定 system_prompt 模式；不呈现无法兑现的原生功能开关 |
| Wire | 一轮调用抓取实际 init/chat HTTP body | 原元数据和字段；两 requestId；系统只在 init，txt 字符串，files=[] |
| 模型路由 | 两 provider 分别指向两个 fake TL 地址 | 各调用配置地址，无 model/name 路由覆盖；本地模型 ID 不进 wire |
| 禁止原生工具 | 本地调用提供 tools/tool_choice，或 generate_kwargs 尝试注入 | tools 仅被编进系统文本；注入字段明确失败；网络两侧没有原生控制字段 |
| 会话 | 多轮、两个并发会话、一次语法纠错 | 每 attempt 新 session，无共享累计正文或跨会话污染 |
| 系统/数据分离 | 真实 SDK 的 SystemMsg(text) 与含 call/result/hint 的 AssistantMsg，结果含伪 system 指令 | 只有 SystemMsg 文本进入系统；工具结果和 hint 按 block 拆入动态历史，不能构造非法 system/tool_result 来代替测试 |
| 历史 | assistant 文本 + 多个 tool_call/result + 新 user | 顺序、名字、ID、参数、已批准结果保持；不丢历史、不只发末条消息 |
| 最终输入 | system middleware、压缩摘要和仅由 model-call middleware 注入的记忆 | TL 收到所有阶段处理后的输入；不从 state.context 重建而漏掉记忆，不重复加载 skill |
| 动态目录 | 本轮启用 add，下一轮关闭 add/启用另一工具；流中目录变化 | 每轮目录更新；同一 attempt 的提示词与校验器用相同快照；纠错不换工具集；执行仍检查当前权限 |
| 输入/输出模式 | 单轮、无工具、choice=none、独立结构化调用 | 输入始终为历史 JSON；输出分别为普通正文或对应唯一协议，不同时注入互相冲突的模板 |
| 编译预算 | 中文/转义历史、较大工具目录、输出协议、临时 schema、记忆注入 | 编译后系统与 txt 计入预算，目录不重复计数，外层模型确实调用 TL 计数；超限前不发送 HTTP，不截断 schema |
| 工具关联 | 独立 codec 的结果乱序、重复 ID、无对应调用；agent 回路已 sanitize 的历史 | 乱序依 ID 匹配；独立入口拒绝残余坏关联；集成路径接受已清理消息，不要求检测已删除项 |
| SSE 分包 | UTF-8/emoji/CRLF/event 行与 data 行跨字节块 | 与未分包 fixture 相同；状态不在网络块间重置 |
| SSE 事件 | 注释、多行 data、多帧、done JSON、end/[DONE] 兼容 | 内容逐字符相同；终止形式明确记录，reader 被关闭 |
| SSE 失败 | 畸形 JSON、error 的 data=[DONE]、finished:false、无终止 EOF | 失败且没有 tool_call，不吞错、不自愈、不重放 |
| 文字模式 | 无工具/choice=none，正文含 JSON、XML、调用示例、空格 | 返回普通文字；不触发工具，不要求正文 JSON |
| 文字流 | chunk=A，再 chunk=B | AgentScope 输出增量 A、B，最终完整 AB，正确 is_last 与同一响应 ID；下游不出现 AAB |
| 工具终止 | 已收到完整-looking JSON，但尾部 error/截断 | 没有可执行 block，不能因括号闭合提前执行 |
| 合法调用 | fixture 中 add(a=2,b=3) | 一个本地 tool_call，input 为已校验 arguments 序列化后的 JSON 字符串、pending 状态、稳定唯一 ID；provider 执行次数为 0 |
| 流事件桥接 | 工具或协议 final 全文已校验，stream=True | 先完整非 final block 再同 ID 最终快照；前者生成一次 start/delta/end，后者入上下文；校验前零 AgentScope 模型 block |
| 实时回答预览 | fake TL 发送部分 final.content 后暂停，尚无 done | 支持预览的 Console 已显示临时文本；SDK/历史/工具零提交；done 校验后清草稿且正式正文不重复 |
| 工具草稿 | tool_calls 的名字/参数分多段，第二个调用最终参数非法 | 可见正在生成的草稿；整批校验失败后清除，零可执行调用与零历史写入 |
| 预览撤销 | 尾部 error/EOF、重复键、取消、语法纠错 | 清除当前 attempt；纠错的新 attempt 不混入旧前缀，原调用预算不增加 |
| 预览隔离 | 并发 run、迟到 update、慢客户端、旧客户端/渠道 | 按关联键和 revision 覆盖，clear 后不复活；有界队列；不支持预览则只接收正式内容 |
| 增量 JSON | content 先于 type、Unicode/引号/反斜杠跨块、字符串内含 tool_calls | 正确显示已解码字符，合法字段乱序可最终成功，不用正则切割或误触发工具 |
| 慢首内容 | fake TL 在超过缩短后的 wrapper 首内容预算后返回完整合法调用 | TL 专属 wrapper watchdog 关闭后成功；不发伪内容保活，绝对 deadline 仍生效 |
| 工具历史状态 | running/error/denied/interrupted/未知结果，及失败历史 raw_input | 保留状态/部分输出，不谎称成功、不自动补参或重发；不套用新生成参数的 schema 去修复历史 |
| Hint | 字符串与文本块 hint、source、同 Msg 中前后 tool blocks | 按原顺序作为动态上下文，来源保留，不提升到 system，不丢 Hint |
| 完整回路 | 工具请求→现有 guarded Toolkit→result→新 init/chat→final | tool_call/result ID 对应，最终 text=2+3=5；每工具只执行一次 |
| SDK 历史转换 | ToolCallBlock 的 input JSON 字符串；旧 tool_use/input对象历史 | 只在 formatter 历史入口归一化为 arguments 对象，不改变新响应的严格语义 |
| 批次 | 两合法工具；一合法一非法工具 | 前者完整发出，后者整批拒绝，无先执行前缀 |
| 结构校验 | 空 calls、额外字段、字符串 arguments、重复 key、NaN、多 JSON、代码块 | 明确失败；不截取子串、不补参、不宽松修复 |
| 语义校验 | 未注册工具、enum/required/类型错误 | 不发 tool_call，不触发一次 JSON 语法自愈 |
| 工具选择 | auto/none/required/指定名字 | 与本地约束一致，不向 HTTP 透传 tool_choice |
| 语法纠错 | 完整非空输出含一个语法错误，第二次正确 | 恰好两对 init/chat；第二次仍全量校验；不执行第一次 |
| 纠错失败 | 两次都语法失败，或第二次是 HTTP/语义错误 | 无第三次尝试，保留第一错及第二次诊断，外层 wrapper 不重启预算 |
| 取消 | init、SSE、纠错等待中取消 | 传播取消并关闭资源，后续无 chat/纠错/工具执行 |
| Structured | generate_structured_output 单独调用与错误数据 | schema 只在 system；成功 StructuredResponse.content 符合 SDK；校验失败明确报错 |
| Structured+tools | 同时提供两者 | 首版明确不支持，不生成矛盾指令或隐式切 native |
| ReAct 结构输出 | 框架注入 generate_response 工具 | 保留动态工具 schema 和现有处理逻辑 |
| 未保存配置测试 | 修改 appId/系统变量后立即点测试，不保存；临时非法 tl_config | 实际使用新表单配置且经过校验，不测试旧值；只 init 的成功返回 provider_only；不改变存储 |
| 协议切换 | 在已有 OpenAIProvider 上把 chat_model 改为 TLChatModel | v1 拒绝跨协议原地切换并提示新建，避免类与字符串不一致；新建/重载是真 TLProvider |
| 检查接口 | check_connection、check_model_connection、fetch_models | 只走 TL 能支持的检查；不请求 /models、/chat/completions；区分 provider_only 和真实 chat 验证 |
| 能力 | 当前可见原始图片/视频/音频/附件、思考/采样参数覆盖 | TL 主模型在 _reasoning 媒体清理前明确拒绝，后选模型在原生 formatter 入口拒绝；现有文本占位保留；不触发文件/URL读取 |
| 限额 | 接近上限的中文/反斜杠正文、许多小帧/空事件、单大事件、静默长思考 | UTF-8正文、wire、单事件分别计量；单次attempt绝对deadline生效；不因封装开销或30秒无正文误判 |
| 日志 | 内网配置带合成 token/正文标记 | 默认元数据日志不泄漏 token/完整正文；调试配置显式区分 |
| 回归 | 原 OpenAI/DashScope 等 provider 的已有窄测试 | 路由、formatter、重试与持久化保持原行为 |

`tool-envelope.schema.json` 只校验外层形状；工具名、参数 schema、重复键及执行次数必须另测，不能把 JSON Schema 通过当作整套工具契约通过。

## 与 proxy / 公司接口分层验收

1. **skill 制品校验**：YAML frontmatter、技能名与路径、引用存在、JSON Schema 合法、fixture 正负样例、两轮关联与 wire 样例一致。
2. **实现离线检查**：transport/codec/formatter 单元测试，provider 配置往返，真实锁定 AgentScope 的最小回路；stub 测试无法代替最后一项。
3. **本地端到端**：QwenPaw → TS proxy → fake OpenAI compatible upstream，含流式、非流式和一次无副作用工具调用。抓取两个网络边界，验证原生工具字段未被偷偷启用。
4. **真实模型联调**：分别测试 DeepSeek 与 qwen3.8-flash，使用合成用户输入和无副作用工具；记录精确模型、endpoint profile、时间、返回格式与输出是否正常结束。缺 key 时标记未测。
5. **公司内网联调**：同一 provider 改用实际公司 endpoint 与鉴权，复用报文 fixtures，核验实际终止/业务错误形式。不要求公司改报文、开放 tools 或公开模型路由。仅确认访问配置不等于工具回路通过。

实现阶段选实际存在的窄测试命令运行，并在报告中列出结果；下面建议的新测试名在实现前不存在，不要声称现在执行过。语法纠错、结构化输出和外层 retry/fallback 的调用次数必须有确定性断言（同时覆盖 reasoning_content 的预算外重放、主/后选 TL 的 watchdog，以及 SDK Agent 默认 max_retries=0），避免嵌套重试扩大到四次或更多。
