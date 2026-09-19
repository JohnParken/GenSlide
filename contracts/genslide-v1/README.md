# GenSlide 一期契约源

`execute.schema.json` 是 `ExecuteRequest` 的 JSON Schema；两独立包保有副本，不在运行时依赖本目录。
schema 或语义变更须同步更新本契约与 `services/genslide-agentscope/contracts/execute.schema.json`，并运行该服务测试。

BFF 编排顺序：稳定幂等键 → 原子准入与授权 → 固定 engine/Pod → 同步 execute → 查权威回执。
claim 中的 `request_fingerprint` 为完整执行请求（按 Pydantic 默认值补全、排除 authorization）
的规范 JSON SHA-256：UTF-8、键排序、`ensure_ascii=False`、无额外分隔空白。
字段包含 message、草稿版本、本轮附件、问题答案、接受的建议等；客户端重试不得更改这些字段。
请使用各包 `domain.digest` 或完全一致的序列化规则，不对原始 HTTP 字节直接散列。

接口时间一律带时区的 ISO-8601 UTC。会话与执行状态以 BFF 为准；服务内存仅为可丢失草稿。
claim 必须绑定整个租户/用户/会话/epoch/action/engine、基础版本及执行实例。
result 必须原子提交结构化结果、适用文件引用、版本和回执；不能先返回成功再保存。
无文件写作/澄清/大纲/确认同样需要 result；并发取消与提交必须原子裁决。
settle 的调用权限和原因必须验证，普通 GET 不关闭操作，关闭后拒绝迟到提交。

内部路由、JSON 字段和联调模拟实现分别见每包 `bff.py`、`mock_bff.py` 及 README。
文件下载采用 BFF 字节代理；BFF 可以内部使用已有 file_id/长期 URL，不要求本服务直连存储。
这是一套待真实 BFF 联调确认的实现契约，模拟服务不能证明现有 BFF 已满足原子性要求。
