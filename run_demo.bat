@echo off
chcp 65001 >nul
REM Демонстрационный запуск: источник "demo", данные помечены как ДЕМО.
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PARSER_DEMO=1

if not exist data mkdir data

echo.
echo   PARSER DEMO  ^|  http://127.0.0.1:8777
echo   Внимание: источник demo генерирует тестовые записи и помечает их "ДЕМО".
echo.

rem Браузер открываем с задержкой, чтобы сервер успел подняться.
start "" /min cmd /c "timeout /t 2 /nobreak >nul & start "" http://127.0.0.1:8777"
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8777
