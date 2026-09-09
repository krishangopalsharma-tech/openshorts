@echo off
echo Stopping OpenShorts dev servers (ports 8000, 5173)...
for %%P in (8000 5173) do (
    for /f "tokens=5" %%I in ('netstat -ano ^| findstr /r ":%%P .*LISTENING"') do (
        echo   killing PID %%I on port %%P
        taskkill /f /pid %%I >nul 2>&1
    )
)
echo Done.
timeout /t 3 >nul
