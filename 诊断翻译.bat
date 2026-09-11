@echo off
chcp 65001 >nul
title 屏幕翻译 - 诊断工具
echo 正在运行翻译链路诊断，请稍候（约 10~60 秒）...
set "PY=C:\Users\29227\.workbuddy\binaries\python\envs\screentrans\Scripts\python.exe"
if exist "%PY%" (
    "%PY%" -X utf8 "%~dp0diagnose_translation.py"
) else (
    python -X utf8 "%~dp0diagnose_translation.py"
)
echo.
pause
