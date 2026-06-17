@echo off
REM === Set working directory to this file's folder ===
cd /d "%~dp0"

REM === Create venv if missing ===
if not exist ".venv\Scripts\python.exe" (
  py -m venv .venv
)

REM === Activate venv and install deps ===
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt

REM === Run the bot ===
python bot.py

REM === Keep window open on crash ===
echo.
echo Bot exited. Press any key to close...
pause >nul
