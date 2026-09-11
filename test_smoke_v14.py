"""v14: 词性排序（常用义项优先）

背景:
  ecdict 的 pos/translation 几乎总是"名词在前"，与该词常用义项不符
  （run/get 主要是动词、mere 主要是形容词，却都排成 n. 在前）。
  离线词典没有逐词性词频，故用一张常见多义词的"主词性顺序"表；
  表外词保持词典原序（不动）。

验证:
  1. lookup() 返回的 pos 列表按期望顺序
  2. 弹窗正文 split_translations 出来的义项，按 pos_order_index 排序后
     与 pos 列表顺序一致
  3. 表外词原序不变
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dict_lookup import Dict, split_translations, pos_order_index


def _ensure():
    Dict.get().ensure_loaded()


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    return cond


def body_order(word):
    """模拟弹窗正文：拆义项并按 pos_order_index 稳定排序，返回顺序 pos 列表。"""
    d = Dict.get().lookup(word)
    if not d:
        return []
    all_trans = d.get("all_translations") or [d["translation"]]
    seen, gathered, seq = set(), [], 0
    for trans in all_trans:
        if not trans or trans in seen:
            continue
        seen.add(trans)
        for item in split_translations(trans):
            gathered.append((pos_order_index(word, item["pos"]), seq, item))
            seq += 1
    gathered.sort(key=lambda x: (x[0], x[1]))
    return [it["pos"] for _, _, it in gathered]


def test_word(word, expected_pos):
    d = Dict.get().lookup(word)
    pos = d["pos"]
    ok1 = check(f"{word}.pos = {expected_pos}", pos == expected_pos, f"实际 {pos}")
    # 正文里出现的 pos（去空）应与 pos 列表前缀一致
    body = [p for p in body_order(word) if p]
    # body 里 pos 顺序应为 pos 列表顺序的子序列（同组内 vt./vi. 排序可能不同）
    ok2 = check(f"{word}.正文顺序与 pos 一致", body[:len(pos)] == pos,
                f"正文 {body[:len(pos)]}")
    return ok1 and ok2


def main():
    _ensure()
    print("=== 截图场景 ===")
    test_word("mere", ["a.", "n."])
    print()
    print("=== 动词为主（动词应在前）===")
    test_word("run", ["vi.", "vt.", "n.", "a."])
    test_word("get", ["vt.", "vi.", "n."])
    test_word("take", ["vt.", "vi.", "n."])
    test_word("charge", ["vt.", "vi.", "n."])
    test_word("settle", ["vt.", "vi.", "n."])
    test_word("open", ["vt.", "vi.", "a.", "n."])
    test_word("show", ["vt.", "vi.", "n."])
    print()
    print("=== 形容词为主 ===")
    test_word("free", ["a.", "vt.", "adv."])
    test_word("right", ["a.", "n.", "vt.", "vi.", "adv."])
    test_word("present", ["a.", "n.", "vt.", "vi."])
    print()
    print("=== 名词为主：保持原序（不动）===")
    test_word("water", ["n.", "vt.", "vi.", "a."])
    test_word("book", ["n.", "v."])
    test_word("table", ["n.", "vt."])
    test_word("light", ["n.", "a.", "vt.", "vi.", "adv."])
    print()
    print("=== 表外词：原序不变 ===")
    d1 = Dict.get().lookup("apple")
    check("apple 单词性", d1["pos"] == ["n."], f"{d1['pos']}")
    d2 = Dict.get().lookup("beautiful")
    check("beautiful 单词性", d2["pos"] == ["a."], f"{d2['pos']}")
    print()
    print("全部测试执行完成")


if __name__ == "__main__":
    main()
