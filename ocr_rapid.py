"""RapidOCR 客户端 —— 在子进程里跑高精度识别，主进程只发图片收文字。

设计要点:
  - 子进程由 pyenv.json 指向的共享 Python 启动（与离线翻译同一个环境），
    所以 onnxruntime / opencv 不需要打进 exe，绿色包体积不变。
  - 进程常驻：模型只加载一次，之后每次识别 0.4~0.9 秒。
  - 任何一步失败都返回 None，调用方（ocr.py）自动退回 Windows OCR，
    绝不让识别链路因为第三方引擎挂掉。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_REQ_TIMEOUT = 30.0      # 单次识别超时（秒）——首次含模型加载留足余量
_READY_TIMEOUT = 120.0   # 子进程启动 + 模型加载


def _script_path() -> Optional[Path]:
    cands = []
    if getattr(sys, "frozen", False):
        cands.append(Path(sys.executable).parent / "ocr_worker.py")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            cands.append(Path(meipass) / "ocr_worker.py")
    cands.append(Path(__file__).parent / "ocr_worker.py")
    for c in cands:
        try:
            if c.exists():
                return c
        except Exception:
            continue
    return None


def _log_dir() -> Path:
    try:
        from config import data_dir
        d = data_dir().parent / "logs"
    except Exception:
        d = Path(__file__).parent / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        d = Path(os.environ.get("TEMP", "."))
    return d


_python_cache: Optional[str] = None
_python_checked = False


def find_python() -> Optional[str]:
    """找能 import rapidocr 的 Python。

    候选顺序由 pylocator 统一给出（SCREENTRANS_PY 环境变量 → pyenv.json →
    常见共享环境/.venv → PATH），再逐个验证能否 import rapidocr。
    命中结果缓存；探测只做一次，避免每次识别都付冷启动代价。
    """
    global _python_cache, _python_checked
    if _python_checked:
        return _python_cache
    _python_checked = True

    cands: list[str] = []
    try:
        from pylocator import candidate_pythons
        cands = candidate_pythons()
    except Exception as e:
        log.debug("解释器探测失败: %s", e)

    for p in cands:
        try:
            r = subprocess.run(
                [p, "-c", "import rapidocr, onnxruntime"],
                capture_output=True, timeout=60, creationflags=_NO_WINDOW)
            if r.returncode == 0:
                _python_cache = p
                log.info("RapidOCR 使用 Python: %s", p)
                return p
        except Exception as e:
            log.debug("探测 %s 失败: %s", p, e)
    log.info("未找到带 rapidocr 的 Python，OCR 将使用 Windows 引擎")
    return None


class RapidOcrClient:
    """RapidOCR 常驻子进程客户端（单例使用）。"""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._id = 0
        self._disabled = False
        self._log_fp = None
        self._err_count = 0
        self._last_error = ""

    # ---------- 生命周期 ----------
    def _ensure_proc(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        if self._disabled:
            return False
        script = _script_path()
        py = find_python()
        if not script or not py:
            self._disabled = True
            return False
        try:
            if self._log_fp is None:
                self._log_fp = open(_log_dir() / "ocr_worker.log", "a",
                                    encoding="utf-8", errors="ignore")
            self._log_fp.write(f"\n===== {__import__('time').strftime('%Y-%m-%d %H:%M:%S')} "
                               f"启动 {py} {script} =====\n")
            self._log_fp.flush()
            self._proc = subprocess.Popen(
                [py, str(script)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self._log_fp, cwd=str(script.parent),
                creationflags=_NO_WINDOW,
            )
            # 等 ready 行（含模型加载）
            q: "queue.Queue" = queue.Queue()

            def _read_ready():
                try:
                    line = self._proc.stdout.readline()
                    q.put(line)
                except Exception:
                    q.put(b"")

            threading.Thread(target=_read_ready, daemon=True).start()
            try:
                line = q.get(timeout=_READY_TIMEOUT)
            except queue.Empty:
                self._last_error = "RapidOCR 子进程启动超时"
                log.warning(self._last_error)
                self._kill()
                self._disabled = True
                return False
            if not line:
                self._last_error = "RapidOCR 子进程无响应（rapidocr 未安装？）"
                log.warning(self._last_error)
                self._kill()
                self._disabled = True
                return False
            self._err_count = 0
            log.info("RapidOCR 引擎就绪")
            return True
        except Exception as e:
            self._last_error = f"启动 RapidOCR 失败: {e}"
            log.warning(self._last_error)
            self._kill()
            self._disabled = True
            return False

    def _kill(self) -> None:
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass
            try:
                p.wait(timeout=5)
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            self._kill()
        if self._log_fp is not None:
            try:
                self._log_fp.close()
            except Exception:
                pass
            self._log_fp = None

    # ---------- 识别 ----------
    def recognize(self, img, lang: str = "zh-Hans-CN") -> Optional[str]:
        """识别一张 PIL 图。失败返回 None（调用方回退 Windows OCR）。"""
        with self._lock:
            if not self._ensure_proc():
                return None
            try:
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                self._id += 1
                req = {"id": self._id,
                       "png": base64.b64encode(buf.getvalue()).decode("ascii"),
                       "lang": lang}
            except Exception as e:
                log.debug("准备 OCR 请求失败: %s", e)
                return None
            try:
                self._proc.stdin.write(
                    json.dumps(req).encode("utf-8") + b"\n")
                self._proc.stdin.flush()
                resp = self._read_response()
            except Exception as e:
                self._last_error = f"RapidOCR 通信失败: {e}"
                log.warning(self._last_error)
                self._kill()
                self._disabled = True
                return None
            if resp is None or not resp.get("ok"):
                self._err_count += 1
                self._last_error = (resp or {}).get("error", "识别失败")
                log.debug("RapidOCR 识别失败: %s", self._last_error)
                return None
            self._err_count = 0
            return resp.get("text", "")

    def _read_response(self) -> Optional[dict]:
        q: "queue.Queue" = queue.Queue()

        def _read():
            try:
                q.put(self._proc.stdout.readline())
            except Exception:
                q.put(None)

        threading.Thread(target=_read, daemon=True).start()
        try:
            line = q.get(timeout=_REQ_TIMEOUT)
        except queue.Empty:
            self._last_error = "RapidOCR 识别超时"
            log.warning(self._last_error)
            self._kill()
            self._disabled = True
            return None
        if not line:
            return None
        try:
            return json.loads(line.decode("utf-8"))
        except Exception:
            return None

    # ---------- 状态 ----------
    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def last_error(self) -> str:
        return self._last_error


_client: Optional[RapidOcrClient] = None
_client_lock = threading.Lock()


def get_client() -> RapidOcrClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = RapidOcrClient()
        return _client


def warmup_async() -> None:
    """后台预热：探测 Python + 起进程 + 加载模型。"""
    def _run():
        try:
            c = get_client()
            with c._lock:
                c._ensure_proc()
        except Exception as e:
            log.debug("RapidOCR 预热失败: %s", e)
    threading.Thread(target=_run, daemon=True).start()


def preflight() -> bool:
    """同步探测: 当前环境能否用 RapidOCR（给设置界面/启动检查用）。"""
    return find_python() is not None and _script_path() is not None
