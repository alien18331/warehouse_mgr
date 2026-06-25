@echo off
REM 倉管系統 — Windows 安裝腳本 (只安裝, 不啟動)
REM 啟動請用 run.bat
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python not found. Please install Python 3.10+ from https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist ".venv" (
  echo [setup] Creating virtualenv...
  python -m venv .venv
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip -q
pip install -q -r requirements.txt
echo.
echo [setup] Done. Run "run.bat" to start the server.
pause
