@echo off
REM Start the Flask backend + Next.js dashboard together, then open the browser.
REM Postgres/TimescaleDB is NOT started by this script - make sure it's running first.
setlocal
cd /d "%~dp0"

where node >nul 2>nul
if errorlevel 1 (
  echo Node.js was not found on PATH.
  echo Install the LTS build from https://nodejs.org then run this again.
  pause
  exit /b 1
)

set "PYTHON_EXE=python"
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"

where %PYTHON_EXE% >nul 2>nul
if errorlevel 1 (
  echo Python was not found on PATH ^(and no .venv\Scripts\python.exe exists^).
  echo Install Python 3.11+ from https://python.org, or create a venv with:
  echo   python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)

if not exist ".env" (
  echo.
  echo WARNING: .env not found. Copy .env.example to .env and fill in your own
  echo AngelOne credentials ^(never paste them into a chat session^) before
  echo expecting live data - the dashboard will still start in paper mode
  echo without it, just with no broker/data connection.
  echo.
)

if not exist "dashboard\node_modules" (
  echo dashboard\node_modules not found - installing frontend dependencies first...
  pushd dashboard
  call npm install
  popd
)

echo Starting the Flask backend on port 5050...
start "AI Trader - Backend" cmd /k "%PYTHON_EXE% backend\app.py"

echo Starting the Next.js dashboard on port 3000...
start "AI Trader - Dashboard" cmd /k "cd /d dashboard && npm run dev"

timeout /t 3 /nobreak >nul
start "" http://localhost:3000

echo.
echo Two windows opened - keep both running while the system is up.
echo Close them (or Ctrl+C in each) to stop.
echo.
pause
