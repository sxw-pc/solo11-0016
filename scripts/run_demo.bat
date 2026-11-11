@echo off
setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1
python -m archive_extractor.cli demo
if errorlevel 1 exit /b %errorlevel%
endlocal
