"""v17 冒烟测试: OCR 异步链路真正跑通（修 sig_ocr_done 未接线导致的"识别一次就停摆"）。

v16 只断言了 `hasattr(app, "_on_ocr_done")`，所以漏掉了"信号声明了但没 connect"
这类致命接线遗漏 —— 后果是 `_ocr_inflight` 永远停在 True，tick 从第二轮起全被
自己挡住，表现为**画面识别一次后再也不动、翻译永远不出来**。

本测试实例化真实应用（offscreen）并驱动一次 tick，验证:
  A. tick -> OCR 线程 -> sig_ocr_done -> _on_ocr_done -> 投翻译队列 全链路连通
  B. 画面逐像素没变时最多补票 K 次就停（帧内容去重 + 多帧投票，v21 起）
  C. 手动重扫会清掉帧指纹，强制重新识别
  D. 看门狗能把卡死的 inflight 复位

运行: venv python test_smoke_v17.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtWidgets import QApplication  # noqa: E402
from PIL import Image  # noqa: E402

app = QApplication([])

PASS = []


def check(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name, extra)


SHOT = os.path.join("logs", "feedback", "_screenshot.png")
if not os.path.exists(SHOT):
    print("[跳过] 缺少测试截图", SHOT)
    raise SystemExit(0)
BASE_IMG = Image.open(SHOT).convert("RGB")

import main as main_mod  # noqa: E402

# ---- 打桩: grab_region 返回可控图片; ocr_image 计数 ----
_state = {"frame": BASE_IMG, "ocr_calls": 0}
_real_ocr_image = main_mod.ocr_image


def fake_grab(_region):
    return _state["frame"]


def counting_ocr(img, **kw):
    _state["ocr_calls"] += 1
    return _real_ocr_image(img, **kw)


main_mod.grab_region = fake_grab
main_mod.ocr_image = counting_ocr

print("实例化应用（offscreen，首次会加载词典/预热模型）...")
t0 = time.time()
w = main_mod.ScreenTranslatorApp()
print(f"  实例化完成 {time.time() - t0:.1f}s")

# 单区域、全屏（grab_region 已被打桩，不会真的抓屏）；用 Windows 引擎保证确定性
w.cfg["regions"] = []
w.cfg["region"] = None
w.cfg["ocr_engine"] = "windows"
w.cfg["ocr_lang"] = "zh-Hans-CN"


def pump(seconds=40.0, until=None):
    """转事件循环，直到 until() 为真或超时。返回是否达成。"""
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until is None


# ============ A. 全链路连通 ============
st = w._region_state[0]
w._running = True
_state["ocr_calls"] = 0
w._tick()
check("A1.tick 后 inflight 置位", w._ocr_inflight is True)

ok = pump(60.0, lambda: not w._ocr_inflight)
check("A2.识别结果回到 GUI 线程(inflight 复位)", ok,
      "—— 若 FAIL 说明 sig_ocr_done 没接线")
check("A3.确实调用了 OCR", _state["ocr_calls"] >= 1, f"calls={_state['ocr_calls']}")
check("A4.识别出文字", bool(st["key"]), repr(st["text"][:60]))
check("A5.已投翻译队列", bool(st["pending"] or st["trans"]),
      f"pending={st['pending']}")

# ============ B. 帧内容去重（+ 多帧投票） ============
# v21 起同一画面会连投 _VOTE_FRAMES 帧（补票）然后停止，所以这里断言的是
# "同帧最多识别 K 次、投满即停"，而不是"同帧一次都不识别"。
K = main_mod._VOTE_FRAMES
st["vote_texts"] = []          # 清 A 节残留，重开一轮投票
st["vote_tries"] = 0
calls_before = _state["ocr_calls"]
for _ in range(K + 2):         # 多打两次，验证超出的会被挡掉
    w._tick()
    pump(60.0, lambda: not w._ocr_inflight)
check("B1.同帧最多识别 K 次", _state["ocr_calls"] - calls_before == K,
      f"{calls_before} -> {_state['ocr_calls']} (K={K})")
check("B2.投满后不置 inflight", w._ocr_inflight is False)

# 换一帧（盖一块白斑）应重新识别
#   注意: 指纹是 80x45 缩略图，1 个像素的变化会被缩放平均掉——这是刻意的，
#   能滤掉视频噪点；字幕级别的文字变化必然改变整行像素，不受影响。
altered = BASE_IMG.copy()
altered.paste((255, 255, 255), (0, 40, 160, 70))    # 盖掉一小段字
_state["frame"] = altered
calls_before = _state["ocr_calls"]
w._tick()
check("B3.画面变化后 tick 受理", w._ocr_inflight is True)
pump(60.0, lambda: _state["ocr_calls"] > calls_before)
check("B3b.画面变化后重新识别", _state["ocr_calls"] == calls_before + 1,
      f"{calls_before} -> {_state['ocr_calls']}")
pump(60.0, lambda: not w._ocr_inflight)

# ============ C. 手动重扫清帧指纹 ============
_state["frame"] = BASE_IMG
w._tick()
pump(60.0, lambda: not w._ocr_inflight)
check("C1.帧指纹已记录", st["frame_fp"] is not None)
calls_before = _state["ocr_calls"]
w._rescan()                    # 内部走 QTimer.singleShot，需转事件循环
pump(10.0, lambda: _state["ocr_calls"] > calls_before)
check("C2.重扫强制重新识别", _state["ocr_calls"] > calls_before,
      f"{calls_before} -> {_state['ocr_calls']}")
pump(60.0, lambda: not w._ocr_inflight)

# ============ D. 看门狗复位卡死的 inflight ============
w._ocr_inflight = True
w._ocr_started_at = time.time() - 30.0     # 假装卡了 30 秒
altered2 = BASE_IMG.copy()
altered2.paste((0, 0, 0), (0, 90, 200, 130))    # 换一块明显不同的区域
_state["frame"] = altered2
calls_before = _state["ocr_calls"]
w._tick()
check("D1.卡死超时后强制放行", w._ocr_inflight is True)
pump(60.0, lambda: _state["ocr_calls"] > calls_before)
check("D1b.放行后确实识别了新帧", _state["ocr_calls"] == calls_before + 1,
      f"{calls_before} -> {_state['ocr_calls']}")
w._ocr_inflight = False
w._running = False

# ============ 汇总 ============
failed = [n for n, ok in PASS if not ok]
print(f"\n通过 {len(PASS) - len(failed)}/{len(PASS)}")
for n in failed:
    print("  [FAIL]", n)

try:
    w._timer.stop()
    from translator import shutdown_worker
    shutdown_worker()
except Exception:
    pass
raise SystemExit(1 if failed else 0)
