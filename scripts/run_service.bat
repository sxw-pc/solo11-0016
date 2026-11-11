@echo off
setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1
set PORT=%~1
if "%PORT%"=="" set PORT=5001
python -m archive_extractor.cli service --port %PORT%
if errorlevel 1 exit /b %errorlevel%
endlocal
