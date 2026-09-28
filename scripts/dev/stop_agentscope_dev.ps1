# GenSlide AgentScope 本地全流程交互联调服务停止脚本 (PowerShell)
$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "正在停止本地联调服务 (端口 8010, 8002, 8501, 8089)..." -ForegroundColor Yellow

$ports = @(8010, 8002, 8501, 8089)
foreach ($port in $ports) {
    $connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($connections) {
        foreach ($conn in $connections) {
            $procId = $conn.OwningProcess
            try {
                $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
                if ($proc) {
                    Write-Host "正在停止端口 $port 进程 $($proc.ProcessName) (PID: $procId)..." -ForegroundColor Cyan
                    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                }
            } catch {
                # 忽略已退出的进程
            }
        }
    }
}

Write-Host "所有本地联调服务已停止。" -ForegroundColor Green
