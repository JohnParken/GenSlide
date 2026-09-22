# GenSlide AgentScope 本地全流程交互联调服务启动脚本 (PowerShell)
$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RepoRoot = Resolve-Path "$PSScriptRoot\.."
Set-Location $RepoRoot

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "  GenSlide AgentScope 本地全流程交互联调服务启动脚本 (PowerShell)" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan

# 1. 统一开发环境变量设置
$env:GENSLIDE_ENV = "development"
$env:GENSLIDE_ALLOW_MOCK = "1"
$env:GENSLIDE_SERVICE_TOKEN = "local-development-token-at-least-32-characters"
$env:GENSLIDE_BFF_URL = "http://127.0.0.1:8010/internal/genslide/v1"
$env:MODEL_PROVIDER = "tl"
$env:MODEL_BASE_URL = "http://127.0.0.1:8089"
$env:MODEL_API_KEY = "local-proxy-key"
$env:MODEL_NAME = "deepseek-flash"

function Test-PortListening {
    param([int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $conn)
}

# 2. 检查并启动 tl-proxy (8089)
if (Test-PortListening 8089) {
    Write-Host "✔ [1/4] tl-proxy 已在端口 8089 运行中。" -ForegroundColor Green
} else {
    Write-Host "➔ [1/4] 正在启动 tl-proxy (端口 8089)..." -ForegroundColor Yellow
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c cd /d `"$RepoRoot\services\tl-proxy`" && npm run build && npm start" -WindowStyle Minimized
    Start-Sleep -Seconds 2
}

# 3. 检查并启动 mock_bff (8010)
if (Test-PortListening 8010) {
    Write-Host "✔ [2/4] mock_bff 已在端口 8010 运行中。" -ForegroundColor Green
} else {
    Write-Host "➔ [2/4] 正在启动 mock_bff (端口 8010)..." -ForegroundColor Yellow
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c cd /d `"$RepoRoot\backend`" && uv run --locked uvicorn genslide_agentscope.mock_bff:create_mock_bff --factory --host 127.0.0.1 --port 8010" -WindowStyle Minimized
    Start-Sleep -Seconds 2
}

# 4. 检查并启动 genslide-agentscope (8002)
if (Test-PortListening 8002) {
    Write-Host "✔ [3/4] genslide-agentscope API 已在端口 8002 运行中。" -ForegroundColor Green
} else {
    Write-Host "➔ [3/4] 正在启动 genslide-agentscope (端口 8002)..." -ForegroundColor Yellow
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c cd /d `"$RepoRoot\backend`" && uv run --locked uvicorn genslide_agentscope.api:create_app --factory --host 127.0.0.1 --port 8002" -WindowStyle Minimized
    Start-Sleep -Seconds 2
}

# 5. 检查并启动 Streamlit UI (8501)
if (Test-PortListening 8501) {
    Write-Host "✔ [4/4] Streamlit 前端已在端口 8501 运行中。" -ForegroundColor Green
} else {
    Write-Host "➔ [4/4] 正在启动 Streamlit 前端 (端口 8501)..." -ForegroundColor Yellow
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c cd /d `"$RepoRoot`" && uv run streamlit run frontend\assistant_demo.py --server.port 8501" -WindowStyle Minimized
    Start-Sleep -Seconds 2
}

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "  所有联调组件运行状态：" -ForegroundColor Cyan
Write-Host "  1. TL 协议代理:     http://127.0.0.1:8089"
Write-Host "  2. Mock BFF 网关:   http://127.0.0.1:8010"
Write-Host "  3. AgentScope 服务: http://127.0.0.1:8002"
Write-Host "  4. 交互测试页面 UI: http://127.0.0.1:8501"
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "  请在浏览器打开: http://127.0.0.1:8501 开始测试！" -ForegroundColor Green
Write-Host "  停止服务请运行: .\scripts\stop_agentscope_dev.ps1" -ForegroundColor Yellow
Write-Host "=================================================================" -ForegroundColor Cyan
