"""下载并安装 Argos 离线翻译模型

用法:
    python install_models.py            # 安装默认语言集 (ja, ko)
    python install_models.py ja ko fr   # 安装指定语言 (双向: xx<->en)
    python install_models.py --list     # 列出索引中所有可用语言对
    python install_models.py --all-core # 安装全部核心语言

模型安装到 assets/argos_models/ (程序通过 ARGOS_PACKAGES_DIR 直接读取,
不做二次拷贝, 节省磁盘)。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import zipfile
from pathlib import Path

import requests

ROOT = Path(__file__).parent
MODELS_DIR = ROOT / "assets" / "argos_models"
CACHE_DIR = ROOT / "assets" / "argos_cache"
INDEX_URLS = [
    "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json",
    "https://gh-proxy.com/https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json",
    "https://cdn.jsdelivr.net/gh/argosopentech/argospm-index@main/index.json",
]
LOCAL_INDEX = ROOT / "argos_index.json"

# 语言代码 -> 中文名 (用于显示)
LANG_NAMES = {
    "zh": "中文", "en": "英语", "ja": "日语", "ko": "韩语",
    "fr": "法语", "de": "德语", "es": "西班牙语", "ru": "俄语",
    "it": "意大利语", "pt": "葡萄牙语", "th": "泰语", "vi": "越南语",
    "ar": "阿拉伯语", "nl": "荷兰语", "pl": "波兰语", "tr": "土耳其语",
    "id": "印尼语", "hi": "印地语", "sv": "瑞典语", "uk": "乌克兰语",
    "zt": "繁体中文",
}
# 默认核心语言 (除中英外)
DEFAULT_LANGS = ["ja", "ko", "zt"]


def _hr(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def load_index(force: bool = False) -> list[dict]:
    """读取包索引，本地没有则联网下载。"""
    if LOCAL_INDEX.exists() and not force:
        try:
            return json.loads(LOCAL_INDEX.read_text(encoding="utf-8"))
        except Exception:
            pass
    last_err = None
    for url in INDEX_URLS:
        try:
            print(f"  获取索引: {url}")
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            data = r.json()
            LOCAL_INDEX.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            print(f"  ✓ 索引已保存 ({len(data)} 个包)")
            return data
        except Exception as e:
            last_err = e
            print(f"  ✗ 失败: {type(e).__name__}")
    if last_err:
        raise SystemExit(f"[错误] 无法获取索引: {last_err}")
    return []


def find_package(index: list[dict], code: str) -> dict | None:
    return next((p for p in index if p.get("code") == code), None)


def download(url: str, dst: Path) -> bool:
    """流式下载，支持断点续传。"""
    if dst.exists():
        print(f"  [已缓存] {dst.name} ({_hr(dst.stat().st_size)})")
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    resume = tmp.stat().st_size if tmp.exists() else 0
    headers = {"Range": f"bytes={resume}-"} if resume else {}
    try:
        r = requests.get(url, stream=True, timeout=120, headers=headers)
        if r.status_code == 416:  # 已完整
            tmp.rename(dst)
            return True
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0)) + resume
        got = resume
        t0 = time.time()
        mode = "ab" if resume else "wb"
        with open(tmp, mode) as f:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                got += len(chunk)
                speed = got / max(0.5, time.time() - t0) / 1024 / 1024
                print(f"  {got * 100 / max(1, total):5.1f}%  {_hr(got)}/{_hr(total)}  {speed:.1f}MB/s",
                      end="\r", flush=True)
        print()
        tmp.rename(dst)
        return True
    except Exception as e:
        print(f"\n  ✗ 下载失败: {type(e).__name__}: {e}")
        return False


def install_package(pkg_path: Path, dest_dir: Path) -> bool:
    """解压 .argosmodel (zip) 到模型目录。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = pkg_path.stem  # translate-ja_en-1_1
    # 归一化成 argos 期望的目录名: translate-ja_en-1_1
    target = dest_dir / name
    if target.exists():
        if (target / "metadata.json").exists():
            print(f"  [已安装] {name}")
            return True
        # 上次解压不完整/层级错误 -> 重来
        shutil.rmtree(target, ignore_errors=True)
    tmp = dest_dir / (name + "_tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        with zipfile.ZipFile(pkg_path) as z:
            z.extractall(tmp)
        # 有些包在最外层还套了一层目录，找到真正的模型根（含 metadata.json）
        meta = sorted(tmp.rglob("metadata.json"))
        if not meta:
            raise RuntimeError("包内没有 metadata.json")
        root = meta[0].parent
        shutil.move(str(root), str(target))
        print(f"  ✓ 安装 {name} -> {target}")
        return True
    except Exception as e:
        print(f"  ✗ 解压失败: {e}")
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="安装 Argos 离线翻译模型")
    ap.add_argument("langs", nargs="*", help="语言代码，如 ja ko fr（自动装双向）")
    ap.add_argument("--list", action="store_true", help="列出可用语言对")
    ap.add_argument("--all-core", action="store_true", help="安装全部核心语言")
    ap.add_argument("--force", action="store_true", help="强制重新下载")
    args = ap.parse_args()

    index = load_index()

    if args.list:
        print(f"\n可用语言对 (共 {len(index)}):")
        for p in sorted(index, key=lambda x: x.get("code", "")):
            fc, tc = p["from_code"], p["to_code"]
            print(f"  {p['code']:24} {LANG_NAMES.get(fc, fc):8} -> {LANG_NAMES.get(tc, tc)}")
        return

    if args.all_core:
        langs = ["ja", "ko", "fr", "de", "es", "ru", "it", "pt", "th", "vi"]
    elif args.langs:
        langs = args.langs
    else:
        langs = DEFAULT_LANGS

    targets = []
    for lang in langs:
        for code in (f"translate-{lang}_en", f"translate-en_{lang}"):
            pkg = find_package(index, code)
            if pkg:
                targets.append(pkg)
            else:
                print(f"  ! 索引中没有 {code}")

    if not targets:
        print("[错误] 没有可安装的包")
        return

    total_mb = 0
    for p in targets:
        print(f"  待装: {p['code']}  {LANG_NAMES.get(p['from_code'], p['from_code'])} -> "
              f"{LANG_NAMES.get(p['to_code'], p['to_code'])}")

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ok = 0
    for p in targets:
        url = p["links"][0]
        fname = url.split("/")[-1]
        dst = CACHE_DIR / fname
        print(f"\n[{targets.index(p) + 1}/{len(targets)}] {p['code']}")
        if not download(url, dst):
            continue
        if install_package(dst, MODELS_DIR):
            ok += 1

    print("\n" + "=" * 50)
    print(f"完成: {ok}/{len(targets)} 个模型")
    print(f"模型目录: {MODELS_DIR}")
    installed = sorted(x.name for x in MODELS_DIR.glob("translate-*"))
    print(f"当前已装 ({len(installed)}): {installed}")


if __name__ == "__main__":
    main()
