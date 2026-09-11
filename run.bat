@echo off
REM 屏幕翻译 - 一键启动脚本
REM 首次使用前请先安装依赖: pip install -r requirements.txt

setlocal
set PY="C:\Users\29227\.workbuddy\binaries\python\envs\screentrans\Scripts\python.exe"

cd /d "%~dp0"

REM 检查依赖（首次会安装）
%PY% -c "import PyQt5, mss, winocr, requests, PIL" 2>nul
if errorlevel 1 (
    echo [首次启动] 正在安装依赖...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo 依赖安装失败，请检查网络后重试
        pause
        exit /b 1
    )
)

REM 启动主程序
%PY% main.py
endlocal
