"""下载并安装 Argos 离线翻译模型（命令行入口；下载逻辑见 model_fetch.py）

用法:
    python install_models.py            # 安装默认语言集 (ja, ko, zt)
    python install_models.py ja ko fr   # 安装指定语言 (双向: xx<->en)
    python install_models.py --core     # 只装中英双向（精简版首次使用）
    python install_models.py --list     # 列出索引中所有可用语言对
    python install_models.py --all-core # 安装全部核心语言
    python install_models.py --force    # 忽略本地索引缓存，强制刷新

模型安装到 assets/argos_models/（程序通过 ARGOS_PACKAGES_DIR 直接读取，
不做二次拷贝，节省磁盘）。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import model_fetch as mf

ROOT = Path(__file__).parent
MODELS_DIR = ROOT / "assets" / "argos_models"
CACHE_DIR = ROOT / "assets" / "argos_cache"
INDEX_CACHE = ROOT / "argos_index.json"

# 语言代码 -> 中文名 (用于显示)
LANG_NAMES = {
    "zh": "中文", "en": "英语", "ja": "日语", "ko": "韩语",
    "fr": "法语", "de": "德语", "es": "西班牙语", "ru": "俄语",
    "it": "意大利语", "pt": "葡萄牙语", "th": "泰语", "vi": "越南语",
    "ar": "阿拉伯语", "nl": "荷兰语", "pl": "波兰语", "tr": "土耳其语",
    "id": "印尼语", "hi": "印地语", "sv": "瑞典语", "uk": "乌克兰语",
    "zt": "繁体中文",
}
DEFAULT_LANGS = ["ja", "ko", "zt"]
ALL_CORE_LANGS = ["zh", "en", "ja", "ko", "zt", "fr", "de", "es", "ru",
                  "it", "pt", "th", "vi"]


def _progress(got: int, total: int) -> None:
    pct = f"{got * 100 / total:5.1f}%" if total else "  ...  "
    print(f"  {pct}  {mf.human(got)}/{mf.human(total) if total else '?'}",
          end="\r", flush=True)


def _say(msg: str) -> None:
    print(msg, flush=True)


def main():
    ap = argparse.ArgumentParser(description="安装 Argos 离线翻译模型")
    ap.add_argument("langs", nargs="*", help="语言代码，如 ja ko fr（自动装双向）")
    ap.add_argument("--list", action="store_true", help="列出可用语言对")
    ap.add_argument("--core", action="store_true", help="只装中英双向（约 165MB）")
    ap.add_argument("--all-core", action="store_true", help="安装全部核心语言")
    ap.add_argument("--force", action="store_true", help="强制重新拉取索引")
    args = ap.parse_args()

    index = mf.load_index(INDEX_CACHE, force=args.force, log=_say)

    if args.list:
        print(f"\n可用语言对 (共 {len(index)}):")
        for p in sorted(index, key=lambda x: x.get("code", "")):
            fc, tc = p["from_code"], p["to_code"]
            print(f"  {p['code']:24} {LANG_NAMES.get(fc, fc):8} -> {LANG_NAMES.get(tc, tc)}")
        return

    if args.core:
        langs = list(mf.CORE_LANGS)
    elif args.all_core:
        langs = ALL_CORE_LANGS
    elif args.langs:
        langs = args.langs
    else:
        langs = DEFAULT_LANGS

    targets, missing = mf.resolve_packages(index, langs)
    for code in missing:
        print(f"  ! 索引中没有 {code}")
    if not targets:
        print("[错误] 没有可安装的包")
        return

    for p in targets:
        print(f"  待装: {p['code']}  {LANG_NAMES.get(p['from_code'], p['from_code'])} -> "
              f"{LANG_NAMES.get(p['to_code'], p['to_code'])}")

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ok = []
    for i, p in enumerate(targets, 1):
        url = (p.get("links") or [""])[0]
        if not url:
            continue
        dst = CACHE_DIR / url.split("/")[-1]
        print(f"\n[{i}/{len(targets)}] {p['code']}")
        if mf.download(url, dst, progress=_progress, log=_say) and \
                mf.install_package(dst, MODELS_DIR, log=_say):
            ok.append(p["code"])
        print()

    print("=" * 50)
    print(f"完成: {len(ok)}/{len(targets)} 个模型")
    print(f"模型目录: {MODELS_DIR}")
    installed = mf.installed_codes(MODELS_DIR)
    print(f"当前已装 ({len(installed)}): {installed}")


if __name__ == "__main__":
    main()
