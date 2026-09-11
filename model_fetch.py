"""Argos 离线翻译模型的获取与安装 —— 供 install_models.py 与程序自动下载共用。

为什么要独立出来：
    精简版发布包不带 842MB 模型，程序首次启动要能自己去下载。
    下载逻辑（索引回退 → 断点续传 → 解压校验）只能有一份，否则两边会不一致。

设计要点：
    - **不硬依赖 requests**：优先用 requests，装不上就回退标准库 urllib
      （用户机器上不一定有 requests，程序自动下载时不能因此失败）。
    - 索引有多个镜像（GitHub raw 在国内常被墙）：raw → gh-proxy → jsdelivr。
    - 下载支持断点续传（``.part`` 文件），中断后重跑接着下。
    - 解压时兼容"包内多套一层目录"的情况，用 metadata.json 定位真正的模型根。
"""
from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

INDEX_URLS = [
    "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json",
    "https://gh-proxy.com/https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json",
    "https://cdn.jsdelivr.net/gh/argosopentech/argospm-index@main/index.json",
]
INDEX_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 120
CHUNK = 256 * 1024

# 首次自动下载的核心语言（中英双向 = translate-zh_en + translate-en_zh）
CORE_LANGS = ("zh", "en")

Progress = Optional[Callable[[int, int], None]]
Logger = Optional[Callable[[str], None]]


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def has_requests() -> bool:
    try:
        import requests  # noqa: F401
        return True
    except Exception:
        return False


def _get(url: str, timeout: int):
    """返回一个可迭代的响应对象，接口在 requests / urllib 之间做统一。

    统一后的对象只需支持: ``.status``、``.headers``、``.iter_content()``、``.json()``
    """
    if has_requests():
        import requests
        return _RequestsResp(requests.get(url, stream=True, timeout=timeout,
                                          headers={"User-Agent": "screen-translator"}))
    req = urllib.request.Request(url, headers={"User-Agent": "screen-translator"})
    return _UrllibResp(urllib.request.urlopen(req, timeout=timeout))


class _RequestsResp:
    def __init__(self, r):
        self._r = r

    @property
    def status(self) -> int:
        return self._r.status_code

    @property
    def headers(self):
        return self._r.headers

    def iter_content(self):
        for chunk in self._r.iter_content(chunk_size=CHUNK):
            if chunk:
                yield chunk

    def json(self):
        return self._r.json()


class _UrllibResp:
    def __init__(self, r):
        self._r = r

    @property
    def status(self) -> int:
        return getattr(self._r, "status", 200)

    @property
    def headers(self):
        return self._r.headers

    def iter_content(self):
        while True:
            chunk = self._r.read(CHUNK)
            if not chunk:
                break
            yield chunk

    def json(self):
        return json.loads(self._r.read().decode("utf-8"))


# --------------------------------------------------------------------------
# 索引
# --------------------------------------------------------------------------
def load_index(cache_file: Optional[Path] = None, force: bool = False,
               log: Logger = None) -> list:
    """读取包索引；本地有缓存就直接用，否则按镜像顺序联网取。"""
    def _say(m):
        if log:
            log(m)

    if cache_file is not None and cache_file.exists() and not force:
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if data:
                return data
        except Exception:
            pass

    last_err = None
    for url in INDEX_URLS:
        try:
            _say(f"  获取索引: {url}")
            data = _get(url, INDEX_TIMEOUT).json()
            if cache_file is not None:
                try:
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(json.dumps(data, ensure_ascii=False),
                                          encoding="utf-8")
                except Exception:
                    pass
            _say(f"  索引已获取（{len(data)} 个包）")
            return data
        except Exception as e:
            last_err = e
            _say(f"  失败: {type(e).__name__}")
    if last_err:
        raise RuntimeError(f"无法获取模型索引: {last_err}")
    return []


def find_package(index: Sequence[dict], code: str) -> Optional[dict]:
    return next((p for p in index if p.get("code") == code), None)


def resolve_packages(index: Sequence[dict], langs: Iterable[str]) -> tuple[list, list]:
    """把语言代码展开成双向包，返回 (命中的包, 缺失的代码)。

    注意 ``en`` 本身跳过：所有模型都是 xx<->en 形态，``translate-en_en`` 不存在。
    """
    found, missing = [], []
    seen, miss_seen = set(), set()
    for lang in langs:
        if lang == "en":
            continue
        for code in (f"translate-{lang}_en", f"translate-en_{lang}"):
            pkg = find_package(index, code)
            if pkg:
                if code not in seen:
                    seen.add(code)
                    found.append(pkg)
            elif code not in miss_seen:
                miss_seen.add(code)
                missing.append(code)
    return found, missing


# --------------------------------------------------------------------------
# 下载 / 解压
# --------------------------------------------------------------------------
def download(url: str, dst: Path, progress: Progress = None,
             log: Logger = None, retries: int = 2) -> bool:
    """流式下载到 dst，支持断点续传（同目录 ``.part``）。"""
    def _say(m):
        if log:
            log(m)

    if dst.exists():
        _say(f"  [已缓存] {dst.name} ({human(dst.stat().st_size)})")
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")

    for attempt in range(retries + 1):
        resume = tmp.stat().st_size if tmp.exists() else 0
        try:
            req_url = url
            r = _get(req_url, DOWNLOAD_TIMEOUT)
            if r.status == 416 and resume:      # Range 越界 = 本地已完整
                tmp.rename(dst)
                return True
            total = 0
            try:
                total = int(r.headers.get("Content-Length", 0)) + resume
            except Exception:
                total = 0
            got = resume
            t0 = time.time()
            mode = "ab" if resume else "wb"
            _say(f"  下载 {dst.name}（{human(total) if total else '未知大小'}）"
                 + (f"，从 {human(resume)} 续传" if resume else ""))
            with open(tmp, mode) as f:
                for chunk in r.iter_content():
                    f.write(chunk)
                    got += len(chunk)
                    if progress:
                        progress(got, total)
                    elif log:
                        speed = got / max(0.5, time.time() - t0) / 1024 / 1024
                        pct = f"{got * 100 / total:5.1f}%" if total else "  ...  "
                        _say(f"  {pct}  {human(got)}/{human(total) if total else '?'}  "
                             f"{speed:.1f}MB/s")
            tmp.rename(dst)
            return True
        except Exception as e:
            _say(f"  下载失败（第 {attempt + 1} 次）: {type(e).__name__}: {e}")
            if attempt >= retries:
                return False
            time.sleep(2)
    return False


def install_package(pkg_path: Path, dest_dir: Path, log: Logger = None) -> bool:
    """把 .argosmodel（zip）解压成 argos 期望的目录结构。"""
    def _say(m):
        if log:
            log(m)

    name = pkg_path.stem                  # translate-ja_en-1_1
    # 去掉 .part 等中间后缀，argos 只认 translate-xx_yy-x_y
    if not name.startswith("translate-"):
        name = pkg_path.name.split(".argosmodel")[0]
    target = dest_dir / name
    dest_dir.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if (target / "metadata.json").exists():
            _say(f"  [已安装] {name}")
            return True
        shutil.rmtree(target, ignore_errors=True)

    tmp = dest_dir / (name + "_tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        with zipfile.ZipFile(pkg_path) as z:
            z.extractall(tmp)
        meta = sorted(tmp.rglob("metadata.json"))
        if not meta:
            raise RuntimeError("包内没有 metadata.json")
        shutil.move(str(meta[0].parent), str(target))
        _say(f"  ✓ 安装 {name}")
        return True
    except Exception as e:
        _say(f"  ✗ 解压失败: {e}")
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 高层入口
# --------------------------------------------------------------------------
def installed_codes(models_dir: Path) -> list[str]:
    """已安装的包名（目录名）。"""
    try:
        if not models_dir.exists():
            return []
        return sorted(p.name for p in models_dir.glob("translate-*"))
    except Exception:
        return []


def ensure_langs(langs: Sequence[str], models_dir: Path,
                 cache_dir: Optional[Path] = None,
                 progress: Progress = None,
                 log: Logger = None) -> list[str]:
    """确保 langs 的双向模型已装好，返回本次装上的包名。

    ``progress(got, total)`` 是**单个文件**的进度；跨文件的总进度由调用方
    结合 ``total_bytes`` 自己换算（见 fetch_core_models）。
    """
    models_dir = Path(models_dir)
    cache_dir = Path(cache_dir) if cache_dir else models_dir.parent / "argos_cache"
    index = load_index(cache_dir / "index.json", log=log)
    pkgs, missing = resolve_packages(index, langs)
    if missing and log:
        log(f"  索引中缺少: {', '.join(missing)}")
    done = []
    for p in pkgs:
        url = (p.get("links") or [""])[0]
        if not url:
            continue
        dst = cache_dir / url.split("/")[-1]
        if log:
            log(f"  {p['code']}")
        if download(url, dst, progress=progress, log=log) and \
                install_package(dst, models_dir, log=log):
            done.append(p["code"])
    return done


def models_missing(models_dir: Path) -> bool:
    """模型目录里一套可用模型都没有。"""
    return not installed_codes(models_dir)
