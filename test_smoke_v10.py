"""P5 回归: 4 字符吞空格还原（用户截图真实场景 ofmy / onme / inmy）+ 修长护栏。

bug: OCR 把相邻词之间的空格给吃了时，最常见的是 5+ 字符的复合形态
（ofthe / intothe / oneofthe），护栏设到 len≥5 完全够用。但截图显示
实际场景里也出现 4 字符的吞空格（ofmy / onme / inmy / byus / ofme），
原因：闭集词(2字符) + 短代词(2字符) 的组合原本就被 OCR 吐成 4 字符。
护栏漏判，导致翻译拿到 "each ofmy finger" 这种原文。

修法:
- 把长度下限从 5 降到 4
- 4 字符形态强制走双闭集路径（防 over→o ver / hand→h and）
- _SWALLOWED_BACK 加入人称代词 + 缩写后缀（us/me/him/her/its/you/she）
- 双闭集检测时 back 也算合法 rest（让 we+ve 也能进入还原链，最终由 3.5 合并为 we've）
- 循环开头加"末段已合法"早退出（避免 ofmy→of+my 切第一段后循环空切）
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import ocr

r_fn = lambda s: ocr._repair_latin_words(ocr._normalize_latin(s))

cases = [
    # === 用户截图真实场景（paws 段）===
    # "ofmy" = OCR 把 "of my" 之间的空格吃了
    ("each ofmy finger", "each of my finger"),
    ("each ofmy finger gripping into a hard surface.",
     "each of my finger gripping into a hard surface."),
    # 4 字符吞空格的其它形态（必须切）
    ("ofmy", "of my"),
    ("byus", "by us"),
    ("ofme", "of me"),
    ("onme", "on me"),
    ("inmy", "in my"),
    ("onmy", "on my"),
    # === 不切（词典里有合法词：onit/toit/ofus/tomy 都是 ecdict 收录的少见缩写/术语）===
    ("tomy", "tomy"),
    ("init", "init"),
    ("onit", "onit"),
    ("toit", "toit"),
    ("ofus", "ofus"),
    # === 4 字符合法词不能误切 ===
    ("over", "over"),
    ("only", "only"),
    ("onto", "onto"),
    ("also", "also"),
    ("into", "into"),
    ("have", "have"),
    ("been", "been"),
    ("them", "them"),
    ("each", "each"),
    ("with", "with"),
    ("from", "from"),
    ("long", "long"),
    ("hand", "hand"),
    ("need", "need"),
    ("told", "told"),
    ("kind", "kind"),
    ("best", "best"),
    # === 之前的吞空格还原不能退化 ===
    ("ofthe", "of the"),
    ("tothe", "to the"),
    ("intothe", "into the"),
    ("onhis", "on his"),
    ("oneofthe", "one of the"),
    ("ofthe goatS", "of the goats"),
    # === 撇号合并 3.5 期望（仅词间空隙形态：OCR 把撇号当空格）===
    # 3.5 不处理连写（weve / Ive 这种应当由 3.7 切分后再交给 3.5，
    # 但 OCR 实际很少把 we've 直接读成 weve，所以允许保持原样）
    ("we ve got", "we've got"),
    ("I m going home", "I'm going home"),
    ("don t have anything", "don't have anything"),
    # === 整句测试（用户原句）===
    ("Lusterfield is all l ve ever known, I don t have anything to do with the goatS out there.",
     "Lusterfield is all l've ever known, I don't have anything to do with the goats out there."),
    ("Yes, I was never a part ofthe tribe. My ancestors were one ofthe few goats that settled back.",
     "Yes, I was never a part of the tribe. My ancestors were one of the few goats that settled back."),
    # 截图2原句里的 ofmy
    ("The strength comes from my paws, you see, the rough pads here are tough and rubbery, it's like suction cups on each ofmy finger gripping into a hard surface.",
     "The strength comes from my paws, you see, the rough pads here are tough and rubbery, it's like suction cups on each of my finger gripping into a hard surface."),
    # 专名 / 缩写 不能误切
    ("McDonald", "McDonald"),
    ("macOS", "macOS"),
    ("GOATS", "GOATS"),
    ("Lusterfield is unknown", "Lusterfield is unknown"),
]

passed = 0
failed = []
for inp, expect in cases:
    out = r_fn(inp)
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