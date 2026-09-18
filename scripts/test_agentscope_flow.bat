@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo ==========================================================
echo   开始 AgentScope 端到端交互链路冒烟测试 (Windows)
echo ==========================================================
uv run python scripts\test_agentscope_flow.py
pause
