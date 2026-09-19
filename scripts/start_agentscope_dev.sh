#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "================================================================="
echo "  GenSlide AgentScope 本地全流程交互联调服务启动脚本"
echo "================================================================="

# 1. 环境变量设置
export GENSLIDE_ENV=development
export GENSLIDE_ALLOW_MOCK=1
export GENSLIDE_SERVICE_TOKEN=local-development-token-at-least-32-characters
export GENSLIDE_BFF_URL=http://127.0.0.1:8010/internal/genslide/v1
export MODEL_PROVIDER=tl
export MODEL_BASE_URL=http://127.0.0.1:8089
export MODEL_API_KEY=local-proxy-key
export MODEL_NAME=deepseek-flash

LOG_DIR="$REPO_ROOT/.logs"
mkdir -p "$LOG_DIR"

# 检查端口辅助函数
is_port_in_use() {
  lsof -i ":$1" -sTCP:LISTEN >/dev/null 2>&1
}

# 2. 启动/复用 tl-proxy (8089)
if is_port_in_use 8089; then
  echo "✔ [1/4] tl-proxy 已在端口 8089 运行中。"
else
  echo "➔ [1/4] 正在启动 tl-proxy (端口 8089)..."
  (
    cd "$REPO_ROOT/services/tl-proxy"
    npm run build >/dev/null 2>&1 || true
    nohup node dist/src/cli.js > "$LOG_DIR/tl_proxy.log" 2>&1 &
  )
  sleep 2
  if is_port_in_use 8089; then
    echo "✔ [1/4] tl-proxy 启动成功！"
  else
    echo "✘ [1/4] tl-proxy 启动失败，请检查 $LOG_DIR/tl_proxy.log"
  fi
fi

# 3. 启动/复用 mock_bff (8010)
if is_port_in_use 8010; then
  echo "✔ [2/4] mock_bff 已在端口 8010 运行中。"
else
  echo "➔ [2/4] 正在启动 mock_bff (端口 8010)..."
  (
    cd "$REPO_ROOT/services/genslide-agentscope"
    nohup uv run --locked uvicorn genslide_agentscope.mock_bff:create_mock_bff \
      --factory --host 127.0.0.1 --port 8010 > "$LOG_DIR/mock_bff.log" 2>&1 &
  )
  sleep 2
  if is_port_in_use 8010; then
    echo "✔ [2/4] mock_bff 启动成功！"
  else
    echo "✘ [2/4] mock_bff 启动失败，请检查 $LOG_DIR/mock_bff.log"
  fi
fi

# 4. 启动/复用 genslide-agentscope (8002)
if is_port_in_use 8002; then
  echo "✔ [3/4] genslide-agentscope API 已在端口 8002 运行中。"
else
  echo "➔ [3/4] 正在启动 genslide-agentscope (端口 8002)..."
  (
    cd "$REPO_ROOT/services/genslide-agentscope"
    nohup uv run --locked uvicorn genslide_agentscope.api:create_app \
      --factory --host 127.0.0.1 --port 8002 > "$LOG_DIR/agentscope.log" 2>&1 &
  )
  sleep 2
  if is_port_in_use 8002; then
    echo "✔ [3/4] genslide-agentscope 启动成功！"
  else
    echo "✘ [3/4] genslide-agentscope 启动失败，请检查 $LOG_DIR/agentscope.log"
  fi
fi

# 5. 启动/复用 Streamlit UI (8501)
if is_port_in_use 8501; then
  echo "✔ [4/4] Streamlit 前端已在端口 8501 运行中。"
else
  echo "➔ [4/4] 正在启动 Streamlit 前端 (端口 8501)..."
  nohup uv run streamlit run frontend/service_chat.py \
    --server.port 8501 --server.headless true > "$LOG_DIR/streamlit.log" 2>&1 &
  sleep 2
  if is_port_in_use 8501; then
    echo "✔ [4/4] Streamlit 前端启动成功！"
  else
    echo "✘ [4/4] Streamlit 启动失败，请检查 $LOG_DIR/streamlit.log"
  fi
fi

echo "================================================================="
echo "  所有联调组件运行状态："
echo "  1. TL 协议代理:     http://127.0.0.1:8089"
echo "  2. Mock BFF 网关:   http://127.0.0.1:8010"
echo "  3. AgentScope 服务: http://127.0.0.1:8002"
echo "  4. 交互测试页面 UI: http://127.0.0.1:8501"
echo "================================================================="
echo "  请在浏览器打开: http://127.0.0.1:8501 开始测试！"
echo "  停止服务请运行: ./scripts/stop_agentscope_dev.sh"
echo "================================================================="
