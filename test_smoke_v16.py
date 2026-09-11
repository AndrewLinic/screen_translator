"""v16 冒烟测试: OCR 引擎路由（RapidOCR 子进程 / Windows / 自动择优）+ 修复层新增护栏。

运行: venv python test_smoke_v16.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtWidgets import QApplication  # noqa: E402

app = QApplication([])

PASS = []


def check(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name, extra)


# ============ 1. 修复层: 换行不再让行首词漏修 ============
import ocr  # noqa: E402

check("修复.换行行首", ocr._repair_latin_words("with the\ngoatS out there")
      == "with the\ngoats out there")
check("修复.换行行首数字",
      ocr._repair_latin_words("desperately\nt1Ying to please")
      == "desperately\ntrying to please")
check("修复.词中夹大写", ocr._repair_latin_words("trYing to please")
      == "trying to please")
check("修复.缩写不动", ocr._repair_latin_words("USA today") == "USA today")
check("修复.专名不动", ocr._repair_latin_words("Sebas the McDonald")
      == "Sebas the McDonald")

# ============ 2. 引擎常量与配置 ============
check("引擎常量", ocr.OCR_ENGINES == ("rapidocr", "windows", "auto"))
from config import DEFAULT_CONFIG, load_config  # noqa: E402
check("配置.默认引擎", load_config().get("ocr_engine") in ocr.OCR_ENGINES,
      str(load_config().get("ocr_engine")))
check("配置.默认值存在", "ocr_engine" in DEFAULT_CONFIG)

# ============ 3. RapidOCR 客户端 ============
import ocr_rapid  # noqa: E402

has_py = ocr_rapid.find_python()
check("客户端.找到带rapidocr的Python", bool(has_py), str(has_py))
check("客户端.preflight", ocr_rapid.preflight() == (has_py is not None
                                                    and ocr_rapid._script_path() is not None))
check("客户端.worker脚本存在", ocr_rapid._script_path() is not None)

# ============ 4. 真实截图两引擎对比 ============
from PIL import Image  # noqa: E402

IMG = os.path.join("logs", "feedback", "_screenshot.png")
GOLD = ("Look, courier. Lusterfield is all I've ever known, I don't have "
        "anything to do with the goats out there.")


def cer(ref, hyp):
    ref, hyp = " ".join(ref.split()).lower(), " ".join(hyp.split()).lower()
    d = list(range(len(hyp) + 1))
    for i in range(1, len(ref) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(hyp) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (ref[i - 1] != hyp[j - 1]))
            prev = cur
    return d[len(hyp)] / max(1, len(ref))


if os.path.exists(IMG):
    img = Image.open(IMG).convert("RGB")
    t0 = time.time()
    w = ocr.ocr_image(img, lang="zh-Hans-CN", engine="windows")
    t_w = time.time() - t0
    check("windows引擎.有结果", bool(w.strip()))
    print(f"     windows CER={cer(GOLD, w):.3f} {t_w:.2f}s  {w!r}")

    t0 = time.time()
    r = ocr.ocr_image(img, lang="zh-Hans-CN", engine="rapidocr")
    t_r = time.time() - t0
    ok_r = bool(r.strip())
    check("rapidocr引擎.有结果", ok_r)
    if ok_r:
        print(f"     rapidocr CER={cer(GOLD, r):.3f} {t_r:.2f}s  {r!r}")
        check("rapidocr.精度不劣于windows", cer(GOLD, r) <= cer(GOLD, w) + 1e-9,
              f"{cer(GOLD, r):.3f} vs {cer(GOLD, w):.3f}")
    else:
        print("     [跳过] RapidOCR 不可用（无 Python 依赖）—— 应已回退 Windows")

    a = ocr.ocr_image(img, lang="zh-Hans-CN", engine="auto")
    check("auto引擎.有结果", bool(a.strip()))
    print(f"     auto CER={cer(GOLD, a):.3f} {a!r}")

    bad = ocr.ocr_image(img, lang="zh-Hans-CN", engine="不存在的引擎")
    check("非法引擎名.回退不崩", bool(bad.strip()))
else:
    print("[跳过] 缺少测试截图", IMG)

# ============ 5. 设置界面（含新下拉框） ============
from settings_dialog import SettingsDialog  # noqa: E402

dlg = SettingsDialog()
items = [dlg.ocr_engine_combo.itemData(i)
         for i in range(dlg.ocr_engine_combo.count())]
check("设置.引擎下拉选项", items == ["rapidocr", "windows", "auto"], str(items))
check("设置.引擎当前值", dlg.ocr_engine_combo.currentData()
      == load_config().get("ocr_engine"))

# ============ 6. main 接线（仅导入，不实例化） ============
import main as main_mod  # noqa: E402

check("main.有OCR信号", hasattr(main_mod.ScreenTranslatorApp, "sig_ocr_done"))
check("main.有OCR回调", hasattr(main_mod.ScreenTranslatorApp, "_on_ocr_done"))
check("main.有OCR线程", hasattr(main_mod.ScreenTranslatorApp, "_ocr_loop"))
src = open("main.py", encoding="utf-8").read()
check("main.tick不再同步OCR", "text = ocr_image(img, lang=lang)" not in src)
check("main.传入引擎参数", "self.cfg.get(\"ocr_engine\"" in src)

# ============ 汇总 ============
failed = [n for n, ok in PASS if not ok]
print(f"\n通过 {len(PASS) - len(failed)}/{len(PASS)}")
for n in failed:
    print("  [FAIL]", n)
raise SystemExit(1 if failed else 0)
