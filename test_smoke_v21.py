"""v21 冒烟测试: 多帧投票。

字幕叠在动态场景上时，每帧截图都有细微差异（背景在动、抗锯齿抖动），
OCR 结果随之浮动 —— 同一个词这帧读对、下帧读错。多帧投票 = 把同一画面
连续识别的几帧结果按"词"取多数。

设计要点（本测试逐条验证）:
  A. _vote_texts 纯函数: 多数、并列取首、长度差异大退回最长
  B. 同一画面连续投递 _VOTE_FRAMES(3) 帧，投满即停（不再重复识别）
  C. 第 1 帧就出结果；后续帧投票若修正了内容会触发重译
  D. 画面变化 -> 投票缓冲与计数被重置

运行: venv python test_smoke_v21.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = []


def check(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name, extra)


import main as main_mod  # noqa: E402

V = main_mod._vote_texts

# ============ A. 纯函数 ============
check("投票.全一致", V(["a b c", "a b c", "a b c"]) == "a b c")
check("投票.二对一", V(["a b c", "a x c", "a b c"]) == "a b c",
      repr(V(["a b c", "a x c", "a b c"])))
check("投票.首帧孤立错误被纠正", V(["a b X", "a b c", "a b c"]) == "a b c",
      repr(V(["a b X", "a b c", "a b c"])))
check("投票.单条", V(["a b"]) == "a b")
check("投票.空输入", V([]) == "" and V(None) == "" and V(["", ""]) == "")
check("投票.并列取最早", V(["a b", "a c"]) == "a b", repr(V(["a b", "a c"])))
check("投票.词数差异大取最长", V(["a b c d e f g", "a b"]) == "a b c d e f g",
      repr(V(["a b c d e f g", "a b"])))
check("投票.帧数常量", main_mod._VOTE_FRAMES == 3, f"K={main_mod._VOTE_FRAMES}")

# ============ B~D. 全链路 ============
from PyQt5.QtWidgets import QApplication  # noqa: E402
from PIL import Image  # noqa: E402

app = QApplication([])

SHOT = os.path.join("logs", "feedback", "_screenshot.png")
if not os.path.exists(SHOT):
    print("[跳过] 缺少测试截图", SHOT)
    raise SystemExit(0)
BASE = Image.open(SHOT).convert("RGB")

# OCR 结果序列: 第 1 帧把 fox 误读成 box，后两帧读对 -> 投票应纠正
SEQ = ["the quick brown box", "the quick brown fox", "the quick brown fox"]
_state = {"frame": BASE, "calls": 0}


def fake_grab(_region):
    return _state["frame"]


def fake_ocr(_img, **kw):
    i = min(_state["calls"], len(SEQ) - 1)
    _state["calls"] += 1
    return SEQ[i]


main_mod.grab_region = fake_grab
main_mod.ocr_image = fake_ocr

print("实例化应用（offscreen）...")
t0 = time.time()
w = main_mod.ScreenTranslatorApp()
print(f"  完成 {time.time() - t0:.1f}s")

w.cfg["regions"] = []
w.cfg["region"] = None
w.cfg["engine_mode"] = "mock"     # 不做真翻译，专注投票链路
w._running = True


def pump(seconds=30.0, until=None):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until is None


st = w._region_state[0]

# ---- 第 1 帧 ----
w._tick()
pump(30, lambda: not w._ocr_inflight)
check("B1.第1帧立即出结果", st["text"] == "the quick brown box", repr(st["text"]))
gen1 = st["gen"]

# ---- 第 2 帧（同画面补票） ----
w._tick()
pump(30, lambda: not w._ocr_inflight)
check("B2.同画面补第2帧", _state["calls"] == 2, f"calls={_state['calls']}")

# ---- 第 3 帧 ----
w._tick()
pump(30, lambda: not w._ocr_inflight)
check("B3.同画面补第3帧", _state["calls"] == 3, f"calls={_state['calls']}")
check("C1.投票纠正了误识", st["text"] == "the quick brown fox", repr(st["text"]))
check("C2.修正触发重译", st["gen"] > gen1, f"gen {gen1} -> {st['gen']}")

# ---- 投满即停 ----
w._tick()
time.sleep(0.25)
app.processEvents()
check("B4.投满即停（不再识别）", _state["calls"] == 3, f"calls={_state['calls']}")
check("B5.投满时不置 inflight", w._ocr_inflight is False)

# ---- 画面变化 -> 重置投票会话 ----
altered = BASE.copy()
altered.paste((255, 255, 255), (0, 40, 160, 70))
_state["frame"] = altered
_state["calls"] = 0
w._tick()
pump(30, lambda: not w._ocr_inflight)
check("D1.换画面重新识别", _state["calls"] >= 1, f"calls={_state['calls']}")
check("D2.新画面投票计数重置", st["vote_tries"] <= 1, f"tries={st['vote_tries']}")

# ---- 重扫也要清投票缓冲 ----
st["vote_tries"] = 99
w._rescan()
time.sleep(0.3)
app.processEvents()
check("D3.重扫清投票计数", st["vote_tries"] <= 1, f"tries={st['vote_tries']}")
pump(30, lambda: not w._ocr_inflight)

w._running = False
w._timer.stop()

# ============ 汇总 ============
failed = [n for n, ok in PASS if not ok]
print(f"\n通过 {len(PASS) - len(failed)}/{len(PASS)}")
for n in failed:
    print("  [FAIL]", n)

try:
    from translator import shutdown_worker
    shutdown_worker()
except Exception:
    pass
raise SystemExit(1 if failed else 0)
