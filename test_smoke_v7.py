"""本次 bug 的专项回归: 启动时 main.py 连接的每个字幕信号都存在。
触发条件: P2 #7 删 request_clear 时漏清 connect 端，启动 AttributeError。
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtWidgets import QApplication
_app = QApplication.instance() or QApplication(sys.argv)
import subtitle

sw = subtitle.SubtitleWindow()
needed = [
    "request_select_region", "request_quit", "request_history",
    "request_manual_translate", "request_set_target_lang",
    "request_display_prefs", "request_rescan",
    "request_toggle_click_through", "request_toggle_paused",
    "request_switch_region", "request_feedback", "request_save_geometry",
]
missing = [n for n in needed if not hasattr(sw, n)]
print(f"MISSING={missing}")
print(f"HAS_request_clear={hasattr(sw, 'request_clear')}")
if missing:
    sys.exit(1)
for n in needed:
    sig = getattr(sw, n, None)
    sig.connect(lambda *a, _n=n: None)
print(f"OK {len(needed)} signals connected")
sys.stdout.flush()
