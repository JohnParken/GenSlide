#!/usr/bin/env bash
set -euo pipefail

echo "正在停止本地联调服务 (端口 8010, 8002, 8501)..."

# 停止 mock_bff (8010)
PID_8010=$(lsof -ti :8010 || true)
if [ -n "$PID_8010" ]; then
  kill $PID_8010 2>/dev/null || true
  echo "✔ 已停止 mock_bff (端口 8010)"
fi

# 停止 genslide-agentscope (8002)
PID_8002=$(lsof -ti :8002 || true)
if [ -n "$PID_8002" ]; then
  kill $PID_8002 2>/dev/null || true
  echo "✔ 已停止 genslide-agentscope (端口 8002)"
fi

# 停止 streamlit (8501)
PID_8501=$(lsof -ti :8501 || true)
if [ -n "$PID_8501" ]; then
  kill $PID_8501 2>/dev/null || true
  echo "✔ 已停止 Streamlit (端口 8501)"
fi

# 可选：询问或提示 tl-proxy (8089)
PID_8089=$(lsof -ti :8089 || true)
if [ -n "$PID_8089" ]; then
  kill $PID_8089 2>/dev/null || true
  echo "✔ 已停止 tl-proxy (端口 8089)"
fi

echo "所有本地联调服务已停止。"
