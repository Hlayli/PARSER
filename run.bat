@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist data mkdir data

echo.
echo   PARSER  ^|  http://127.0.0.1:8777
echo   Остановить: Ctrl+C
echo.

rem Браузер открываем с задержкой, чтобы сервер успел подняться.
start "" /min cmd /c "timeout /t 2 /nobreak >nul & start "" http://127.0.0.1:8777"
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8777
