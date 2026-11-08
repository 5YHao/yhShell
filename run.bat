@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo [yhShell] .venv not found. Run setup.ps1 first.
  pause
  exit /b 1
)
".venv\Scripts\pythonw.exe" app.py
