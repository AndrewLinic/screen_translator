@echo off
:: 屏幕翻译 - 离线翻译引擎一键安装
:: 1. 安装 argostranslate + 全部依赖
:: 2. 验证 Argos 模型在用户目录(已在 exe 首次启动时自动复制)
:: 3. 把 Python 路径写入 pyenv.json,让屏幕翻译.exe 知道用哪个 Python
::
:: 用法: 双击 -> 装好之后重启 exe 即可使用离线翻译

setlocal
cd /d "%~dp0"

echo ============================================================
echo   屏幕翻译 - 离线翻译引擎一键安装
echo ============================================================
echo.

:: 1. 检查 Python
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 python，请先安装 Python 3.10+ 并加入 PATH
    echo 下载: https://www.python.org/downloads/
    echo 安装时必须勾选 "Add Python to PATH"
    pause
    exit /b 1
)

echo [1/4] 检测 Python 版本...
for /f "delims=" %%v in ('python -c "import sys;print(sys.executable)"') do set "PYEXE=%%v"
echo   解释器: %PYEXE%
python --version
echo.

:: 2. 装 argostranslate + 全部依赖
echo [2/4] 安装 argostranslate 及其全部依赖...
echo   依赖 (pip 自动处理): ctranslate2, sentencepiece, protobuf, spacy, sacremoses, minisbd, stanza
echo.
echo   注: 首次安装需下载约 200MB 依赖包，请耐心等待
echo.

python -m pip install --user --upgrade argostranslate
if errorlevel 1 (
    echo.
    echo [警告] pip 安装失败。常见原因:
    echo   1) 网络问题 - 重试本脚本
    echo   2) 缺少 MSVC++ 运行库 - 到 https://aka.ms/vs/17/release/vc_redist.x64.exe 下载安装
    pause
    exit /b 1
)
echo.

:: 3. 验证模型
echo [3/4] 检查 Argos 模型文件...
set "PKG_DIR=%LOCALAPPDATA%\argos-translate\packages"
if not exist "%PKG_DIR%" set "PKG_DIR=%USERPROFILE%\.local\share\argos-translate\packages"

if exist "%PKG_DIR%\translate-zh_en-1_9\model\model.bin" (
    echo   已发现 bundled 模型: %PKG_DIR%\translate-zh_en-1_9
) else (
    echo   bundled 模型未找到,从网络下载...
    python "%~dp0download_assets.py" --install zhen enzh
    if errorlevel 1 (
        echo [警告] 模型下载失败,但请先重启 exe 重试
        pause
        exit /b 1
    )
)
echo.

:: 4. 把 Python 路径写入 pyenv.json (exe 用来定位 Python)
echo [4/4] 记录 Python 路径到 pyenv.json...
(
    echo {
    echo   "python": "%PYEXE:\=\\%",
    echo   "installed_at: "%DATE% %TIME%"
    echo }
) > "%~dp0pyenv.json"
echo   写入: %~dp0pyenv.json
echo.

echo ============================================================
echo   安装完成! 重启 屏幕翻译.exe 即可使用离线翻译
echo ============================================================
echo.
pause
