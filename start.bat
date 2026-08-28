@echo off
chcp 65001 >nul
setlocal

set PYTHON_EXE=C:\Users\10739\AppData\Local\Programs\Python\Python311\python.exe
set PROJECT_DIR=D:\lakejobai-job-radar-main
set BOSS_PORT=8010

cd /d %PROJECT_DIR% || (echo [ERROR] project dir missing & pause & exit /b 1)
if not exist %PYTHON_EXE% (echo [ERROR] python 3.11 missing & pause & exit /b 1)

echo ============================================
echo  lakejobai Web console (one script)
echo ============================================
echo.

echo [1/2] Checking dependencies...
%PYTHON_EXE% -c "import fastapi, uvicorn, yaml, bs4, lxml, websockets, playwright" 2>nul
if errorlevel 1 (
  echo [WARN] missing deps, installing...
  %PYTHON_EXE% -m pip install %PROJECT_DIR%\requirements.txt
  if errorlevel 1 (echo [ERROR] install failed & pause & exit /b 1)
)
echo       OK
echo.

echo [2/2] Starting Web console on port %BOSS_PORT%...
echo       (browser auto-launches from inside the console)
echo.

start "" http://127.0.0.1:%BOSS_PORT%/

%PYTHON_EXE% %PROJECT_DIR%\boss_app.py --port %BOSS_PORT% --auto-start

pause
