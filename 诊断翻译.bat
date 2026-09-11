@echo off
chcp 65001 >nul
title 屏幕翻译 - 诊断工具
echo 正在运行翻译链路诊断，请稍候（约 10~60 秒）...
REM 解释器自动探测，优先用装了本项目依赖的那个（不写死某台机器的路径）
set "PY=%SCREENTRANS_PY%"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\screentrans\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\screentrans\Scripts\python.exe"
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY set "PY=python"
"%PY%" -X utf8 "%~dp0diagnose_translation.py"
echo.
pause
