@echo off
chcp 65001 >nul
setlocal

set PYTHON_EXE=%~dp0.venv\Scripts\python.exe
set PROJECT_DIR=%~dp0
set BOSS_PORT=8010

cd /d "%PROJECT_DIR%" || (echo [ERROR] project dir missing & pause & exit /b 1)
if not exist "%PYTHON_EXE%" (
  echo [ERROR] .venv not found. Run: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
  pause & exit /b 1
)

echo ============================================
echo  AI_job_platform Web console
echo ============================================
echo.

echo [1/2] Checking dependencies...
"%PYTHON_EXE%" -c "import fastapi, uvicorn, bs4, lxml, websockets, playwright, sqlalchemy, langgraph" >nul 2>nul
if errorlevel 1 (
  echo [WARN] missing deps, installing into .venv...
  "%PYTHON_EXE%" -m pip install -r "%PROJECT_DIR%requirements.txt"
  if errorlevel 1 (echo [ERROR] install failed & pause & exit /b 1)
)
echo       OK
echo.

echo [2/2] Starting Web console on port %BOSS_PORT%...
echo       (browser auto-launches from inside the console)
echo.

start "" http://127.0.0.1:%BOSS_PORT%/

"%PYTHON_EXE%" "%PROJECT_DIR%boss_app.py" --port %BOSS_PORT% --auto-start

pause
