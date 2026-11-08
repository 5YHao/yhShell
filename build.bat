@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\build.ps1" %*
if errorlevel 1 (
  echo.
  echo Build failed. See the error above.
  exit /b 1
)
endlocal
