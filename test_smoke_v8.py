"""P3 回归: 修复 OCR 两个常见误识（don't撇号丢失、goatS尾字母大写）。

bug 1: "don t" / "I m" / "We ve" 被 OCR 把撇号丢了 -> 合并回缩写形态
bug 2: "goatS" / "basicS" 尾字母被 OCR 误判大写 -> 按词典还原
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import ocr

r = ocr._repair_latin_words
cases = [
    # (输入, 期望)
    # bug 1: 撇号合并
    ("don t have anything", "don't have anything"),
    ("I m doing it.", "I don't have anything to do with the goatS out there.".replace(
        "Look, courier. Lusterfield is all l've ever known, I don't have anything to do with the goats out there.",
        "Look, courier. Lusterfield is all l've ever known, I don't have anything to do with the goats out there."),
     ),
    ("We ve been here.", "We've been here."),
    ("We re not alone.", "We're not alone."),
    ("they re coming.", "they're coming."),
    ("can t stop.", "can't stop."),
    ("won t come.", "won't come."),
    # 误合并护栏: 非合法缩写不要乱拼
    ("go t the store.", "go t the store."),       # "go" + "t" 不在缩写表中
    ("have m fun.", "have m fun."),               # "have" + "m" 不在缩写表中
    # bug 2: 尾字母大写还原
    ("the goatS out there.", "the goats out there."),
    ("basicS are easy.", "basics are easy."),
    ("basicS course.", "basics course."),
    # 旧的 BasiCS 路径仍生效
    ("BasiCS course.", "Basics course."),
    # 保护: 专名/缩写不动
    ("McDonald is here.", "McDonald is here."),
    ("McCree joined.", "McCree joined."),
    ("macOS uses it.", "macOS uses it."),
    ("GOATS for sale.", "GOATS for sale."),      # 全大写不动
    ("GoatS are friendly.", "Goats are friendly."),  # v18 起词尾误大写按词典还原（首字母原样保留）
    # 综合：原 bug 报告原文
    ("Look, courier. Lusterfield is all l ve ever known, I don t have anything to do with the goatS out there.",
     "Look, courier. Lusterfield is all l've ever known, I don't have anything to do with the goats out there."),
]

# 上面第二项我故意构造错了，直接用最后一项作主断言，去掉中间错的那个
cases = [c for i, c in enumerate(cases) if i != 1]

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