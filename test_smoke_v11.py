"""P6 回归: 多义词上下文消歧（翻译后处理层 _disambiguate）。

bug: Argos en->zh 是固定权重神经翻译，对多义词输出"最高频义项"，无法
根据上下文消歧。实测系统性错译（无论语境都选错）的义项:
    charge 的"冲锋"义 -> 电荷/充电/收费/指控/控罪/电话
    settle 的"定居"义 -> 结算/解决
    spring 的"春天"义 -> 弹簧（孤立时）
    bark  的"树皮"义 -> 剥皮

修法: 翻译后处理。每条规则 (原文触发正则, 译文错译正则, 正确替换)，
只在【原文触发 + 译文错译】双重条件同时满足才替换，宁缺毋滥。
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")
from translator import _disambiguate

cases = [
    # ===== charge 的"冲锋"义 =====
    # lead/led the charge
    ("lead the charge", "引导电荷", "带头冲锋"),
    ("led the charge", "主控", "带头冲锋"),
    ("led the charge", "控罪", "带头冲锋"),
    ("led the charge", "指控", "带头冲锋"),
    # the charge began
    ("the charge began", "开始充电", "冲锋"),
    ("the charge began", "指控", "冲锋"),
    # charge forward / into / toward
    ("charge forward", "前期收费", "冲锋"),
    ("charge into battle", "开始战斗", "冲锋"),
    ("charge toward the enemy", "充电", "冲锋"),
    # a charge of energy
    ("a charge of energy", "能源费", "一股能量"),
    ("a charge of energy", "电荷", "一股能量"),

    # ===== settle 的"定居"义 =====
    ("settled in the valley", "已结算", "定居"),
    ("settle down here", "下来", "定居"),
    ("settled back here", "结算", "定居"),

    # ===== spring 的"春天"义 =====
    ("in spring", "弹簧", "春天"),

    # ===== bark 的"树皮"义 =====
    ("the bark of the tree", "剥皮", "树皮"),

    # ===== 不应替换（原文没触发 / 译文没错译）=====
    # take charge / in charge / free of charge：原文不匹配"lead the charge"
    ("take charge of the team", "掌管队伍", "掌管队伍"),
    ("in charge of the project", "负责这个项目", "负责这个项目"),
    ("free of charge", "免费", "免费"),
    # criminal charge：原文不匹配冲锋模式
    ("criminal charges were filed", "提出了刑事指控", "提出了刑事指控"),
    # electric charge：物理电荷，原文不匹配
    ("an electric charge", "电费", "电费"),
    # settle dispute：原文不匹配地点搭配（"settle the dispute"）
    ("settle the dispute", "解决争端", "解决争端"),
    # the dust settled：原文不匹配
    ("the dust settled", "尘埃落定", "尘埃落定"),
    # spring coil：弹簧，原文有 coil 排除
    ("spring coil", "弹簧圈", "弹簧圈"),
    # dog barked：原文不匹配 "the bark of the tree"
    ("the dog barked", "狗叫声", "狗叫声"),
]

passed = 0
failed = []
for en, zh, expect in cases:
    out = _disambiguate(zh, en)
    if out == expect:
        passed += 1
    else:
        failed.append((en, zh, expect, out))
print(f"PASSED {passed}/{len(cases)}")
for en, zh, expect, out in failed:
    print(f"  FAIL  en={en!r} zh={zh!r}")
    print(f"    expect={expect!r}")
    print(f"    out   ={out!r}")
if failed:
    sys.exit(1)
print("OK")
sys.stdout.flush()
