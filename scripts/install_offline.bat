@echo off
setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1
python scripts\install_offline.py %*
if errorlevel 1 exit /b %errorlevel%
endlocal
