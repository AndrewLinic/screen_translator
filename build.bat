@echo off
REM 屏幕翻译 - PyInstaller 打包脚本（全量重建）
REM 输出: dist\屏幕翻译\屏幕翻译.exe (单目录，免安装)
REM
REM 【推荐】日常迭代发布请用: python deploy.py
REM        它用独立 dist_tmp 构建 + 增量换入正式 dist，不会触发安全钩子，
REM        且 _internal 未变时只换 exe。本脚本适合首次出包/彻底重建。
REM
REM 打包策略:
REM   - PyQt5 + winocr + ECDICT + win32com(TTS) + sqlite3
REM   - 不打包 argostranslate/sentencepiece/ctranslate2 (C 扩展打包不稳)
REM   - Argos 模型作为数据文件打包(assets/argos_models/), exe首次启动自动复制

setlocal
cd /d "%~dp0"

REM 解释器自动探测（不再写死某台机器的绝对路径）：
REM   SCREENTRANS_PY 环境变量 -> 常见共享环境 -> 项目内 .venv -> PATH
set "PY=%SCREENTRANS_PY%"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\screentrans\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\screentrans\Scripts\python.exe"
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
    echo [错误] 未找到 Python。请设置环境变量 SCREENTRANS_PY 指向解释器，或把 python 加入 PATH。
    pause
    exit /b 1
)
echo 使用解释器: %PY%
echo.

REM 装 PyInstaller
"%PY%" -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo 安装 PyInstaller...
    "%PY%" -m pip install pyinstaller
)

REM 检查离线资源
if not exist "assets\ecdict.csv" (
    echo [警告] 词典 assets\ecdict.csv 缺失,打包后将无法查词
    echo        请运行: "%PY%" download_assets.py
)

REM 拷贝安装脚本到 dist
if not exist dist mkdir dist
if not exist "dist\屏幕翻译" mkdir "dist\屏幕翻译"
copy install_offline_translate.bat "dist\屏幕翻译\" >nul
copy download_assets.py "dist\屏幕翻译\" >nul

echo 开始打包...
"%PY%" -m PyInstaller --noconfirm --clean screen_translator.spec
if errorlevel 1 (
    echo 打包失败
    pause
    exit /b 1
)

REM 随 exe 附带的脚本与模型（模型放 exe 同级 assets，不打进 exe 内部）
copy offline_translate.py "dist\屏幕翻译\" >nul
copy pyenv.json "dist\屏幕翻译\" >nul
copy pylocator.py "dist\屏幕翻译\" >nul
copy model_fetch.py "dist\屏幕翻译\" >nul
copy install_models.py "dist\屏幕翻译\" >nul
copy diagnose_translation.py "dist\屏幕翻译\" >nul
REM RapidOCR 高精度识别引擎：由 pyenv.json 的 Python 以子进程方式启动，
REM 所以 ocr_worker.py 必须放在 exe 同级（onnxruntime/opencv 不进 exe）
copy ocr_worker.py "dist\屏幕翻译\" >nul
if not exist "dist\屏幕翻译\assets" mkdir "dist\屏幕翻译\assets"
echo 复制离线翻译模型（首次较慢，约 500MB，之后增量）...
REM /XO: 只复制比目标更新的文件（模型没变时几乎瞬间完成，不再遍历比对整棵树）
robocopy "assets\argos_models" "dist\屏幕翻译\assets\argos_models" /E /XO /NJH /NJS /NFL /NDL /NP
if errorlevel 8 (
    echo [警告] 模型复制可能有问题，请检查 dist\屏幕翻译\assets\argos_models
)

echo.
echo 打包完成！
echo 输出目录: %CD%\dist\屏幕翻译\
echo 主程序:   %CD%\dist\屏幕翻译\屏幕翻译.exe
echo.
echo 完整发布: 把 dist\屏幕翻译\ 整个目录发给用户即可
echo   - 双击屏幕翻译.exe 立即可用(OCR + 查词 + 字幕 + 历史 + 生词本)
echo   - 双击 install_offline_translate.bat 解锁离线翻译
endlocal
