@echo off
chcp 65001 >nul
cd /d "%~dp0"
title MiyakoKB 启动器

set PYTHON_CMD=python
if exist "venv\Scripts\python.exe" (
    set PYTHON_CMD=venv\Scripts\python.exe
) else if exist ".venv\Scripts\python.exe" (
    set PYTHON_CMD=.venv\Scripts\python.exe
)

echo [MiyakoKB] 正在启动统一双进程守护模式 (API + Worker)...
%PYTHON_CMD% run.py %*
if %ERRORLEVEL% neq 0 (
    echo.
    echo [MiyakoKB] 服务非正常退出，退出码: %ERRORLEVEL%
    pause
)
