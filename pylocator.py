"""定位本机可用的 Python 解释器 —— 让脚本不依赖某台机器的绝对路径。

为什么要单独一个模块：
    程序里有三处需要"找到那个装了 argostranslate / rapidocr 的 Python"
    （离线翻译 translator.py、高精度 OCR ocr_rapid.py、构建与诊断脚本）。
    写死 ``C:\\Users\\<某人>\\.workbuddy\\...`` 会让别人克隆后一条命令都跑不起来，
    也把开发机的用户名暴露在公开仓库里。统一到这里按顺序探测。

探测顺序（先命中先返回）
    1. 环境变量 ``SCREENTRANS_PY`` / ``SCREEN_TRANSLATOR_PY``（显式覆盖）
    2. 同级 ``pyenv.json`` 的 ``python`` 字段（install_offline_translate.bat 写入）
    3. 常见共享环境位置：``~/.workbuddy/.../envs/screentrans``、项目内 ``.venv``
    4. PATH 上的 ``python`` / ``python3`` / ``py``（可选，见 include_path）

注意 ``~`` 是运行时用 ``Path.home()`` 展开的，不是写死的用户名。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# 共享虚拟环境相对用户主目录的常见位置（本机约定：Python 环境不放项目里）
_HOME_ENV_RELS = (
    ".workbuddy/binaries/python/envs/screentrans/Scripts/python.exe",
    ".workbuddy/binaries/python/envs/screentrans/bin/python",
    ".workbuddy/binaries/python/envs/screentrans/bin/python3",
    "miniconda3/envs/screentrans/python.exe",
    "anaconda3/envs/screentrans/python.exe",
)

# 项目内虚拟环境
_PROJECT_ENV_RELS = (
    ".venv/Scripts/python.exe",
    ".venv/bin/python",
    "venv/Scripts/python.exe",
    "venv/bin/python",
)


def _bases(extra_bases: Sequence = ()) -> list[Path]:
    """可能放着 pyenv.json / .venv 的目录，按优先级去重。"""
    cands: list[Path] = [Path(__file__).resolve().parent]
    if getattr(sys, "frozen", False):          # exe 运行时：exe 所在目录优先
        cands.append(Path(sys.executable).resolve().parent)
    cands.append(Path.cwd())
    cands.extend(Path(b) for b in extra_bases)

    seen: set[str] = set()
    out: list[Path] = []
    for b in cands:
        try:
            key = str(b.resolve())
        except Exception:
            key = str(b)
        if key not in seen:
            seen.add(key)
            out.append(b)
    return out


def _from_env() -> list[str]:
    out = []
    for name in ("SCREENTRANS_PY", "SCREEN_TRANSLATOR_PY"):
        p = (os.environ.get(name) or "").strip().strip('"')
        if p and Path(p).exists():
            out.append(p)
    return out


def _from_pyenv_json(bases: Sequence[Path]) -> list[str]:
    out = []
    for base in bases:
        cfg = base / "pyenv.json"
        try:
            if not cfg.exists():
                continue
            data = json.loads(cfg.read_text(encoding="utf-8"))
            p = (data.get("python") or "").strip() if isinstance(data, dict) else ""
            if not p:
                continue
            cand = Path(p).expanduser()
            if not cand.is_absolute():          # 支持写相对路径
                cand = base / cand
            if cand.exists():
                out.append(str(cand))
        except Exception:
            continue
    return out


def _from_common_locations(bases: Sequence[Path]) -> list[str]:
    out: list[str] = []
    home = Path.home()
    try:
        for rel in _HOME_ENV_RELS:
            p = home / rel
            if p.exists():
                out.append(str(p))
    except Exception:
        pass
    for base in bases:
        for rel in _PROJECT_ENV_RELS:
            p = base / rel
            try:
                if p.exists():
                    out.append(str(p))
            except Exception:
                continue
    return out


def _from_path() -> list[str]:
    out = []
    for name in ("python", "python3", "py"):
        p = shutil.which(name)
        if p:
            out.append(p)
    return out


def candidate_pythons(extra_bases: Sequence = (),
                      include_path: bool = True) -> list[str]:
    """按优先级返回**实际存在**的 Python 路径（去重）。

    include_path=False 时不含 PATH 上的解释器 —— 用于"无需校验即可信任"的场景
    （PATH 上的 python 未必装了本项目需要的包）。
    """
    bases = _bases(extra_bases)
    seq = (_from_env() + _from_pyenv_json(bases)
           + _from_common_locations(bases))
    if include_path:
        seq += _from_path()

    seen: set[str] = set()
    out: list[str] = []
    for p in seq:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def can_import(py: str, modules: str, timeout: float = 60.0) -> bool:
    """跑一次 ``python -c "import ..."`` 验证解释器是否装了所需包。"""
    mods = [m.strip() for m in modules.split(",") if m.strip()]
    if not mods:
        return Path(py).exists()
    code = "import " + ", ".join(mods)
    try:
        r = subprocess.run([py, "-c", code], capture_output=True,
                           timeout=timeout, creationflags=_NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False


def find_python(modules: str = "", extra_bases: Sequence = (),
                include_path: bool = True,
                verify: bool = True) -> Optional[str]:
    """找第一个能 import ``modules``（逗号分隔）的 Python；找不到返回 None。

    modules 为空或 verify=False 时，只按顺序取第一个存在的解释器，不做校验
    （省掉 python 冷启动的几秒开销）。
    """
    cands = candidate_pythons(extra_bases, include_path=include_path)
    if not cands:
        return None
    if not verify or not modules:
        return cands[0]
    for p in cands:
        if can_import(p, modules):
            return p
    return None
