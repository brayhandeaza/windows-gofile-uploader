@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Upload-GoFile.ps1" %*
set "result=%errorlevel%"
echo.
pause
exit /b %result%
