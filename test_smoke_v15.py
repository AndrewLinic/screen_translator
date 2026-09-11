"""v15 冒烟测试：OCR 修复三项

1. 吞空格 + 首字母误大写  -> "Ofyour" / "Onhis" / "Intothe"
2. 句中首字母误大写        -> "your Cock" -> "your cock"
3. 字母误识为数字          -> "c0ck" / "t1ying"（推广到 0/5/2/6/8/9/4/7）

跑法: screentrans 的 python.exe test_smoke_v15.py
"""
import sys
sys.path.insert(0, '.')
import ocr

FAIL = 0
PASS = 0


def check(inp, expected, note=""):
    global FAIL, PASS
    r = ocr._repair_latin_words(inp)
    if r == expected:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {inp!r:34} -> {r!r:34} 期望 {expected!r}  {note}")


# ---- 1. 吞空格 + 首字母误大写 ----
check("Some of them gasped at the sheer size Ofyour Cock, some of them cheer.",
      "Some of them gasped at the sheer size of your cock, some of them cheer.",
      "截图原句")
check("size Ofyour Cock", "size of your cock")
check("back Onhis horse", "back on his horse")
check("go Intothe cave", "go into the cave")
check("on Ofthe hill", "on of the hill")
check("Thegoat ran", "The goat ran", "句首保留大写")

# ---- 2. 句中首字母误大写 ----
check("at the giant Cock", "at the giant cock", "形容词隔开")
check("the sheer Size of it", "the sheer size of it")
check("with your Hand", "with your hand")
check("in his Paws", "in his paws")

# ---- 2b. 专名 / 正常词必须不动 ----
check("Lusterfield is big", "Lusterfield is big")
check("Moonlight shines", "Moonlight shines")
check("Call John now", "Call John now", "前词大写 -> 不动")
check("tell Rose about it", "tell Rose about it", "裸词位 -> 不动")
check("I met Mark yesterday", "I met Mark yesterday")
check("the Sebas", "the Sebas", "词典外专名")
check("Underwater cave", "Underwater cave")

# ---- 3. 字母误识为数字 ----
check("c0ck", "cock")
check("m0re", "more")
check("l0ve", "love")
check("t1ying", "trying")
check("a1so", "also")
check("w1th", "with")
check("h4ve", "have")
check("size Ofy0ur C0ck", "size of your cock", "数字+吞空格叠加")

# ---- 3b. 真型号 / 编号必须不动 ----
check("H1", "H1")
check("160", "160")
check("a1b1c", "a1b1c", "多数字 -> 不动")
check("sha256", "sha256")
check("B2B", "B2B")
check("utf8", "utf8")
check("AK47", "AK47")
check("gr8t", "gr8t", "替换后非词典词 -> 不动")
check("q1xyz", "q1xyz")
check("th5", "th5")

print(f"\n通过 {PASS}/{PASS + FAIL}")
if FAIL:
    sys.exit(1)
