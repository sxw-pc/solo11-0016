@echo off
setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1
python -m pytest --cov=archive_extractor --cov-branch --cov-report=term-missing --cov-report=html --cov-fail-under=80
if errorlevel 1 exit /b %errorlevel%
endlocal
