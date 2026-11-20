@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
title GiftOffer - two bots

where py >nul 2>nul
if not errorlevel 1 (
    set "PY_CMD=py -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 goto no_python
    set "PY_CMD=python"
)
%PY_CMD% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 goto no_python

if not exist ".env" (
    if not exist ".env.example" goto missing_template
    copy ".env.example" ".env" >nul
    if errorlevel 1 goto failed
    goto edit_settings
)
if /I "%~1"=="setup" goto edit_settings
goto install

:edit_settings
echo.
echo Edit settings in Notepad, save the file and close Notepad to continue.
echo Keep tokens and API hash secret. Do not share the .env file.
start "" /wait notepad.exe "%CD%\.env"
if errorlevel 1 goto failed
if /I "%~1"=="setup" (
    echo Settings saved. Run run_bot.bat to start both bots.
    pause
    exit /b 0
)

:install
if not exist ".venv\Scripts\python.exe" (
    echo Creating Python environment...
    %PY_CMD% -m venv .venv
    if errorlevel 1 goto failed
)
echo Checking and installing dependencies...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto failed

:validate
".venv\Scripts\python.exe" check_env.py
if errorlevel 1 (
    echo.
    echo Settings need attention. Press any key to edit .env in Notepad.
    pause >nul
    start "" /wait notepad.exe "%CD%\.env"
    if errorlevel 1 goto failed
    goto validate
)

echo.
echo Starting both bots. Keep this window open. Press Ctrl+C to stop.
".venv\Scripts\python.exe" bot.py
if errorlevel 1 goto failed
exit /b 0

:missing_template
echo Missing .env.example. Extract the complete archive and try again.
pause
exit /b 1

:no_python
echo Python 3.10 or newer is required: https://www.python.org/downloads/
echo During installation select Add Python to PATH.
pause
exit /b 1

:failed
echo.
echo Could not complete setup or start. Check the message above.
pause
exit /b 1
