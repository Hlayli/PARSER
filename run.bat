@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

rem Порт и адрес: переменные окружения → .env → значения по умолчанию.
if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do call :env_pair "%%A" "%%B"
)
if not defined PARSER_HOST set "PARSER_HOST=127.0.0.1"
if not defined PARSER_PORT set "PARSER_PORT=8777"

echo(%PARSER_PORT%| findstr /r /x "[0-9][0-9]*" >nul 2>nul
if errorlevel 1 (
    echo.
    echo Некорректный PARSER_PORT: "%PARSER_PORT%"
    echo Укажите число, например 8777, в .env или в переменной окружения.
    echo.
    pause
    exit /b 1
)

rem Интерпретатор: .venv → python → py -3. Неверный кандидат отбрасывается.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PY=.venv\Scripts\python.exe"
        goto :python_found
    )
)
python -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "PY=python"
    goto :python_found
)
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
    set "PY=py"
    set "PY_ARGS=-3"
    goto :python_found
)
goto :no_python

:python_found
"%PY%" %PY_ARGS% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto :old_python

"%PY%" %PY_ARGS% -c "import fastapi, uvicorn, httpx, bs4" >nul 2>nul
if errorlevel 1 (
    echo.
    echo Зависимости не найдены — устанавливаю: fastapi, uvicorn, httpx, beautifulsoup4
    "%PY%" %PY_ARGS% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Не удалось установить зависимости.
        echo Проверьте интернет и выполните вручную:
        echo   "%PY%" %PY_ARGS% -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
)

if not exist data mkdir data

set "BROWSER_HOST=%PARSER_HOST%"
if "%PARSER_HOST%"=="0.0.0.0" set "BROWSER_HOST=127.0.0.1"

echo.
echo   PARSER  ^|  http://%PARSER_HOST%:%PARSER_PORT%
echo   Остановить: Ctrl+C
echo.

rem Браузер открываем с задержкой, чтобы сервер успел подняться.
start "" /min cmd /c "timeout /t 2 /nobreak >nul & start "" http://%BROWSER_HOST%:%PARSER_PORT%"
"%PY%" %PY_ARGS% -m uvicorn backend.app:app --host "%PARSER_HOST%" --port "%PARSER_PORT%" || (echo. & echo Сервер завершился с ошибкой. & pause)
exit /b 0

:no_python
echo.
echo Python не найден.
echo Установите Python 3.10 или новее с https://www.python.org/downloads/
echo При установке отметьте галочку "Add python.exe to PATH",
echo затем закройте это окно и запустите run.bat снова.
echo.
pause
exit /b 1

:old_python
echo.
echo Найден Python старше 3.10: %PY% %PY_ARGS%
echo Установите Python 3.10 или новее с https://www.python.org/downloads/
echo.
pause
exit /b 1

:env_pair
rem Разобрать строку .env: export KEY=VALUE, кавычки и #-комментарий.
set "EKEY=%~1"
set "EVAL=%~2"
if /i "%EKEY:~0,7%"=="export " set "EKEY=%EKEY:~7%"
for /f "tokens=1 delims=#" %%C in ("%EVAL%") do set "EVAL=%%C"
rem Для порта и адреса пробелы не нужны — убираем хвостовые и лишние.
set "EVAL=%EVAL: =%"
if /i "%EKEY%"=="PARSER_PORT" if not defined PARSER_PORT set "PARSER_PORT=%EVAL%"
if /i "%EKEY%"=="PARSER_HOST" if not defined PARSER_HOST set "PARSER_HOST=%EVAL%"
exit /b 0
