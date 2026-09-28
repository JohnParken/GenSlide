# 前端与开发演示

默认入口 `assistant_demo.py`，在仓库根目录运行 `make run`。
`assistant_client.py` 是真实用户 BFF 的 transport 示例，`service_chat_client.py` 用于开发联调。
本目录不包含生产登录、持久创作空间或 Vue 客户端。

原 `frontend/service_chat.py`、`autonomous_agent.py`、`expert_council.py`、
`chat_context.py`、`chat_session.py` 已迁至 `frontend/legacy/`，导入改为
`frontend.legacy.<module>`。保留实验功能和回归测试，不在默认助手链路中。

旧 UI（仅开发）可从根目录执行：

```sh
uv run --locked streamlit run frontend/legacy/service_chat.py
```

原根目录 `tests/` 已迁至 `frontend/tests/`。从根目录运行
`uv run --locked pytest -q frontend/tests`，不要混用后端环境。
完整操作见[本地测试指南](../docs/development/local-testing.md)。
