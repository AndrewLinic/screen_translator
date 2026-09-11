"""离线资源下载脚本

下载:
    - Argos Translate 中英互译模型 (~150MB)
    - ECDICT 英汉词典 (~65MB)

用法:
    python download_assets.py            # 下载所有缺失的资源
    python download_assets.py argos      # 只下载翻译模型
    python download_assets.py ecdict     # 只下载词典
    python download_assets.py --force    # 强制重新下载
"""
import argparse
import os
import shutil
import sys
import time
from pathlib import Path

ASSETS = Path(__file__).parent / "assets"


def _hr(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def download_file(url: str, dst: Path, force: bool = False) -> bool:
    """流式下载，进度条输出。"""
    import requests
    if dst.exists() and not force:
        print(f"  [已存在] {dst.name} ({_hr(dst.stat().st_size)})")
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"  下载 {url}")
    print(f"  -> {dst}")
    try:
        r = requests.get(url, stream=True, timeout=180)
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        got = 0
        t0 = time.time()
        with open(dst, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                got += len(chunk)
                if total and got % (1024 * 1024) < 64 * 1024:
                    pct = got * 100 / total
                    speed = got / max(0.1, time.time() - t0) / 1024
                    print(f"  {pct:5.1f}%  {_hr(got)} / {_hr(total)}  {speed:.0f} KB/s", end="\r", flush=True)
        print(f"\n  完成: {_hr(dst.stat().st_size)}")
        return True
    except Exception as e:
        print(f"\n  下载失败: {e}")
        if dst.exists():
            dst.unlink()
        return False


def download_argos(force: bool = False) -> bool:
    """下载 Argos 中英互译模型。"""
    print("\n[1/2] Argos Translate 模型")
    print("-" * 50)
    # 先用 argos 包系统下载（最稳）
    try:
        from argostranslate import package as pkg
        pkg.update_package_index()
        available = pkg.get_available_packages()
        need = [p for p in available if (
            (p.from_code == "zh" and p.to_code == "en")
            or (p.from_code == "en" and p.to_code == "zh")
        )]
        if not need:
            print("  ! 没找到可用模型（网络可能受限）")
            return False

        for p in need:
            tag = f"{p.from_name} -> {p.to_name}"
            print(f"\n  {tag}")
            cache = Path.home() / ".local" / "cache" / "argos-translate" / "downloads"
            fname = f"translate-{p.from_code}_{p.to_code}.argosmodel"
            cached = cache / fname
            if cached.exists() and not force:
                print(f"  [已下载] {cached} ({_hr(cached.stat().st_size)})")
            else:
                print(f"  下载中...")
                path = p.download()
                print(f"  -> {path} ({_hr(Path(path).stat().st_size)})")
            # 安装
            install_path = cached if cached.exists() else path
            print(f"  安装到 Argos 数据目录...")
            pkg.install_from_path(str(install_path))
            print(f"  ✓ {tag} 安装完成")
        return True
    except Exception as e:
        print(f"  ✗ Argos 模型下载/安装失败: {e}")
        print(f"  提示: Argos 模型来自 https://www.argosopentech.com/argospm/index/")
        print(f"        可手动下载后放到 ~/.local/share/argos-translate/packages/")
        return False


def download_ecdict(force: bool = False) -> bool:
    """下载 ECDICT 离线英汉词典。"""
    print("\n[2/2] ECDICT 英汉词典")
    print("-" * 50)
    target = ASSETS / "ecdict.csv"
    if target.exists() and not force:
        print(f"  [已存在] {target.name} ({_hr(target.stat().st_size)})")
        return True
    url = "https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv"
    return download_file(url, target, force=force)


def download_mini(force: bool = False) -> bool:
    """下载迷你版词典（仅 4KB，用于演示）。"""
    print("\n[附] ECDICT 迷你示例")
    print("-" * 50)
    target = ASSETS / "ecdict.mini.csv"
    if target.exists() and not force:
        print(f"  [已存在] {target.name}")
        return True
    url = "https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.mini.csv"
    return download_file(url, target, force=force)


def verify() -> bool:
    """验证资源完整性。"""
    print("\n[验证] 资源完整性")
    print("-" * 50)
    ok = True

    # Argos
    try:
        from argostranslate import translate as _tr
        langs = _tr.get_installed_languages()
        codes = [l.code for l in langs]
        if "zh" in codes and "en" in codes:
            print(f"  ✓ Argos: 已安装 {[(l.code, l.name) for l in langs]}")
        else:
            print(f"  ✗ Argos: 缺少语言 (现有: {codes})")
            ok = False
    except Exception as e:
        print(f"  ✗ Argos: {e}")
        ok = False

    # ECDICT
    target = ASSETS / "ecdict.csv"
    if target.exists() and target.stat().st_size > 1_000_000:
        print(f"  ✓ ECDICT: {target.name} ({_hr(target.stat().st_size)})")
    else:
        print(f"  ✗ ECDICT: 缺失或太小")
        ok = False

    return ok


def main():
    ap = argparse.ArgumentParser(description="下载屏幕翻译的离线资源")
    ap.add_argument("which", nargs="*", default=["argos", "ecdict"],
                    help="要下载的资源: argos / ecdict / all")
    ap.add_argument("--force", action="store_true", help="强制重新下载")
    args = ap.parse_args()

    ASSETS.mkdir(parents=True, exist_ok=True)
    targets = set()
    for w in args.which:
        if w in ("all", "*"):
            targets.update(["argos", "ecdict"])
        else:
            targets.add(w)

    print("屏幕翻译 - 离线资源下载")
    print("=" * 50)
    print(f"目标目录: {ASSETS}")

    if "argos" in targets:
        download_argos(args.force)
    if "ecdict" in targets:
        download_ecdict(args.force)
        download_mini(args.force)

    print()
    verify()
    print()
    print("完成。重新启动主程序即可使用新资源。")


if __name__ == "__main__":
    main()
