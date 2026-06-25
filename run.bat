@echo off
cd /d "%~dp0"
if not exist ".venv" (
  echo [setup] creating venv...
  python -m venv .venv
  if errorlevel 1 (
    echo Python not found. Please install Python 3.10+ from https://www.python.org/downloads/
    pause
    exit /b 1
  )
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
echo.
echo ============================================
echo  Warehouse Manager
echo  Open in browser:  http://127.0.0.1:8000
echo  Press Ctrl+C to stop
echo ============================================
echo.
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
pause
