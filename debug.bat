@echo off
setlocal
cd /d "%~dp0"
set YHSELL_DEBUG=1
if not exist ".venv\Scripts\python.exe" (
  echo [yhShell] .venv not found. Run setup.ps1 first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" app.py
pause
