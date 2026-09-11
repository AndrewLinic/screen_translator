# PyInstaller spec for 屏幕翻译
# 用法: pyinstaller screen_translator.spec
#
# 策略:
#   ✓ 打包: PyQt5 + winocr + ECDICT + win32com(TTS) + dict_lookup + 字幕/TTS/生词本
#   ✓ 打包: assets/ 目录（ECDICT 词典 + Argos 模型文件）
#   ✗ 不打包: argostranslate / ctranslate2 / sentencepiece / stanza
#                原因：sentencepiece 在 Windows 下 PyInstaller hook 缺失，C 扩展打包不稳
#                解决：随 exe 提供 install_offline_translate.bat 一键安装
#                       (用 pip --user，不污染系统 Python)
#
# 出包后流程:
#   1. 双击 "屏幕翻译.exe" — 已可用（OCR + 查词 + 模拟翻译 + 复制/历史/生词本）
#   2. 想用离线翻译 → 双击 "install_offline_translate.bat" → 自动装包+装模型 → 重启 exe

import os
import sys
from pathlib import Path

block_cipher = None

PROJECT_DIR = Path(SPECPATH).resolve()
print(f"[spec] PROJECT_DIR = {PROJECT_DIR}", file=sys.stderr)

a = Analysis(
    [str(PROJECT_DIR / "main.py")],
    pathex=[str(PROJECT_DIR)],
    binaries=[],
    datas=[
        # 只打词典进包；Argos 模型体积大（几百 MB），改为放在 exe 同级 assets/，
        # 由 build.bat 复制，程序首次运行再同步到英文路径下加载。
        (str(PROJECT_DIR / "assets" / "ecdict.csv"), "assets"),
        (str(PROJECT_DIR / "assets" / "ecdict.mini.csv"), "assets"),
    ],
    hiddenimports=[
        "winocr",
        # OCR 词库数据（词频表 + 专名表，由 build_lexicon.py 生成）。
        # ocr._load_lexicon() 里是函数内 import，显式声明以防漏收。
        "lexicon_data",
        # 解释器探测 + 模型自动下载：都是函数内 import，显式声明以防漏收
        "pylocator",
        "model_fetch",
        "PyQt5",
        "PyQt5.QtCore",
        "PyQt5.QtGui",
        "PyQt5.QtWidgets",
        # SQLite
        "sqlite3",
        # pywin32 TTS（注意：可能打包失败，下面已加 fallback）
        "win32com",
        "win32com.client",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "argostranslate", "ctranslate2", "stanza", "torch",
        "sentencepiece", "onnxruntime", "spacy", "thinc",
        "tkinter", "matplotlib", "pandas",
        "torchvision", "torchaudio", "IPython", "notebook",
        # cv2: winocr.py 里 recognize_cv2() 是函数内 import cv2 的可选分支
        # （我们只用 recognize_pil_sync）。一旦 venv 里装了 opencv
        # （RapidOCR 的依赖），PyInstaller 会顺着这个函数级 import 把
        # 整个 cv2 卷进 _internal，exe 体积 +113MB —— 必须显式排除。
        "cv2",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="屏幕翻译",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_DIR / "assets" / "app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="屏幕翻译",
)
