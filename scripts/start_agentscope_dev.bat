@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo =================================================================
echo   GenSlide AgentScope 本地全流程交互联调服务启动脚本 (Windows)
echo =================================================================

cd /d "%~dp0.."
set "REPO_ROOT=%CD%"

:: 1. 统一开发环境变量设置
set "GENSLIDE_ENV=development"
set "GENSLIDE_ALLOW_MOCK=1"
set "GENSLIDE_SERVICE_TOKEN=local-development-token-at-least-32-characters"
set "GENSLIDE_BFF_URL=http://127.0.0.1:8010/internal/genslide/v1"
set "MODEL_PROVIDER=tl"
set "MODEL_BASE_URL=http://127.0.0.1:8089"
set "MODEL_API_KEY=local-proxy-key"
set "MODEL_NAME=deepseek-flash"

if not exist "%REPO_ROOT%\.logs" mkdir "%REPO_ROOT%\.logs"

:: 2. 检查并启动 tl-proxy (端口 8089)
netstat -ano | findstr ":8089 " | findstr /i "LISTENING" >nul
if %errorlevel% equ 0 (
    echo [1/4] tl-proxy 已在端口 8089 运行中。
) else (
    echo [1/4] 正在启动 tl-proxy (端口 8089)...
    start "GenSlide - tl-proxy (8089)" /min cmd /c "cd /d "%REPO_ROOT%\services\tl-proxy" && npm run build && npm start"
    timeout /t 2 /nobreak >nul
)

:: 3. 检查并启动 mock_bff (端口 8010)
netstat -ano | findstr ":8010 " | findstr /i "LISTENING" >nul
if %errorlevel% equ 0 (
    echo [2/4] mock_bff 已在端口 8010 运行中。
) else (
    echo [2/4] 正在启动 mock_bff (端口 8010)...
    start "GenSlide - mock_bff (8010)" /min cmd /c "cd /d "%REPO_ROOT%\backend" && uv run --locked uvicorn genslide_agentscope.mock_bff:create_mock_bff --factory --host 127.0.0.1 --port 8010"
    timeout /t 2 /nobreak >nul
)

:: 4. 检查并启动 genslide-agentscope (端口 8002)
netstat -ano | findstr ":8002 " | findstr /i "LISTENING" >nul
if %errorlevel% equ 0 (
    echo [3/4] genslide-agentscope API 已在端口 8002 运行中。
) else (
    echo [3/4] 正在启动 genslide-agentscope (端口 8002)...
    start "GenSlide - AgentScope API (8002)" /min cmd /c "cd /d "%REPO_ROOT%\backend" && uv run --locked uvicorn genslide_agentscope.api:create_app --factory --host 127.0.0.1 --port 8002"
    timeout /t 2 /nobreak >nul
)

:: 5. 检查并启动 Streamlit UI (端口 8501)
netstat -ano | findstr ":8501 " | findstr /i "LISTENING" >nul
if %errorlevel% equ 0 (
    echo [4/4] Streamlit 前端已在端口 8501 运行中。
) else (
    echo [4/4] 正在启动 Streamlit 前端 (端口 8501)...
    start "GenSlide - Streamlit UI (8501)" /min cmd /c "cd /d "%REPO_ROOT%" && uv run streamlit run frontend\assistant_demo.py --server.port 8501"
    timeout /t 2 /nobreak >nul
)

echo =================================================================
echo   所有联调组件运行状态：
echo   1. TL 协议代理:     http://127.0.0.1:8089
echo   2. Mock BFF 网关:   http://127.0.0.1:8010
echo   3. AgentScope 服务: http://127.0.0.1:8002
echo   4. 交互测试页面 UI: http://127.0.0.1:8501
echo =================================================================
echo   请在浏览器打开: http://127.0.0.1:8501 开始测试！
echo   停止服务请运行: .\scripts\stop_agentscope_dev.bat
echo =================================================================
pause
