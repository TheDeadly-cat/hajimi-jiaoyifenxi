@echo off
chcp 65001 >nul
setlocal

set "AI_STUDIO_PORT=8770"
set "AI_STUDIO_ROOT=%~dp0ai_collaboration_studio"
set "AI_STUDIO_URL=http://127.0.0.1:%AI_STUDIO_PORT%"

rem Reuse the formal local service when its health endpoint is already ready.
powershell.exe -NoProfile -Command "try { $health = Invoke-RestMethod -Uri ($env:AI_STUDIO_URL + '/api/health') -TimeoutSec 2; if ($health.ok -eq $true) { exit 0 } } catch {}; exit 1"
if not errorlevel 1 (
  start "" "%AI_STUDIO_URL%/"
  exit /b 0
)

if not exist "%AI_STUDIO_ROOT%\server.py" (
  echo [AI 共创室] 找不到服务入口：%AI_STUDIO_ROOT%\server.py
  exit /b 1
)

rem Start exactly from the project directory. The server's database owner lock
rem remains the final guard against a concurrent second instance.
powershell.exe -NoProfile -Command "Start-Process -FilePath 'python' -ArgumentList 'server.py' -WorkingDirectory $env:AI_STUDIO_ROOT -WindowStyle Hidden"
if errorlevel 1 (
  echo [AI 共创室] 服务启动失败，请确认 Python 已安装。
  exit /b 1
)

for /l %%I in (1,1,20) do (
  powershell.exe -NoProfile -Command "try { $health = Invoke-RestMethod -Uri ($env:AI_STUDIO_URL + '/api/health') -TimeoutSec 2; if ($health.ok -eq $true) { exit 0 } } catch {}; exit 1"
  if not errorlevel 1 (
    start "" "%AI_STUDIO_URL%/"
    exit /b 0
  )
  timeout /t 1 /nobreak >nul
)

echo [AI 共创室] 服务未在 20 秒内通过健康检查：%AI_STUDIO_URL%/api/health
exit /b 1
