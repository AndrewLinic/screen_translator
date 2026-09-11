@echo off
REM 屏幕翻译 - 一键启动脚本
REM 首次使用前请先安装依赖: pip install -r requirements.txt

setlocal
cd /d "%~dp0"

REM 解释器自动探测（不再写死某台机器的绝对路径）：
REM   SCREENTRANS_PY 环境变量 -> 常见共享环境 -> 项目内 .venv -> PATH
set "PY=%SCREENTRANS_PY%"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\screentrans\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\screentrans\Scripts\python.exe"
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
    echo [错误] 未找到 Python。请先安装 Python 3.10+ 并加入 PATH。
    pause
    exit /b 1
)

REM 检查依赖（首次会安装）
"%PY%" -c "import PyQt5, mss, winocr, requests, PIL" 2>nul
if errorlevel 1 (
    echo [首次启动] 正在安装依赖...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo 依赖安装失败，请检查网络后重试
        pause
        exit /b 1
    )
)

REM 启动主程序
"%PY%" main.py
endlocal
