"""v12: 数字 1 误识还原（r/l/i -> 1）
截图场景: "trying" 被 Windows OCR 读成 "t1Ying"（r 误识为 1，且
中间 Y 因前后紧邻误识字符被误判大写）。词典里 0 个含 1 的纯字母词，
所以"字母 1 字母"形态几乎都是误识。
- 作用域: token 前后都是字母、长度 >= 4
- 替换候选优先级 r > l > i
- 替换后是合法英文词才采用；都不命中则不动
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ocr

CASES = [
    # === 截图场景 ===
    ("the t1Ying to please", "the trying to please"),
    # === 应修复：r -> 1 误识 ===
    ("t1Ying", "trying"),
    ("t1ying", "trying"),
    ("r1ng", "ring"),
    ("a1so", "also"),
    ("w1th", "with"),
    ("g1ve", "give"),
    ("f1ght", "fight"),
    ("w1se", "wise"),
    ("r1ght", "right"),
    ("h1gh", "high"),
    ("b1rd", "bird"),
    ("f1rst", "first"),
    ("wo1k", "work"),
    ("wo1d", "word"),
    ("ye1l", "yell"),
    ("p1ck", "pick"),
    ("s1ck", "sick"),
    ("ca1e", "care"),
    ("ma1n", "main"),
    ("sp1t", "spit"),
    ("sp1n", "spin"),
    ("sp1ll", "spill"),
    ("sha1e", "share"),
    # === 整句 ===
    ("I am t1Ying to wo1k", "I am trying to work"),
    # === 不应动：长度 < 4（可能是 H1/A1 等真型号） ===
    ("a1", "a1"),
    ("H1", "H1"),
    # === 不应动：纯数字 ===
    ("160", "160"),
    ("2024", "2024"),
    # === 不应动：句首独立 1 ===
    ("1n", "1n"),
    ("1s", "1s"),
    # === 不应动：多 1 的保守场景 ===
    ("a1b1c", "a1b1c"),
    # === 不应动：词典里无候选 ===
    ("q1xyz", "q1xyz"),
    # === 不应动：句中正常词里含 1（如 M1A1 主战坦克，但这是缩写）
    # 词典里没收录，OCR 不会误识出这种，所以这种情况实际罕见 ===
    # === 之前已修过的不要破坏 ===
    ("like its nothing", "like its nothing"),
    ("ofmy finger", "of my finger"),
    ("the goatS", "the goats"),
    ("I m going home", "I'm going home"),
]

ok = 0
fail = 0
for inp, exp in CASES:
    r = ocr._repair_latin_words(inp)
    if r == exp:
        ok += 1
    else:
        fail += 1
        print(f"  [FAIL] {inp!r:28} -> {r!r:28}  期望 {exp!r}")

print(f"\n通过 {ok}/{ok+fail}")
sys.exit(0 if fail == 0 else 1)
