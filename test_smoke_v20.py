"""v20 冒烟测试: 词频仲裁（ecdict bnc/frq） + 专名表护栏。

背景（v19 遗留的已知歧义）:
  "1" 可能被读成 r/l/i，候选 thrs/thls/this 在 ecdict 里都是词，
  按 _DIGIT_FIX 的候选顺序会先命中 thrs（垃圾词）。实测比较过 6 种
  顺序都在噪声内 —— 单靠调顺序治不了。

  加入词频仲裁后: "候选中有排名者优先、排名小者优先"。
  ecdict 的 bnc/frq 列（数字越小越常用）能把正确词和垃圾词干净分开:
    this 23/20 vs thrs 0/0        wait 463/400 vs wart 15233/15251
    time 50/52 vs tlme 0/0        from 27/26 vs flom 0/0

专名表:
  第 5 步"句中首字母误大写还原"的回看层把介词也算作触发条件
  （换来 at Night -> at night 的收益），代价是 "with Mark" 会被误改成
  "with mark"。专名表（人名/地名/月份星期/品牌）把这类词保护起来。
  表里刻意排除 will/may/can 等情态助动词，避免挡住必要的修复。

运行: venv python test_smoke_v20.py
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

# ============ 0. 词库确实加载 ============
freq = ocr._load_wordfreq()
names = ocr._load_proper_names()
check("词库.词频表非空", len(freq) > 40000, f"words={len(freq)}")
check("词库.专名表非空", len(names) > 5000, f"names={len(names)}")
check("词库.this有排名", freq.get("this") is not None, f"rank={freq.get('this')}")
check("词库.thrs无排名", freq.get("thrs") is None, f"rank={freq.get('thrs')}")

# ============ 1. 词频仲裁: 候选歧义 ============
VOTE = [
    ("th1s", "this"),            # vs thrs（无排名）
    ("th1s w0rld", "this world"),
    ("wa1t", "wait"),            # vs wart(15251)
    ("t1me", "time"),            # vs tlme（无排名）
    ("fr0m", "from"),
    ("th1ng", "thing"),
    ("some P01nt", "some point"),
    ("d0nT w0rry", "don't worry"),
]
for src, want in VOTE:
    got = R(src)
    check(f"仲裁.{src!r}", got == want, f"-> {got!r}")

# ============ 2. 专名表: 保护句中大写专名 ============
# 这些都是"介词/限定词 + 大写实词"，回看层会触发；靠专名表兜住
PROPER = [
    ("with Mark", "with Mark"),
    ("of Rose", "of Rose"),
    ("in Paris", "in Paris"),
    ("from Mary", "from Mary"),
    ("at London", "at London"),
    ("with Jack", "with Jack"),
]
for src, want in PROPER:
    got = R(src)
    check(f"专名.{src!r}", got == want, f"-> {got!r}")

# ============ 3. 专名表不能破坏原有的"误大写还原" ============
# 这些词的普通义项远多于专名义项，必须照旧还原
STILL_FIX = [
    ("at Night", "at night"),
    ("of Course", "of course"),
    ("the Size", "the size"),
    ("your Cock", "your cock"),
    ("the sheer Size", "the sheer size"),
    ("with patrons WhO", "with patrons who"),
]
for src, want in STILL_FIX:
    got = R(src)
    check(f"仍还原.{src!r}", got == want, f"-> {got!r}")

# ============ 4. 情态助动词不许进专名表 ============
for w in ("will", "may", "can", "would", "should", "must"):
    check(f"专名表不含助动词.{w}", w not in names)

# ============ 5. 多数字还原（v19 回归） ============
REG = [
    ("back to work at some P01nt.", "back to work at some point."),
    ("P01nt taken", "Point taken"),
    ("c0ck", "cock"), ("t1Ying", "trying"), ("a1so", "also"),
]
for src, want in REG:
    got = R(src)
    check(f"回归.{src!r}", got == want, f"-> {got!r}")

# ============ 6. 型号/编号护栏（v19 回归，不能被仲裁破坏） ============
CODES = ["sha256 hash", "H2SO4 acid", "B2B sales", "A1 steak", "MP3 file",
         "C3PO droid", "R2D2 unit", "UTF-8 text", "3D model", "x86 cpu",
         "1st place", "F16 jet", "160 gold"]
for c in CODES:
    got = R(c)
    check(f"护栏.{c!r}", got == c, f"-> {got!r}")

# ============ 7. 换行合并 + 仲裁协同 ============
J = ocr._join_visual_lines
multi = ("Cute as it is to watch ye flounder all embarrassed like, I do got\n"
         "to get back to work at some P01nt.")
check("协同.合并后仲裁", R(J(multi)).endswith("at some point."),
      repr(R(J(multi))[-26:]))

# ============ 汇总 ============
failed = [n for n, ok in PASS if not ok]
print(f"\n通过 {len(PASS) - len(failed)}/{len(PASS)}")
for n in failed:
    print("  [FAIL]", n)
raise SystemExit(1 if failed else 0)
