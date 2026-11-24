@echo off
setlocal
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 exit /b %errorlevel%
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b %errorlevel%
if not exist "apps\scraper\output" mkdir "apps\scraper\output"
if not exist ".env.local" copy /Y ".env.example" ".env.local" >nul
echo Setup complete. Configure .env.local and copy your WebTorrent.movie.json catalogue.
pause
