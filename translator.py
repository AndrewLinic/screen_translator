"""翻译模块 - 离线优先 (Argos Translate)，可选在线 (百度 API)

层级:
    1. Argos Translate (离线, 默认) - 已下载的 zh<->en 模型
    2. 百度翻译 API (在线, 可选) - 用户配置 AppID/Secret 时启用
    3. Mock 兜底 - 所有路径都失败时使用，确保字幕永远有内容
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Windows: 子进程一律不弹控制台黑窗。
# python.exe 是控制台子系统程序，不加此标志时每次 Popen 都会弹出一个
# cmd 窗口；离线翻译 worker 常驻，黑窗会挂满整个程序生命周期。
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# 在 import argos 之前设置：全程离线，禁止 stanza / 其它联网行为
os.environ.setdefault("STANZA_RESOURCES_DIR", "")
# 关键：不设的话 Argos 认为 stanza 可用，加载日/韩等模型时会尝试联网下载分句模型
os.environ.setdefault("ARGOS_STANZA_AVAILABLE", "0")

# ===== 模型目录解析 =====
# 模型统一放在 assets/argos_models/ (项目目录 或 exe 同级目录),
# 通过 ARGOS_PACKAGES_DIR 告诉 Argos 去哪里找, 不做二次拷贝。
_ARGOS_DIR_CACHE: Optional[Path] = None


def _candidate_model_dirs() -> list:
    cands = []
    if getattr(sys, "frozen", False):
        cands.append(Path(sys.executable).parent / "assets" / "argos_models")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            cands.append(Path(meipass) / "assets" / "argos_models")
    cands.append(Path(__file__).parent / "assets" / "argos_models")
    # 用户数据目录 (老版本可能拷贝过)
    data_dir = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    cands.append(data_dir / "argos-translate" / "packages")
    return cands


def runtime_model_dir() -> Path:
    """模型实际加载目录（必须是纯 ASCII 路径）。

    CTranslate2 / sentencepiece 的底层 C++ 无法打开含中文的路径，
    所以哪怕 exe 放在中文目录，模型也要同步到这个英文目录下再加载。
    """
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "ScreenTranslator" / "models"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
                ) / "argos-translate" / "packages"


def bundled_model_dir() -> Optional[Path]:
    """分发源目录：exe 同级 / 打包内部 / 项目 assets。"""
    cands = []
    if getattr(sys, "frozen", False):
        cands.append(Path(sys.executable).parent / "assets" / "argos_models")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            cands.append(Path(meipass) / "assets" / "argos_models")
    cands.append(Path(__file__).parent / "assets" / "argos_models")
    for c in cands:
        try:
            if c.exists() and any(c.glob("translate-*")):
                return c
        except Exception:
            continue
    return None


def sync_bundled_models(dest: Optional[Path] = None) -> list:
    """把分发源里的模型同步到运行目录（只拷没拷过的，返回新增列表）。

    同步过的包记在 .synced.json 里，用户手动删除某语言后不会被重新拷回。
    """
    dest = dest or runtime_model_dir()
    src = bundled_model_dir()
    if not src:
        return []
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log.warning("无法创建模型目录 %s: %s", dest, e)
        return []

    state_file = dest / ".synced.json"
    synced = {}
    if state_file.exists():
        try:
            synced = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            synced = {}

    import shutil
    added = []
    for pkg in sorted(src.glob("translate-*")):
        if pkg.name in synced:
            continue
        target = dest / pkg.name
        try:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            log.info("同步模型 %s -> %s", pkg.name, dest)
            shutil.copytree(pkg, target)
            synced[pkg.name] = str(pkg)
            added.append(pkg.name)
        except Exception as e:
            log.warning("同步模型 %s 失败: %s", pkg.name, e)
    if added:
        try:
            state_file.write_text(json.dumps(synced, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        except Exception:
            pass
    return added


def argos_packages_dir() -> Optional[Path]:
    """找到含模型的目录，并写入 ARGOS_PACKAGES_DIR（子进程会继承）。"""
    global _ARGOS_DIR_CACHE
    if _ARGOS_DIR_CACHE and any(_ARGOS_DIR_CACHE.glob("translate-*")):
        return _ARGOS_DIR_CACHE
    # 已显式指定
    env = os.environ.get("ARGOS_PACKAGES_DIR", "")
    if env:
        p = Path(env)
        if p.exists() and any(p.glob("translate-*")):
            _ARGOS_DIR_CACHE = p
            return p
    # 1) 运行目录（ASCII，优先）
    rt = runtime_model_dir()
    try:
        if rt.exists() and any(rt.glob("translate-*")):
            os.environ["ARGOS_PACKAGES_DIR"] = str(rt)
            _ARGOS_DIR_CACHE = rt
            log.info("Argos 模型目录: %s", rt)
            return rt
    except Exception:
        pass
    # 2) 首次运行：从分发源同步到运行目录
    try:
        if sync_bundled_models(rt):
            os.environ["ARGOS_PACKAGES_DIR"] = str(rt)
            _ARGOS_DIR_CACHE = rt
            log.info("已同步模型到 %s", rt)
            return rt
    except Exception as e:
        log.debug("同步模型失败: %s", e)
    # 3) 退化：直接用分发源（路径含中文时底层可能加载失败）
    for c in _candidate_model_dirs():
        try:
            if c.exists() and any(c.glob("translate-*")):
                os.environ["ARGOS_PACKAGES_DIR"] = str(c)
                _ARGOS_DIR_CACHE = c
                log.info("Argos 模型目录: %s", c)
                return c
        except Exception:
            continue
    # 都没模型：仍指向首选位置，方便 download/install 脚本写入
    pref = _candidate_model_dirs()[0]
    os.environ.setdefault("ARGOS_PACKAGES_DIR", str(pref))
    _ARGOS_DIR_CACHE = pref
    return pref


# 模块导入即配置好模型目录（必须在 import argostranslate 之前）
argos_packages_dir()


def _ensure_argos_settings() -> bool:
    """锁定离线配置：分句器用 MiniSBD（不联网下载 stanza 模型）。

    必须在任何 `import argostranslate.translate` 之前调用一次，
    否则 Argos 会按默认策略选中 Stanza 分句器并尝试联网下载。
    """
    try:
        from argostranslate import settings as _s
        _s.chunk_type = _s.ChunkType.MINISBD
        return True
    except Exception:
        return False


_ensure_argos_settings()


# ===== 语言自动检测 =====
def detect_language(text: str) -> str:
    """按字符特征猜测源语言代码。"""
    counters = {"ja": 0, "ko": 0, "zh": 0, "ru": 0, "th": 0, "ar": 0, "en": 0}
    for ch in text:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF:      # 平假名 / 片假名
            counters["ja"] += 2
        elif 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF:  # 谚文
            counters["ko"] += 2
        elif 0x4E00 <= o <= 0x9FFF:    # 汉字
            counters["zh"] += 1
        elif 0x0400 <= o <= 0x04FF:    # 西里尔
            counters["ru"] += 2
        elif 0x0E00 <= o <= 0x0E7F:    # 泰文
            counters["th"] += 2
        elif 0x0600 <= o <= 0x06FF:    # 阿拉伯
            counters["ar"] += 2
        elif ch.isascii() and ch.isalpha():
            counters["en"] += 1
    best = max(counters, key=lambda k: counters[k])
    return best if counters[best] > 0 else "en"


# ===== Argos Translate (离线) =====
_argos_translations: dict = {}  # (from_code, to_code) -> Translation object
_argos_lock = threading.Lock()
_argos_ready = False
_argos_available_logged = False  # 避免重复输出"Argos 未安装"日志
_argos_error: Optional[str] = None
_argos_settings = None  # 缓存 argos settings 对象
_argos_subprocess_supported: Optional[bool] = None  # 是否支持 subprocess 调用
_lang_graph: Optional[dict] = None  # 已安装语言对图 (缓存)


def _qt_loaded() -> bool:
    """GUI 进程里 Qt 与 ctranslate2 同时加载会 segfault，必须避开。"""
    return any(m.split(".")[0] in ("PyQt5", "PyQt6", "PySide2", "PySide6")
               for m in sys.modules)


def init_argos() -> bool:
    """初始化 Argos 离线引擎。返回是否成功。

    两条路径:
    1) 直接 import (脚本环境，没有 Qt)
    2) worker 子进程调用 offline_translate.py（GUI / 打包 exe）
    """
    global _argos_ready, _argos_error, _argos_settings, _argos_available_logged
    if _argos_ready:
        return True
    # 路径 1: 直接 import（仅限非 GUI 进程）
    if not _qt_loaded():
        try:
            from argostranslate import translate as _tr
            from argostranslate import settings as _settings
            _argos_settings = _settings
            _settings.chunk_type = _settings.ChunkType.MINISBD
            langs = _tr.get_installed_languages()
            log.info("Argos 已加载 (直接导入): %s", [(l.code, l.name) for l in langs])
            _argos_ready = True
            return True
        except ImportError:
            pass  # 继续走路径 2
        except Exception as e:
            log.debug("直接导入 argos 失败: %s", e)

    # 路径 2: subprocess (打包 exe 的情况)
    if _offline_translate_script_exists() and find_python_executable():
        log.info("Argos: 通过 subprocess 调用 offline_translate.py")
        _argos_ready = True  # 标记可用（每次翻译时再确认 Python 是否真存在）
        _argos_available_logged = True
        return True

    if not _argos_available_logged:
        _argos_error = "未安装 argostranslate，请运行 install_offline_translate.bat"
        log.warning("Argos 离线翻译未配置 - 运行 install_offline_translate.bat 解锁")
        _argos_available_logged = True
    return False


def _offline_translate_script_exists() -> bool:
    """找 offline_translate.py。优先 exe 同目录,否则项目根目录。"""
    global _argos_subprocess_supported
    if _argos_subprocess_supported is False:
        return False
    if _argos_subprocess_supported is True:
        return True
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "offline_translate.py")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "offline_translate.py")
    candidates.append(Path(__file__).parent / "offline_translate.py")
    for c in candidates:
        if c.exists():
            _argos_subprocess_supported = True
            return True
    _argos_subprocess_supported = False
    return False


_ARGOS_PYTHON_CACHE: dict = {}  # 缓存: python_path -> has_argos (bool)
_argos_python: Optional[str] = None  # 已确认的 argos Python 路径


def _python_has_argos(py: str) -> bool:
    """测试给定 Python 能不能 import argostranslate。"""
    cached = _ARGOS_PYTHON_CACHE.get(py)
    if cached is True:
        return True
    if cached is False:
        return False
    try:
        r = subprocess.run(
            [py, "-c", "import argostranslate; print('ok')"],
            capture_output=True, text=True, timeout=30,
            creationflags=_NO_WINDOW,
        )
        has = r.returncode == 0
        _ARGOS_PYTHON_CACHE[py] = has
        return has
    except subprocess.TimeoutExpired:
        # 冷启动 import 要 5~15 秒，超时不代表没装，不缓存 False
        log.debug("探测 %s 超时（冷启动 import 慢），不缓存失败", py)
        return False
    except Exception:
        _ARGOS_PYTHON_CACHE[py] = False
        return False


def find_python_executable() -> Optional[str]:
    """找一个能跑 argostranslate 的 Python 解释器。

    策略:
    0. 优先读 pyenv.json (install_offline_translate.bat 写好的)
    1. 检查缓存的 _argos_python
    2. 在 PATH 里逐个 python 解释器跑一个小测试,看谁能 import argostranslate
    3. 如果都找不到,返回 PATH 里的 python (后面用会失败但至少给个友好错误)
    """
    global _argos_python
    if _argos_python:
        return _argos_python

    import shutil

    # 0. 环境变量 / pyenv.json / 常见共享环境（最快路径；install 脚本写入前已
    #    验证过 argos 可用，直接信任，避免每次启动都花 5~15 秒冷启动 import 探测）
    #    include_path=False: PATH 上的 python 未必装了 argos，留到下面逐个验证
    try:
        from pylocator import candidate_pythons
        for p in candidate_pythons(include_path=False):
            _argos_python = p
            log.info("从已配置位置加载 Python: %s", p)
            return p
    except Exception as e:
        log.debug("解释器探测失败: %s", e)

    candidates_set = set()
    # PATH 里
    for cand in ("python", "python3", "py"):
        p = shutil.which(cand)
        if p:
            candidates_set.add(p)
    # Windows 常见位置
    if os.name == "nt":
        for env in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(env)
            if not base:
                continue
            for sub in (r"Programs\Python\Python313\python.exe",
                        r"Programs\Python\Python312\python.exe",
                        r"Programs\Python\Python311\python.exe",
                        r"Programs\Python\Python310\python.exe"):
                candidates_set.add(str(Path(base) / sub))
        # 用户的 AppData
        for sub in (r"Microsoft\WindowsApps\python.exe",
                    r"Microsoft\WindowsApps\python3.exe"):
            p = str(Path(os.environ.get("LOCALAPPDATA", "")) / sub)
            if Path(p).exists():
                candidates_set.add(p)

    # 测每个 candidate 看谁能 import argostranslate
    for py in candidates_set:
        if not Path(py).exists():
            continue
        if _python_has_argos(py):
            _argos_python = py
            return py

    # fallback: 返回 PATH 里的第一个 python (后续翻译会失败但有清晰错误)
    if candidates_set:
        return sorted(candidates_set)[0]
    return None


class _ArgosWorker:
    """常驻的 Argos 翻译子进程。

    模型加载一次要 3~4 秒，每次翻译都重启进程太慢；
    而且把 ctranslate2 放进 GUI 进程会和 Qt 冲突（直接 segfault），
    所以统一放到独立进程里，通过 JSON 行协议通信。

    通信必须带超时: stdout.readline() 本身无法超时，
    首次加载 6 个模型可能要 10 秒以上，若 worker 卡死会永久阻塞调用方。
    方案: 专门一个 reader 线程把响应行放进队列，请求侧 queue.get(timeout=)。
    每个请求带自增 id，worker 原样回带，读取时跳过过期响应。
    """

    def __init__(self, py: str, script: str):
        self.py = py
        self.script = script
        self.proc = None
        self._queue = None      # queue.Queue[str|None] 未带 id 的帧（ready 等）
        self._reader = None     # reader 线程
        self._req_id = 0
        self._stderr_fh = None  # worker stderr 写到文件（见 _spawn）
        # 并发支持: 响应按 id 分发到各等待者自己的队列（P2 #15）
        self._pending: dict = {}
        self._pending_lock = threading.Lock()

    def _spawn(self) -> bool:
        try:
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            # stderr 不能用 PIPE 又从不读取：argostranslate / ctranslate2 的
            # 日志会写满管道缓冲区(4~8KB)，之后 worker 每写一行 stderr 都会
            # 阻塞，整个翻译进程假死。改写到日志文件，顺便方便排查。
            if self._stderr_fh is None:
                try:
                    import tempfile
                    p = Path(tempfile.gettempdir()) / "screen_translator_worker_stderr.log"
                    self._stderr_fh = open(p, "w", encoding="utf-8", errors="replace")
                except Exception:
                    self._stderr_fh = None
            self.proc = subprocess.Popen(
                [self.py, self.script, "--serve"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self._stderr_fh or subprocess.DEVNULL,
                text=True, encoding="utf-8",
                errors="replace", bufsize=1, env=env,
                creationflags=_NO_WINDOW,
            )
            # reader 线程: 持续把 stdout 的行分发出去
            # - 带 id 且有等待者 -> 直接投递到该请求的队列
            # - 其它（ready 等无 id 帧）-> 丢进 _queue 供 _spawn 读取
            # EOF 时唤醒所有等待者（投 None）
            import queue as _q
            self._queue = _q.Queue()

            def _pump():
                try:
                    for line in self.proc.stdout:
                        data = None
                        try:
                            data = json.loads(line)
                        except Exception:
                            pass
                        if (isinstance(data, dict) and data.get("id") is not None):
                            with self._pending_lock:
                                pq = self._pending.get(data["id"])
                            if pq is not None:
                                pq.put(data)
                                continue
                        self._queue.put(line)
                except Exception:
                    pass
                finally:
                    self._queue.put(None)
                    with self._pending_lock:
                        waiters = list(self._pending.values())
                        self._pending.clear()
                    for wq in waiters:
                        wq.put(None)

            self._reader = threading.Thread(target=_pump, daemon=True)
            self._reader.start()
            # 等 ready。worker 启动时会先把所有已装模型真正加载进内存
            # （懒加载的模型首次翻译要 60 秒+），所以 ready 可能来得慢，
            # 超时放宽到 240 秒。ready 一到，之后每次翻译都是秒级。
            try:
                line = self._queue.get(timeout=240)
            except _q.Empty:
                log.warning("Argos worker 启动超时(240s)")
                self._kill()
                return False
            if not line:
                err = ""
                try:
                    if self._stderr_fh:
                        self._stderr_fh.flush()
                        with open(self._stderr_fh.name, "r", encoding="utf-8",
                                  errors="replace") as f:
                            err = f.read()[:300]
                except Exception:
                    pass
                log.warning("Argos worker 启动失败: %s", err.strip())
                self._kill()
                return False
            log.info("Argos worker 就绪")
            return True
        except Exception as e:
            log.debug("启动 Argos worker 失败: %s", e)
            self._kill()
            return False

    def _kill(self):
        if self.proc is not None:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None
        self._queue = None
        self._reader = None
        # 关掉 stderr 文件句柄（下次 _spawn 重新打开，避免句柄泄漏）
        if self._stderr_fh is not None:
            try:
                self._stderr_fh.close()
            except Exception:
                pass
            self._stderr_fh = None
        # 唤醒所有等响应的调用方（reader 已随进程死亡退出）
        with self._pending_lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for wq in waiters:
            wq.put(None)

    def _request(self, payload: dict, timeout: float = 180.0, retries: int = 2):
        """发一个 JSON 请求，返回响应 dict（失败返回 None）。带超时。

        超时处理策略（关键，否则会活锁）:
        - 超时 -> 杀掉 worker 后【不再重试】。因为 worker 是懒加载模型，
          重启后又要从头加载，大概率再次超时，"杀-重启-超时"会永远循环，
          表现就是翻译永远出不来。失败后交给上层走一次性子进程兜底。
        - 进程死亡 -> 可以重试（重启是有意义的）。

        并发（P2 #15）: 只有"确保存活 + 写请求"这一小段持 _worker_lock，
        等响应在自己的队列上等、不持锁 —— worker 卡死或慢启动时不再
        阻塞其它调用线程；响应由 reader 线程按 id 分发到各等待者。
        """
        import queue as _q
        for attempt in range(1, retries + 1):
            with _worker_lock:
                if self.proc is None or self.proc.poll() is not None:
                    if not self._spawn():
                        return None
                self._req_id += 1
                req = dict(payload)
                req["id"] = self._req_id
                my_q = _q.Queue()
                with self._pending_lock:
                    self._pending[req["id"]] = my_q
                try:
                    self.proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
                    self.proc.stdin.flush()
                except Exception as e:
                    log.debug("worker 通信失败(%d/%d): %s", attempt, retries, e)
                    with self._pending_lock:
                        self._pending.pop(req["id"], None)
                    self._kill()
                    continue
            try:
                deadline = time.time() + timeout
                while True:
                    remain = deadline - time.time()
                    if remain <= 0:
                        raise TimeoutError(f"worker 响应超时 {timeout}s")
                    try:
                        resp = my_q.get(timeout=remain)
                    except _q.Empty:
                        raise TimeoutError(f"worker 响应超时 {timeout}s")
                    if resp is None:
                        raise RuntimeError("worker 进程已退出")
                    if resp.get("id") == req["id"]:
                        return resp
            except TimeoutError as e:
                log.warning("worker 请求超时并放弃(不重启重试): %s", e)
                self._kill()
                return None
            except Exception as e:
                log.debug("worker 通信失败(%d/%d): %s", attempt, retries, e)
                self._kill()
            finally:
                with self._pending_lock:
                    self._pending.pop(req["id"], None)
        return None

    def list_pairs(self, timeout: float = 90.0) -> list:
        data = self._request({"cmd": "list"}, timeout=timeout)
        return data.get("pairs", []) if data and data.get("ok") else []

    def translate(self, text: str, from_code: str, to_code: str,
                  timeout: float = 180.0) -> Optional[str]:
        data = self._request({"cmd": "translate", "text": text,
                              "from": from_code, "to": to_code},
                             timeout=timeout)
        if not data:
            return None
        if data.get("ok"):
            return data.get("text", "")
        log.debug("worker 翻译失败: %s", data.get("error"))
        return None


_worker: Optional[_ArgosWorker] = None
_worker_lock = threading.Lock()


def _get_worker() -> Optional[_ArgosWorker]:
    global _worker
    with _worker_lock:
        if _worker is not None:
            return _worker
        py = find_python_executable()
        script = _resolve_offline_script()
        if py and script:
            _worker = _ArgosWorker(py, script)
        return _worker


def shutdown_worker():
    """退出程序时杀掉常驻 worker 子进程，避免残留 python 进程。"""
    global _worker
    with _worker_lock:
        if _worker is not None:
            _worker._kill()
        _worker = None


def warmup_worker() -> bool:
    """后台预热：拉起 worker 并做一次真实翻译，把模型加载进内存。

    首次翻译要加载全部模型（10~60 秒）。启动阶段就预热完，
    用户划选区后的第一次翻译就能秒出结果。
    """
    try:
        w = _get_worker()
        if w is None:
            return False
        out = w.translate("hello", "en", "zh", timeout=240)
        ok = out is not None
        log.info("离线引擎预热: %s", "完成" if ok else "失败")
        return ok
    except Exception as e:
        log.debug("预热失败: %s", e)
        return False


def _run_argos_subprocess(text: str, from_code: str, to_code: str) -> Optional[str]:
    """通过常驻 worker 进程调用 offline_translate.py。"""
    w = _get_worker()
    if w:
        out = w.translate(text, from_code, to_code)
        if out is not None:
            return out

    # 回退：一次性子进程
    py = find_python_executable()
    script = _resolve_offline_script()
    if not py or not script:
        return None
    try:
        result = subprocess.run(
            [py, script, text, from_code, to_code],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout.rstrip("\n")
        log.debug("Argos subprocess 失败 rc=%d: %s",
                  result.returncode, (result.stderr or "").strip()[:200])
        return None
    except Exception as e:
        log.debug("Argos subprocess 异常: %s", e)
        return None


def _build_graph(langs) -> dict:
    """已安装语言对 -> 有向图 {from_code: {to_code, ...}}"""
    graph: dict = {}
    for lang in langs:
        graph.setdefault(lang.code, set())
        for t in getattr(lang, "translations_from", []) or []:
            tgt = getattr(t, "to_lang", None)
            if tgt is not None:
                graph[lang.code].add(tgt.code)
    return graph


def _find_path(graph: dict, src: str, dst: str, max_hops: int = 3):
    """在语言图中找 src->dst 的最短路径，优先经过英语。"""
    if src == dst:
        return [src]
    # 1) 直达
    if dst in graph.get(src, ()):
        return [src, dst]
    # 2) 经英语中转（最常见，如 ja->en->zh）
    if "en" in graph.get(src, ()) and dst in graph.get("en", ()):
        return [src, "en", dst]
    # 3) 通用 BFS
    from collections import deque
    q = deque([[src]])
    seen = {src}
    while q:
        path = q.popleft()
        if len(path) > max_hops + 1:
            continue
        node = path[-1]
        for nxt in sorted(graph.get(node, ())):
            if nxt == dst:
                return path + [nxt]
            if nxt not in seen:
                seen.add(nxt)
                q.append(path + [nxt])
    return None


def _direct_translate(text: str, from_code: str, to_code: str) -> Optional[str]:
    """单跳 Argos 翻译（带缓存）。"""
    from argostranslate import translate as _tr
    key = (from_code, to_code)
    with _argos_lock:
        trans = _argos_translations.get(key)
        if trans is None:
            src = _tr.get_language_from_code(from_code)
            tgt = _tr.get_language_from_code(to_code)
            if src is None or tgt is None:
                return None
            trans = src.get_translation(tgt)
            if trans is None:
                return None
            _argos_translations[key] = trans
    return trans.translate(text)


def _translate_inprocess(text: str, from_code: str, to_code: str) -> Optional[str]:
    """进程内翻译，支持多跳中转（pivot）。按行翻译，保留分段结构。"""
    from argostranslate import translate as _tr
    global _lang_graph
    if _lang_graph is None:
        _lang_graph = _build_graph(_tr.get_installed_languages())
    path = _find_path(_lang_graph, from_code, to_code)
    if not path:
        log.debug("无可用翻译路径: %s -> %s", from_code, to_code)
        return None
    if len(path) > 2:
        log.debug("中转翻译: %s", " -> ".join(path))
    out = []
    for ln in text.split("\n"):
        if not ln.strip():
            out.append("")      # 空行 / 分段直接保留
            continue
        cur = ln
        for a, b in zip(path, path[1:]):
            cur = _direct_translate(cur, a, b)
            if cur is None:
                return None
        out.append(cur)
    return "\n".join(out)


def argos_translate(text: str, from_code: str, to_code: str) -> Optional[str]:
    """Argos 离线翻译（支持中转）。返回 None 表示失败。"""
    global _argos_available_logged
    if not text.strip():
        return ""
    if not _argos_ready and not init_argos():
        return None
    if from_code == "auto":
        from_code = detect_language(text)
    if from_code == to_code:
        return text

    # 路径 1: 本地 in-process 翻译（GUI 进程跳过，Qt 与 ctranslate2 冲突会崩）
    if not _qt_loaded():
        try:
            from argostranslate import translate as _tr  # noqa: F401
            return _translate_inprocess(text, from_code, to_code)
        except ImportError:
            pass  # 走路径 2

    # 路径 2: 常驻 worker 子进程（内部同样支持中转）
    out = _run_argos_subprocess(text, from_code, to_code)
    if out is not None:
        return out
    return None


# ===== 百度翻译 API (在线，可选) =====
import hashlib
import random
import string

import requests

BAIDU_API_URL = "https://fanyi-api.baidu.com/api/trans/vip/translate"

# 离线语言代码 -> 中文名
OFFLINE_LANG_NAMES = {
    "zh": "中文", "en": "英语", "ja": "日语", "ko": "韩语",
    "fr": "法语", "de": "德语", "es": "西班牙语", "ru": "俄语",
    "it": "意大利语", "pt": "葡萄牙语", "th": "泰语", "vi": "越南语",
    "ar": "阿拉伯语", "nl": "荷兰语", "pl": "波兰语", "tr": "土耳其语",
    "id": "印尼语", "hi": "印地语", "sv": "瑞典语", "uk": "乌克兰语",
    "zt": "繁体中文",
}

TARGET_LANGS = {
    "zh": "中文", "en": "英语", "jp": "日语", "kor": "韩语",
    "fra": "法语", "de": "德语", "ru": "俄语", "th": "泰语",
    "spa": "西班牙语", "pt": "葡萄牙语", "it": "意大利语",
    "nl": "荷兰语", "vie": "越南语",
}


# 离线代码 -> 百度 API 代码
BAIDU_CODE_MAP = {
    "ja": "jp", "ko": "kor", "fr": "fra", "es": "spa", "ru": "ru",
    "pt": "pt", "it": "it", "nl": "nl", "vi": "vie", "ar": "ara",
    "th": "th", "de": "de", "zh": "zh", "en": "en", "id": "id",
}


def _sign(appid: str, q: str, salt: str, secret: str) -> str:
    return hashlib.md5((appid + q + salt + secret).encode("utf-8")).hexdigest()


def baidu_translate(text: str, appid: str, secret: str,
                    from_lang: str = "auto", to_lang: str = "zh",
                    timeout: float = 5.0) -> Optional[str]:
    if not text.strip() or not appid or not secret:
        return None
    # 统一的代码转百度代码（如 ja -> jp, ko -> kor）
    from_lang = BAIDU_CODE_MAP.get(from_lang, from_lang)
    to_lang = BAIDU_CODE_MAP.get(to_lang, to_lang)
    salt = "".join(random.choices(string.ascii_letters + string.digits, k=10))
    sign = _sign(appid, text, salt, secret)
    try:
        resp = requests.post(BAIDU_API_URL, data={
            "q": text, "from": from_lang, "to": to_lang,
            "appid": appid, "salt": salt, "sign": sign,
        }, timeout=timeout)
        data = resp.json()
    except Exception as e:
        log.warning("百度翻译请求失败: %s", e)
        return None
    if "error_code" in data:
        log.warning("百度翻译错误 %s: %s", data.get("error_code"), data.get("error_msg"))
        return None
    parts = data.get("trans_result") or []
    return "\n".join(p.get("dst", "") for p in parts if p.get("dst"))


# ===== Mock 兜底 =====
def mock_translate(text: str, target_lang: str = "zh") -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    if target_lang.startswith("zh"):
        return "\n".join(f"[译文] {ln}" for ln in lines)
    return "\n".join(f"{ln} [en]" for ln in lines)


# ===== 主入口 =====
def merge_lines_for_translation(text: str) -> str:
    """合并段内被 OCR 硬换行切断的半句话，再送翻译引擎。

    屏幕文本经常在句子中间换行（"…after the trip. Then / bring them
    back to Sebas."）。逐行翻译时 "Then" 单独成段会被离线引擎译成
    乱码甚至幻觉文本。规则：
    - 上一行不以句末标点结尾，且当前行以小写字母开头 => 两行是同一句，拼接
    - 空行（段落边界）原样保留
    """
    if "\n" not in text:
        return text
    sent_end = tuple(".!?:;。！？：；…）)]\"'”’")
    out = []
    buf = ""
    for ln in text.split("\n"):
        s = ln.strip()
        if not s:
            if buf:
                out.append(buf)
                buf = ""
            out.append("")
            continue
        if buf and s[:1].islower() and not buf.rstrip().endswith(sent_end):
            buf += " " + s      # 句中断行 -> 拼接
        else:
            if buf:
                out.append(buf)
            buf = s
    if buf:
        out.append(buf)
    return "\n".join(out)


_HALLUCINATION_PAT = re.compile(
    r"[（(]\s*原始内容存档于\s*\d{4}\s*[-年/]\s*\d{1,2}\s*[-月/]\s*\d{1,2}\s*[)）]\s*[.。]?"
)


def _strip_hallucination(text: str) -> str:
    """清洗离线模型的引用式幻觉。

    Argos en->zh 模型对 "Reward: ..." 等游戏 UI 短句会确定性地在译文
    前面生成维基百科式的引用垃圾（如 "(原始内容存档于2017-09-21)."，
    训练数据残留）。实测可复现、模式固定，直接剔除。
    """
    out = _HALLUCINATION_PAT.sub("", text)
    return out.strip() or text.strip()


# ===== 多义词上下文消歧（翻译后处理）=====
# Argos 是固定权重的神经机器翻译，en->zh 时对多义词会输出训练数据里的
# "最高频义项"，无法根据句子上下文消歧。实测只有少数义项被【系统性译错】
# （即：无论语境如何都选错），这些可以精准修复。绝大多数多义词 Argos 其实
# 译得对（如 take charge->负责、criminal charge->刑事指控、free of charge->免费、
# charge your phone->给手机充电），这些一律不碰。
#
# 实测系统性错译（可精准修复）:
#   lead/led the charge   -> "引导电荷"/"主控"（应为"带头冲锋"）
#   the charge began      -> "开始充电"（应为"冲锋开始"）
#   charge forward        -> "前期收费"（应为"向前冲锋"）
#   a charge of energy    -> "a 能源费"（应为"一股能量"）
#   settled in            -> "已结算"（应为"定居/安顿下来"）
#   settle down here      -> "下来"（漏译"定居"）
#
# 策略: 只在【原文有明确搭配触发 + 译文出现已知错译词】双重条件同时满足时
# 才替换，宁缺毋滥，避免误伤 Argos 本就译对的句子。
# 每条规则: (source_pattern, wrong_pattern, replacement)

# 每条: (source_pattern, wrong_pattern, replacement)
# source_pattern: 在【英文原文】上匹配，命中说明语境触发
# wrong_pattern:  在【中文译文】上匹配，命中说明 Argos 确实译错了
# replacement:    替换字符串（可用 \1 引用 wrong_pattern 的分组）
_DISAMBIG_RULES = [
    # ---- charge 的"冲锋"义项（Argos 系统性译成 电荷/充电/收费/指控/控罪/电话）----
    # lead/led the charge => 带头冲锋
    (r"\b(?:lead|led)\s+the\s+charge\b",
     r"引导电荷|主控|控罪|指控|充电|收费",
     "带头冲锋"),
    # the charge began/begun => 冲锋开始
    (r"\bthe\s+charge\s+(?:began|begun|started)\b",
     r"开始充电|开始收费|充电开始|指控",
     "冲锋"),
    # charge forward / charge into / charge toward / charge at => 冲锋
    (r"\bcharge\s+(?:forward|into|toward|at)\b",
     r"前期收费|收费|充电|指控|控罪|开始战斗",
     "冲锋"),
    # a charge of X（能量/电荷语境）=> 一股能量
    (r"\ba\s+charge\s+of\b",
     r"(?:能源|电|一)?(?:费|电荷|电荷费)",
     "一股能量"),
    # charge 后接 明确冲锋目标（enemy/foe/battle/line/ranks）=> 冲锋
    (r"\bcharge\b(?=\s+(?:the\s+)?(?:enemy|foe|battle|lines|ranks|gate|wall|hill))",
     r"充电|收费|指控|控罪",
     "冲锋"),

    # ---- settle 的"定居/安顿"义项（Argos 系统性译成 结算/解决）----
    # settled/settle + 地点（in/back/here/there/down/into/around）=> 定居
    (r"\bsettle[ds]?\s+(?:in|back|here|there|down|into|around|nearby)\b",
     r"已?(?:结算|解决|和解)|下来",
     "定居"),
    # settled in ...（"已结算"是最高频错译）
    (r"\bsettled\s+in\b",
     r"已?结算",
     "定居"),

    # ---- spring 的"春天"义项（Argos 孤立时译"弹簧"）----
    # in/during/by + spring（且非 spring coil 等）=> 春天
    (r"\b(?:in|during|by|the|this|last|next)\s+spring\b(?!\s+(?:coil|board|roll))",
     r"弹簧",
     "春天"),

    # ---- bark 的"树皮"义项（Argos 偶译"剥皮"）----
    (r"\bthe\s+bark\s+of\s+(?:the\s+)?(?:tree|oak|birch|pine|maple)\b",
     r"剥皮",
     "树皮"),
]

_DISAMBIG_COMPILED = [
    (re.compile(src, re.IGNORECASE), re.compile(wrong), repl)
    for src, wrong, repl in _DISAMBIG_RULES
]


def _disambiguate(text_zh: str, text_en: str) -> str:
    """翻译后消歧: 根据【英文原文】的搭配，修正【中文译文】里 Argos 选错的义项。

    双重条件（缺一不可）:
      1. 英文原文命中某条规则的 source_pattern（语境触发）
      2. 中文译文命中该规则的 wrong_pattern（Argos 确实译错了）
    两个都命中才替换，保证不误伤 Argos 译对的句子。
    """
    if not text_zh or not text_en:
        return text_zh
    out = text_zh
    for src_re, wrong_re, repl in _DISAMBIG_COMPILED:
        if src_re.search(text_en):
            out, n = wrong_re.subn(repl, out, count=1)
    return out


# 游戏 UI 标签: "Reward: ..." 这类行的标签自译为中文，只把内容送引擎。
# 实测 Argos en->zh 对含 "Reward:" 的整行会确定性产出幻觉 + 坏翻译
# （"Reward: Progress toward X." -> "(原始内容存档于...). 进步走向X."），
# 而内容单独翻译质量良好；把中文标签直接混进英文输入反而更糟（"解锁:"->"QQ:"）。
GAME_LABELS = {
    "reward": "奖励", "unlock": "解锁", "progress": "进度",
    "objective": "目标", "task": "任务", "goal": "目标",
    "hint": "提示", "note": "备注", "tip": "提示", "tips": "提示",
    "requirement": "需求", "requirements": "需求",
    "description": "说明", "warning": "警告", "bonus": "加成",
    "effect": "效果", "cost": "花费", "level": "等级", "name": "名称",
    "location": "位置", "difficulty": "难度",
}
_LABEL_LINE_RE = re.compile(
    r"^\s*(" + "|".join(GAME_LABELS) + r")\s*[:：]\s*(.*)$",
    re.IGNORECASE,
)


def translate_labelled(text: str, engine) -> str:
    """拆分游戏标签行: 标签用固定中文，内容部分交给 engine 整体翻译。

    非标签行按原样成组交给 engine（保持句中断行合并等上下文优势）。
    """
    out = []
    buf = []

    def flush():
        if buf and "\n".join(buf).strip():
            out.append(engine("\n".join(buf)))
        buf.clear()

    for ln in text.split("\n"):
        m = _LABEL_LINE_RE.match(ln)
        if m:
            flush()
            rest = m.group(2).strip()
            # 纯数字/符号内容不送引擎（"Level: 5" -> 引擎会输出乱码）
            if rest and re.search(r"[A-Za-z\u4e00-\u9fff]", rest):
                rest = engine(rest).strip()
            out.append(f"{GAME_LABELS[m.group(1).lower()]}: {rest}".rstrip())
        else:
            buf.append(ln)
    flush()
    return "\n".join(out)


def _translate_plain(
    text: str,
    appid: str = "",
    secret: str = "",
    from_lang: str = "auto",
    to_lang: str = "zh",
    use_mock: bool = False,
    prefer_offline: bool = True,
) -> str:
    """多级兜底翻译（无标签拆分），供 translate_with_fallback 分段调用。"""
    if not text.strip():
        return ""
    if use_mock:
        return mock_translate(text, to_lang)

    # 离线优先
    if prefer_offline:
        out = argos_translate(text, from_lang, to_lang)
        if out:
            out = _strip_hallucination(out)
            # 多义词消歧（仅 en->zh 有效，需原文配合判断语境）
            if from_lang in ("en", "auto") and to_lang in ("zh", "zt"):
                out = _disambiguate(out, text)
            return out

    # 在线
    if appid and secret:
        out = baidu_translate(text, appid, secret, from_lang, to_lang)
        if out:
            return _strip_hallucination(out)

    # 再尝试离线（用户首次启动时可能还没 init）
    if not prefer_offline:
        out = argos_translate(text, from_lang, to_lang)
        if out:
            out = _strip_hallucination(out)
            if from_lang in ("en", "auto") and to_lang in ("zh", "zt"):
                out = _disambiguate(out, text)
            return out

    # 兜底
    return mock_translate(text, to_lang) + " ⚠"


def translate_with_fallback(
    text: str,
    appid: str = "",
    secret: str = "",
    from_lang: str = "auto",
    to_lang: str = "zh",
    use_mock: bool = False,
    prefer_offline: bool = True,
) -> str:
    """带多级兜底的翻译。绝不返回 None。

    优先级:
        1. Argos 离线 (默认)
        2. 百度 API (有密钥时)
        3. Mock
    """
    if not text.strip():
        return ""
    text = merge_lines_for_translation(text)
    return translate_labelled(
        text,
        engine=lambda t: _translate_plain(
            t, appid, secret, from_lang, to_lang, use_mock, prefer_offline),
    )


def engine_status(deep: bool = True) -> dict:
    """返回各引擎的就绪状态，供 UI 显示。

    deep=False: 只做快速文件检查（适合 GUI 线程调用，不阻塞）。
    deep=True:  额外查询已安装语言对（要拉起 worker 加载模型，
                可能阻塞 10 秒+，只应在后台线程或用户主动打开设置时用）。
    """
    status = {
        "argos_available": False,
        "argos_languages": [],
        "baidu_configured": False,
        "mock_available": True,
        "argos_mode": "none",  # import / subprocess / none
        "argos_pairs": [],  # [["en","zh"], ...] 已安装的语言对
    }
    # 路径 1: 直接 import（GUI 进程跳过：ctranslate2 与 Qt 冲突）
    if not _qt_loaded():
        try:
            from argostranslate import translate as _tr
            langs = _tr.get_installed_languages()
            status["argos_available"] = bool(langs)
            status["argos_languages"] = [(l.code, l.name) for l in langs]
            status["argos_mode"] = "import" if langs else "none"
            status["argos_pairs"] = query_installed_pairs()
        except ImportError:
            pass
        except Exception:
            pass

    # 路径 2: subprocess
    if not status["argos_available"]:
        if _offline_translate_script_exists() and find_python_executable():
            status["argos_available"] = True
            status["argos_mode"] = "subprocess"
            if deep:
                pairs = query_installed_pairs()
                if pairs:
                    status["argos_pairs"] = pairs
                    status["argos_languages"] = sorted({c for p in pairs for c in p})

    return status


_subprocess_pairs_cache: Optional[list] = None


def query_installed_pairs(refresh: bool = False) -> list:
    """查询已安装的翻译语言对 [["en","zh"], ...]。

    - 能直接 import argos 时直接读；
    - 打包 exe 通过 `python offline_translate.py --list` 查询（结果缓存）。
    """
    global _subprocess_pairs_cache
    if _subprocess_pairs_cache is not None and not refresh:
        return _subprocess_pairs_cache

    pairs: list = []
    if not _qt_loaded():
        try:
            from argostranslate import translate as _tr
            for lang in _tr.get_installed_languages():
                for t in getattr(lang, "translations_from", []) or []:
                    tgt = getattr(t, "to_lang", None)
                    if tgt is not None:
                        pairs.append([lang.code, tgt.code])
        except Exception:
            pairs = []

    if not pairs:
        w = _get_worker()
        if w is not None:
            pairs = w.list_pairs()
    if not pairs:
        py = find_python_executable()
        script = _resolve_offline_script()
        if py and script:
            try:
                r = subprocess.run([py, script, "--list"],
                                   capture_output=True, text=True, timeout=60,
                                   creationflags=_NO_WINDOW)
                if r.returncode == 0 and r.stdout.strip():
                    pairs = [p.split("-") for p in r.stdout.strip().splitlines()
                             if "-" in p]
            except Exception as e:
                log.debug("查询子进程语言对失败: %s", e)

    _subprocess_pairs_cache = pairs
    return pairs


def _resolve_offline_script() -> Optional[str]:
    """定位 offline_translate.py。"""
    cands = []
    if getattr(sys, "frozen", False):
        cands.append(Path(sys.executable).parent / "offline_translate.py")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            cands.append(Path(meipass) / "offline_translate.py")
    cands.append(Path(__file__).parent / "offline_translate.py")
    for c in cands:
        if c.exists():
            return str(c)
    return None
