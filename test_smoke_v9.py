"""P4 回归: OCR 吞空格还原（"ofthe"/"intothe"/"withthe" 这类相邻词被合并的形态）。
以及与上一个 P3 用例（撇号合并 + 尾字母大写）联动，确保合并链条不冲突。

bug: 用户反馈的英文原文是 "Yes, I was never a part of the tribe in the west.
     My ancestors were one of the few goats that settled back here."
 但 OCR 把 "of the" 之间的空格吃了，吐出 "ofthe"。同时把 "goats" 的 's'
 误判成大写 S（goatS）和顿号 '、' 替代英文逗号。
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import ocr

r = lambda s: ocr._repair_latin_words(ocr._normalize_latin(s))

cases = [
    # 用户截图反映的两个真实案例（OCR 把 "," 误识成中文顿号 "、"，
# 但仍然带原来那一个空格位；最终还原是 "one of the , goats"）
    ("Yes, I was never a part ofthe tribe in the west. My ancestors were one ofthe 、goatS that settled back",
     "Yes, I was never a part of the tribe in the west. My ancestors were one of the , goats that settled back"),
    # 双闭集拆（按"短左闭集"优先）
    ("ofthe", "of the"),
    ("intothe", "into the"),   # 词典优先: into + the (而非 in + to the)
    ("tothe", "to the"),
    ("the", "the"),           # 不切（单闭集已经在 [5,16] 之外）
    ("thetribe", "the tribe"),
    ("thelittle", "the little"),
    ("oneofthe", "one of the"),
    ("ofthe intothe tothe", "of the into the to the"),
    # 新增的高频闭集介词
    ("withthe", "with the"),
    ("fromthe", "from the"),
    ("forhis", "for his"),
    ("onhis", "on his"),
    ("withthe fromthe forhis", "with the from the for his"),
    # 标点处理
    ("goatS,that", "goats, that"),
    ("goatS, that", "goats, that"),
    ("1,500 dollars", "1,500 dollars"),
    ("3.14 is pi", "3.14 is pi"),
    # bug 1: 撇号合并
    ("don t have anything", "don't have anything"),
    ("I m doing it.", "I'm doing it."),
    ("We ve been here.", "We've been here."),
    ("We re not alone.", "We're not alone."),
    ("can t stop.", "can't stop."),
    ("won t come.", "won't come."),
    # bug 2: 尾字母大写还原
    ("the goatS out there.", "the goats out there."),
    ("basicS are easy.", "basics are easy."),
    ("basicS course.", "basics course."),
    ("BasiCS course.", "Basics course."),
    # 保护: 专名/缩写不动
    ("McDonald is here.", "McDonald is here."),
    ("McCree joined.", "McCree joined."),
    ("macOS uses it.", "macOS uses it."),
    ("GOATS for sale.", "GOATS for sale."),
    ("GoatS are friendly.", "Goats are friendly."),
    # 真实完整句
    ("Look, courier. Lusterfield is all l ve ever known, I don t have anything to do with the goatS out there.",
     "Look, courier. Lusterfield is all l've ever known, I don't have anything to do with the goats out there."),
    ("Lusterfield is one ofthe few goats", "Lusterfield is one of the few goats"),
    # 不应拆分（保护）
    ("meaning menace zenith method", "meaning menace zenith method"),
    ("the Lusterfield is unknown", "the Lusterfield is unknown"),
    ("Lusterfield is good", "Lusterfield is good"),
    ("PLEASEhelp", "PLEASEhelp"),
    # 综合歧义
    ("have m fun", "have m fun"),                  # "have" + "m": m 后没有合法缩写词
    ("go t the store", "go t the store"),          # "go" + "t": not in APOS_PREFIXES
    ("goatS,that", "goats, that"),
]

passed = 0
failed = []
for inp, expect in cases:
    out = r(inp)
    if out == expect:
        passed += 1
    else:
        failed.append((inp, expect, out))
print(f"PASSED {passed}/{len(cases)}")
for inp, expect, out in failed:
    print(f"  FAIL  in={inp!r}")
    print(f"    expect={expect!r}")
    print(f"    out   ={out!r}")
if failed:
    sys.exit(1)
print("OK")
sys.stdout.flush()
