#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""出发布包：把 dist/屏幕翻译 打成可直接上传 GitHub Releases 的 zip。

为什么不在仓库里放 exe：
    完整目录 1.1GB（其中离线模型 842MB），既超 GitHub 单文件 100MB 限制，
    也不是给人 git clone 用的。改为 Release 附件 + 程序首次启动自动下载模型。

用法:
    python make_release.py                 # 精简版（不含模型，约 210MB）★推荐
    python make_release.py --tag v1.0.0    # 版本号写进包名
    python make_release.py --full          # 连模型一起打（约 900MB）
    python make_release.py --out D:/out    # 指定输出目录（默认 release/）
    python make_release.py --dry-run       # 只统计，不写 zip

出包后:
    打开 https://github.com/<user>/<repo>/releases/new
    -> 填 tag -> 把 zip 拖进 "Attach binaries" -> Publish release
"""
from __future__ import annotations

import argparse
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "dist" / "屏幕翻译"
DEFAULT_OUT = ROOT / "release"

# 精简版排除：离线模型（842MB）+ 下载缓存 + 各种运行产物
SLIM_EXCLUDE_DIRS = {"argos_models", "argos_cache", "__pycache__",
                     "logs", "data", ".workbuddy"}
SLIM_EXCLUDE_NAMES = {".synced.json"}
FULL_EXCLUDE_DIRS = {"argos_cache", "__pycache__", "logs", "data", ".workbuddy"}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def collect(src: Path, exclude_dirs: set) -> list[Path]:
    """列出要打包的文件（相对路径），排除指定目录。"""
    files = []
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        parts = set(rel.parts)
        if parts & exclude_dirs:
            continue
        if rel.name in SLIM_EXCLUDE_NAMES:
            continue
        files.append(rel)
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description="打发布包（GitHub Releases 用）")
    ap.add_argument("--tag", default="", help="版本号，如 v1.0.0（写进包名）")
    ap.add_argument("--full", action="store_true", help="含离线模型（约 900MB）")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    ap.add_argument("--dry-run", action="store_true", help="只统计不打包")
    args = ap.parse_args()

    if not SRC.exists():
        print(f"[错误] 找不到 {SRC}，请先运行 python deploy.py 出包")
        return 1

    exclude = FULL_EXCLUDE_DIRS if args.full else SLIM_EXCLUDE_DIRS
    kind = "完整版" if args.full else "精简版"
    files = collect(SRC, exclude)
    if not files:
        print("[错误] 没有可打包的文件")
        return 1

    total = sum((SRC / f).stat().st_size for f in files)
    print(f"来源   : {SRC}")
    print(f"类型   : {kind}（{'含' if args.full else '不含'}离线模型）")
    print(f"文件数 : {len(files)}")
    print(f"原始体积: {human(total)}")

    missing = [n for n in ("屏幕翻译.exe", "_internal", "offline_translate.py",
                           "model_fetch.py", "pylocator.py", "pyenv.json")
               if not (SRC / n).exists()]
    if missing:
        print(f"[警告] dist 里缺少: {', '.join(missing)}"
              f"（运行 python deploy.py 可自动补齐随附脚本）")

    if args.dry_run:
        print("\n--dry-run：未写 zip")
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"-{args.tag}" if args.tag else ""
    zip_path = out_dir / f"屏幕翻译-{kind}{tag}.zip"

    t0 = time.time()
    print(f"\n打包中 -> {zip_path}")
    done_bytes = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for i, rel in enumerate(files, 1):
            full = SRC / rel
            z.write(full, rel.as_posix())
            done_bytes += full.stat().st_size
            if i % 200 == 0 or i == len(files):
                print(f"  {i}/{len(files)}  {human(done_bytes)}"
                      f"  {time.time() - t0:.0f}s", end="\r", flush=True)
    print()
    print(f"完成: {zip_path}")
    print(f"压缩后: {human(zip_path.stat().st_size)}"
          f"（压缩率 {zip_path.stat().st_size * 100 / max(1, total):.0f}%），"
          f"耗时 {time.time() - t0:.0f}s")
    print("\n上传到 GitHub Releases:")
    print("  https://github.com/AndrewLinic/screen_translator/releases/new")
    print(f"  tag: {args.tag or 'v1.0.0'}  附件: {zip_path.name}")
    if not args.full:
        print("\n提示: 精简版不含模型，首次启动会自动下载中英双向（约 165MB），")
        print("      日/韩/繁等语言可用 install_models.py 追加。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
