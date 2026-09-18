@echo off
chcp 65001 >nul
echo 正在停止本地联调服务 (端口 8010, 8002, 8501, 8089)...

for %%p in (8010 8002 8501 8089) do (
    for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":%%p " ^| findstr /i "LISTENING"') do (
        echo 正在停止端口 %%p 对应进程 (PID: %%a)...
        taskkill /f /pid %%a >nul 2>&1
    )
)

echo 所有本地联调服务已停止。
pause
