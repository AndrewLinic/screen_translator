"""v18 冒烟测试: 大小写误识还原（WhO/ThE/缩写）+ 视觉换行合并（不再强制分段）。

两类问题:
  1. 词尾/词中被 OCR 抬成大写的字母没还原（"patrons WhO" 原样输出），
     以及 "donT" 被吞空格逻辑切碎成 "do nt"。
  2. OCR 的换行来自屏幕自动折行，原样保留会让 argostranslate **按行独立
     翻译**（它内部按 "\\n" 切分），译文被强行分段、支离破碎。

运行: venv python test_smoke_v18.py
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
J = ocr._join_visual_lines

# ============ 1. 大小写误识还原 ============
CASES = [
    # 截图实测: "patrons WhO" —— 词尾字母被抬高判成大写
    ("patrons WhO", "patrons who"),
    # 句首保留大写（首字母大小写原样保留，两种场景都不出错）
    ("WhO are you", "Who are you"),
    ("ThE door is open", "The door is open"),
    ("goatS out there", "goats out there"),
    ("basicS of life", "basics of life"),
    # 缩写尾部误大写: 词典里只有带撇号形态，必须靠 _APOS_T_STEMS 判
    ("donT do it", "don't do it"),
    ("canT come", "can't come"),
    ("wonT go", "won't go"),
    ("isnT it", "isn't it"),
    ("doesnT work", "doesn't work"),
    # 不能被吞空格逻辑切碎（曾产出 "donT" -> "do nt"）
    ("donT", "don't"),
]
for src, want in CASES:
    got = R(src)
    check(f"还原.{src!r}", got == want, f"-> {got!r}")

# 护栏: 专名/缩略语/大小写正常的词一个都不能动
GUARDS = [
    "Call John now", "Rose is here", "Sebas the McDonald",
    "Lusterfield is all I've ever known", "USA today", "IT works fine",
    "May I come in", "I will go", "Who are you",
]
for src in GUARDS:
    got = R(src)
    check(f"护栏.{src[:22]!r}", got == src, f"-> {got!r}")

# ============ 2. 视觉换行合并 ============
# 2a. 整句折行 -> 合成一行
SUB = ("It means yer gonna put that body to use and spend some quality time with\n"
       "patrons WhO\n"
       "have ya ass reserved... for their enjoyment.")
joined = J(SUB)
check("合并.整句折行无换行", "\n" not in joined)
check("合并.内容完整",
      joined == "It means yer gonna put that body to use and spend some "
                "quality time with patrons WhO have ya ass reserved... "
                "for their enjoyment.",
      repr(joined))

# 2b. 先合并再修复: 修复链能看到完整句子，WhO -> who
check("顺序.合并后修复得到 who",
      R(joined).endswith("with patrons who have ya ass reserved... "
                         "for their enjoyment."),
      repr(R(joined)))

# 2c. 真分段要保留
check("保留.句末标点后换行", J("I went home.\nShe stayed.") == "I went home.\nShe stayed.")
check("保留.标签行各自成行",
      J("Reward: 100 gold\nObjective: kill 3 goats")
      == "Reward: 100 gold\nObjective: kill 3 goats")
check("保留.列表项各自成行",
      J("- find the key\n- open the door") == "- find the key\n- open the door")
check("合并.句中折行", J("Deals 25 damage to all\nenemies in a 3 meter radius.")
      == "Deals 25 damage to all enemies in a 3 meter radius.")

# 2d. 中日文合并不补空格（中文本来不用空格）
check("CJK.中文不补空格", J("你好世界\n欢迎回来") == "你好世界欢迎回来")
check("CJK.日文不补空格", J("これはとても重要な\n話です。") == "これはとても重要な話です。")
check("CJK.中英混排补空格", J("中文和 English\n混排的句子") == "中文和 English 混排的句子")

# ============ 3. 标签行拆分不受合并影响 ============
import translator  # noqa: E402

calls = []


def fake_engine(t):
    calls.append(t)
    return "T(" + t + ")"


out = translator.translate_labelled("Reward: 100 gold\nObjective: kill 3 goats",
                                    fake_engine)
check("标签.仍能拆出中文标签",
      out.startswith("奖励: ") and "目标: " in out, repr(out))
check("标签.标签行不进引擎", not any("Reward" in c for c in calls), str(calls))

# 整句段落应**一次**交给引擎（而不是按行拆成多次调用）
calls.clear()
translator.translate_labelled("line one here\nline two there", fake_engine)
check("段落.整体一次送引擎", len(calls) == 1, str(calls))

# ============ 4. 真实截图端到端 ============
from PIL import Image  # noqa: E402

IMG = os.path.join("logs", "feedback", "_screenshot.png")
if os.path.exists(IMG) and ocr.ocr_image is not None:
    img = Image.open(IMG).convert("RGB")
    for eng in ("windows", "rapidocr"):
        t = ocr.ocr_image(img, lang="zh-Hans-CN", engine=eng)
        if not t.strip():
            print(f"  [跳过] {eng} 无结果")
            continue
        check(f"端到端.{eng}无换行", "\n" not in t, repr(t[:60]))
        check(f"端到端.{eng}专名保留", "Lusterfield" in t, repr(t[:80]))
        print(f"     {eng}: {t!r}")

# ============ 汇总 ============
failed = [n for n, ok in PASS if not ok]
print(f"\n通过 {len(PASS) - len(failed)}/{len(PASS)}")
for n in failed:
    print("  [FAIL]", n)
raise SystemExit(1 if failed else 0)
