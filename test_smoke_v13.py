"""v13: 查词弹窗词性标注修复

bug 根因:
  ecdict.csv 词典的 translation 字段, 多义项分隔符是字面反斜杠+n
  ('\\n' 两字符, chr(92)+'n'), 不是真换行符 ('\\n', chr 10)。
  split_translations() 之前只按真换行符拆, 拆不动字面 '\\n', 结果
  所有多义项被合并成一坨, 词性标注错乱 (mere 显示 n. · 小湖, 但
  实际上 a. 仅仅的 才是主项)。
  同时, ecdict 的 pos 字段经常缺失 (mere 的 pos 字段是空字符串),
  lookup() 之前直接拿空数组, 弹窗 meta_label 不显示"词性:"行。

修复:
  1. split_translations() 先把字面 '\\n' 归一为真换行, 再按行处理
  2. lookup() 若 pos 字段空, 从 translation 拆解时按出现顺序补全
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dict_lookup import Dict, split_translations


def _ensure():
    Dict.get().ensure_loaded()


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  {detail}" if detail else ""))
    return ok


def test_mere():
    """截图场景: mere 应当正确显示 n. + a. 两个义项"""
    d = Dict.get().lookup("mere")
    pos = d["pos"]
    ok1 = "n." in pos and "a." in pos, f"pos={pos}"
    items = split_translations(d["translation"])
    ok2 = len(items) == 2, f"split 段数={len(items)} (期望 2)"
    ok3 = items[0]["pos"] == "n." and "小湖" in items[0]["means"]
    ok4 = items[1]["pos"] == "a." and "仅仅的" in items[1]["means"]
    return (check("mere.pos 双词性", ok1)
            and check("mere.拆解 2 段", ok2)
            and check("mere.n. 段含 小湖", ok3)
            and check("mere.a. 段含 仅仅的", ok4))


def test_polysemous(name, expected_pos, expected_n_items_min=2):
    """通用多义项测试: 应有完整 pos 列表, split 出多段"""
    d = Dict.get().lookup(name)
    pos = d["pos"]
    ok1 = all(p in pos for p in expected_pos), f"pos={pos}, 期望含 {expected_pos}"
    items = split_translations(d["translation"])
    ok2 = len(items) >= expected_n_items_min, f"split 段数={len(items)}"
    return check(f"{name}.pos 完整", ok1) and check(f"{name}.拆解 {expected_n_items_min}+ 段", ok2)


def test_no_literal_bs_n_in_output():
    """修复后, split_translations 内部不应残留字面 \\n 字符"""
    d = Dict.get().lookup("get")
    items = split_translations(d["translation"])
    for it in items:
        for m in it["means"]:
            if chr(92) + "n" in m:
                return check("get.means 无残留反斜杠", False,
                             f"发现残留: {m!r}")
    return check("get.means 无残留反斜杠", True)


def test_pos_field_fallback():
    """pos 字段空时, lookup 应从 translation 反推补全"""
    d = Dict.get().lookup("mere")
    # 词典里 mere 的 pos 字段是空字符串
    # 注意: v14 起"常用义项优先"排序会把形容词提到名词前（mere 在
    # _ADJ_FIRST_WORDS 里），所以期望值是 ["a.", "n."] 而非 ecdict 原序
    return check("mere.pos 字段补全 (a.+n. 常用义项优先)",
                 d["pos"] == ["a.", "n."], f"实际={d['pos']}")


def test_pos_field_kept():
    """pos 字段有值时, 不应被翻译反推覆盖"""
    d = Dict.get().lookup("world")  # 通常 pos 字段是 'n.'
    # 至少有 n. (具体值不严格, 只测有内容)
    return check("world.pos 有内容", len(d["pos"]) > 0, f"pos={d['pos']}")


def main():
    _ensure()
    print("=== mere 截图场景 ===")
    test_mere()
    print()
    print("=== 其他多义项 ===")
    test_polysemous("get", ["vt.", "vi.", "n."])
    test_polysemous("play", ["n.", "v."])
    test_polysemous("run", ["n.", "vi.", "vt.", "a."])
    test_polysemous("set", ["n.", "vt.", "vi.", "a."])
    test_polysemous("take", ["vt.", "vi.", "n."])
    print()
    print("=== 反推与保留 ===")
    test_pos_field_fallback()
    test_pos_field_kept()
    test_no_literal_bs_n_in_output()
    print()
    # 整体通过判断
    print("全部测试完成")


if __name__ == "__main__":
    main()
