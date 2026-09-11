"""新功能 offscreen 冒烟测试（无头环境，不验证截图/托盘）。"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from PyQt5.QtWidgets import QApplication

app = QApplication(sys.argv)

failures = []

def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        failures.append(name)

# ===== 1. 翻译缓存 =====
import trans_cache
db = Path(tempfile.mkdtemp()) / "translations.db"
check("cache.init", trans_cache.init(db) is True)
k = trans_cache.cache_key("hello world", "auto", "zh")
check("cache.put", (trans_cache.put(k, "你好世界"), True)[1])
check("cache.get", trans_cache.get(k) == "你好世界")
check("cache.miss", trans_cache.get(trans_cache.cache_key("xxx", "auto", "zh")) is None)
for i in range(60):
    trans_cache.put(trans_cache.cache_key(f"t{i}", "auto", "zh"), str(i))
check("cache.roundtrip", trans_cache.get(k) == "你好世界")

# ===== 2. 字幕窗口 =====
from subtitle import SubtitleWindow
sub = SubtitleWindow()
sub.show()  # offscreen 也需要 show，isVisible 才有意义
sub.apply_prefs({
    "font_size_original": 18, "font_size": 22, "show_original": True,
    "display_mode": "block", "theme": "dark", "window_opacity": 0.9,
    "tts_rate": 2,
})
sub.show_text("Hello world\nSecond line", "你好世界\n第二行")
check("show_text.block", sub._translated_view.isVisible() or sub._translated_view.toPlainText() != "")
# 逐行对照
sub.set_display_mode("interleave")
html = sub._interleave_html("A\nB", "甲\n乙")
check("interleave.pairs", "甲" in html and "A" in html)
html2 = sub._interleave_html("A\nB\nC", "甲\n乙")
check("interleave.fallback", "甲" in html2 and "C" in html2)
sub.show_text("Hello", "你好")
# 主题
sub.set_theme("light")
check("theme.light", sub._text_color() == "#111111")
sub.set_theme("dark")
check("theme.dark", sub._text_color() == "white")
# 穿透: 实现改用原生窗口标志 Qt.WindowTransparentForInput
# （WA_TransparentForMouseEvents 会反向改写该标志，导致穿透关不掉，故不再用）
sub.set_click_through(True)
from PyQt5.QtCore import Qt
check("clickthrough.on", bool(sub.windowFlags() & Qt.WindowTransparentForInput))
sub.set_click_through(False)
check("clickthrough.off",
      not (sub.windowFlags() & Qt.WindowTransparentForInput))
# 区域 chips
sub.set_region_chips(3, 1)
check("chips.show", sub._region_chips[1].isVisible())
sub.set_region_chips(0, 0)
check("chips.hide", not sub._region_chips[0].isVisible())
# 状态灯 / spinner
sub.set_engine_status("ok", "test")
check("engine_dot", sub._engine_dot.isVisible())
sub.set_busy(True)
check("busy.on", sub._spinner.isVisible())
sub.set_busy(False)
check("busy.off", not sub._spinner.isVisible())
# 翻译中 + 对照模式
sub.set_display_mode("interleave")
sub.show_translating("Hello")
check("translating.interleave", "翻译中" in sub._translated_view.toHtml())
# 恢复几何（最小宽度约 841，用足够大的值）
sub.restore_geometry([100, 100, 900, 300])
check("geometry.restore", sub.width() == 900 and sub.height() == 300)
# 显示弹窗（offscreen 下打开不崩）
try:
    sub._open_display_popup()
    sub.close_pref = True
    check("display_popup", True)
    if sub._pref_dlg is not None:
        sub._pref_dlg.close()
except Exception as e:
    check("display_popup (%s)" % e, False)
# 偏好信号（dict）
got = {}
sub.request_display_prefs.connect(lambda d: got.update(d))
sub._pref_dlg = None
# 直接模拟 _on_pref_changed 无法无弹窗运行；改为手动验证信号类型
sub.request_display_prefs.emit({"show_original": True})
check("prefs.dict_signal", got.get("show_original") is True)
# 编辑对话框带反馈按钮
from subtitle import _EditDialog, _HistoryDialog
dlg = _EditDialog("abc", sub)
check("editdlg.feedback", hasattr(dlg, "_send_feedback"))
# 历史导出
sub._record_history("hello", "你好")
sub._record_history("world", "世界")
hd = _HistoryDialog(sub._history, sub)
check("historydialog", hd is not None)

# ===== 3. 截图选择器类结构 =====
import screenshot
check("screenshot.import", hasattr(screenshot, "select_region_interactive"))

# ===== 4. 区域指示框颜色 =====
from region_overlay import RegionOverlay
ov = RegionOverlay()
ov.set_region((10, 10, 200, 100), color="#34c759")
check("overlay.color", ov.isVisible() and ov.CORNER_PEN.green() > 150)
ov.set_region(None)
check("overlay.hide", not ov.isVisible())

# ===== 5. main 模块导入（不实例化，避免 offscreen 托盘段错误） =====
import importlib
m = importlib.import_module("main")
check("main.import", hasattr(m, "ScreenTranslatorApp"))
check("main.signal", m.ScreenTranslatorApp.sig_show_text is not None)

# ===== 6. 配置新键 =====
from config import DEFAULT_CONFIG, data_dir
check("cfg.keys", all(k in DEFAULT_CONFIG for k in (
    "display_mode", "theme", "window_opacity", "tts_rate",
    "click_through", "regions", "subtitle_geometry")))
check("data_dir", data_dir().exists())

print()
if failures:
    print("FAILED:", failures)
    sys.exit(1)
print("ALL SMOKE TESTS PASSED")
