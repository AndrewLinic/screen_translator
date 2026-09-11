"""v19 冒烟测试: 字母误识为数字的**多数字组合还原**。

问题（用户截图）: "back to work at some P01nt." —— OCR 同时把 "o" 和 "i"
读成数字，得到两个数字的 token。旧的 2.5 步硬性要求"只含 1 个数字"
（`len(digs) != 1` 直接跳过），于是 P01nt 原样进了翻译。

修法: 数字个数放宽到 1~3，把每个数字按 _DIGIT_FIX 换成候选字母后
**组合枚举**（P01nt 需要 0->o 且 1->i 同时成立），
  第一优先级 = 某个组合直接是词典词；
  第二优先级 = 某个组合呈"吞空格形态"（交给 3.7 继续拆）；
都不命中则原样不动（保守，不误伤真实型号/编号）。

运行: venv python test_smoke_v19.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

PASS = []


def check(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name, extra)


import ocr  # noqa: E402

R = ocr._repair_latin_words

# ============ 1. 多数字误识还原 ============
# 句中（前有限定词 some/the）应还原成小写 point
CASES = [
    # 用户截图原句
    ("back to work at some P01nt.",
     "back to work at some point."),
    ("at some p01nt", "at some point"),
    ("the P01nt of it", "the point of it"),
    ("there is no P01nt", "there is no point"),
    # 句首保留首字母大写
    ("P01nt taken", "Point taken"),
    # 其它多数字组合
    ("d0nT w0rry", "don't worry"),
    ("P01nt!", "Point!"),
]
for src, want in CASES:
    got = R(src)
    check(f"多数字.{src!r}", got == want, f"-> {got!r}")

# 已知歧义（**既有行为，非本次引入**）: "1" 同时可以是 l/i/r, 而 ecdict 里
# thrs/wart/tlme 也都是词，按候选顺序 (r,l,i) 会先命中错的那个。
# 实测比较过 6 种顺序（95 个真实误识样本）: rli 81、lri 82、lir 84，
# 但 lir 会把更常用的 from 改成 flom、trying 依赖 r 位 —— 净收益在噪声内，
# 故保持原序。彻底解决需要词频仲裁（ecdict 是字母序，没有免费词频）。
got = R("th1s w0rld")
check("已知歧义.th1s仍被还原（不是原样数字）", "1" not in got, f"-> {got!r}")
check("已知歧义.w0rld正确", got.endswith("world"), f"-> {got!r}")

# ============ 2. 单数字路径不能退化（回归） ============
SINGLE = [
    ("c0ck", "cock"),
    ("t1Ying", "trying"),
    ("a1so", "also"),
    ("w0rk", "work"),
    ("th1ng", "thing"),
    ("some P0int", "some point"),
    ("some Po1nt", "some point"),
]
for src, want in SINGLE:
    got = R(src)
    check(f"单数字.{src!r}", got == want, f"-> {got!r}")

# ============ 3. 护栏: 型号/编号/纯数字一律不动 ============
CODES = [
    "sha256 hash", "H2SO4 acid", "B2B sales", "A1 steak", "MP3 file",
    "ISO 9001", "B12 vitamin", "C3PO droid", "R2D2 unit", "COVID-19 case",
    "UTF-8 text", "3D model", "x86 cpu", "mp4 video", "1st place",
    "version 2.0", "AK47 rifle", "USB3 port", "W3C spec", "K2 mountain",
    "F16 jet", "M16 rifle", "ID4 movie", "160 gold",
]
for c in CODES:
    got = R(c)
    check(f"护栏.{c!r}", got == c, f"-> {got!r}")

# ============ 4. 与大小写/换行步骤协同 ============
J = ocr._join_visual_lines
multi = ("Cute as it is to watch ye flounder all embarrassed like, I do got\n"
         "to get back to work at some P01nt.")
joined = J(multi)
check("协同.折行先合并", "\n" not in joined, repr(joined[:40]))
check("协同.合并后修数字", R(joined).endswith("at some point."), repr(R(joined)[-30:]))

# ============ 5. 真实截图端到端 ============
from PIL import Image  # noqa: E402

IMG = os.path.join("logs", "feedback", "_point.png")
if os.path.exists(IMG):
    im = Image.open(IMG).convert("RGB")
    crop = im.crop((0, int(im.height * 0.10), im.width, int(im.height * 0.48)))
    for eng in ("rapidocr", "windows"):
        t = ocr.ocr_image(crop, lang="zh-Hans-CN", engine=eng)
        if not t.strip():
            print(f"  [跳过] {eng} 无结果")
            continue
        print(f"     {eng}: {t!r}")
        check(f"端到端.{eng}无换行", "\n" not in t)
        if eng == "rapidocr":   # 默认引擎: Windows 小字整体质量差，不做硬断言
            check("端到端.rapidocr含some point", "some point" in t.lower(), repr(t[-40:]))
else:
    print(f"  [跳过] 缺少 {IMG}")

# ============ 汇总 ============
failed = [n for n, ok in PASS if not ok]
print(f"\n通过 {len(PASS) - len(failed)}/{len(PASS)}")
for n in failed:
    print("  [FAIL]", n)
raise SystemExit(1 if failed else 0)
