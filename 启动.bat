@echo off
cd /d "%~dp0"
title MiyakoKB Launcher

set "PYTHON_CMD=python"
if exist "venv\Scripts\python.exe" (
    set "PYTHON_CMD=venv\Scripts\python.exe"
) else if exist ".venv\Scripts\python.exe" (
    set "PYTHON_CMD=.venv\Scripts\python.exe"
)

echo [MiyakoKB] Starting API + Worker...
%PYTHON_CMD% run.py %*
if %ERRORLEVEL% neq 0 (
    echo.
    echo [MiyakoKB] Service exited abnormally, code: %ERRORLEVEL%
    pause
)