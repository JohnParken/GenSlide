# 开发和测试脚本

| 路径 | 职责 |
| --- | --- |
| `dev/start_agentscope_dev.sh` | Linux/macOS 默认 TL 联调栈启动 |
| `dev/stop_agentscope_dev.sh` | 按默认端口停止联调进程 |
| `dev/*.ps1`、`dev/*.bat` | 保留的 Windows 辅助脚本；后端仍需 WSL2/Linux |
| `testing/test_agentscope_flow.py` | 真实服务/模型单回合出稿冒烟 |
| `testing/test_agentscope_flow.bat` | Windows 冒烟包装 |
| `testing/eval_chat_quality.py` | 旧工作台质量评估；支持 `--dry-run` |

文件原来都直接位于 scripts 根目录，现在按用途分类。Makefile 入口保持
`make dev-agentscope`、`make stop-agentscope`、`make test-flow`。

建议先按[逐步指南](../docs/development/local-testing.md)在独立终端启动。
一键脚本假设依赖已安装，固定端口并按端口复用已有服务；不能确认该进程属于本项目。
停止脚本按端口找进程，不验证所有权；有其他应用占用端口时不要执行，改在各终端 Ctrl+C。
在线冒烟和不带 `--dry-run` 的质量评估会调用真实模型并可能计费。
