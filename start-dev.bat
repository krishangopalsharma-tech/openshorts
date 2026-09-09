@echo off
setlocal
title OpenShorts dev launcher
set "ROOT=%~dp0"
cd /d "%ROOT%"

rem --- venv: prefer one next to this script, else the shared one in F:\openshorts
set "VENV=%ROOT%venv\Scripts"
if not exist "%VENV%\uvicorn.exe" set "VENV=F:\openshorts\venv\Scripts"
if not exist "%VENV%\uvicorn.exe" (
    echo [!] uvicorn not found. Create the venv first:
    echo     python -m venv venv ^&^& venv\Scripts\pip install -r requirements.txt
    pause & exit /b 1
)

rem --- .env: worktrees do not carry it (gitignored), borrow from main checkout
if not exist "%ROOT%.env" if exist "F:\openshorts\.env" copy "F:\openshorts\.env" "%ROOT%.env" >nul

rem --- dashboard deps
if not exist "%ROOT%dashboard\node_modules" (
    echo [*] Installing dashboard dependencies...
    pushd dashboard
    call npm install --no-audit --no-fund
    popd
)

set "PYTHONIOENCODING=utf-8"
set "VITE_PROXY_TARGET=http://127.0.0.1:8000"

echo [*] Starting backend on http://127.0.0.1:8000
start "OpenShorts backend" cmd /k ""%VENV%\uvicorn.exe" app:app --host 0.0.0.0 --port 8000"

echo [*] Starting dashboard on http://127.0.0.1:5173
start "OpenShorts frontend" /d "%ROOT%dashboard" cmd /k "npm run dev -- --port 5173 --host"

echo [*] Waiting for servers...
timeout /t 8 /nobreak >nul
start "" http://127.0.0.1:5173

echo.
echo Both servers run in their own windows. Close them (or run stop-dev.bat) to stop.
timeout /t 5 >nul
endlocal
