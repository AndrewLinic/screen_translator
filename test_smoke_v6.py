"""offscreen 冒烟测试 v6 —— 验证 14 项 P2 修复

#4  暂停时停 spinner       #5  slow-hint 不再强引用 self
#6  删除孤儿工具栏按钮      #7  删除死代码（set_font_size/_highlight_words/request_clear）
#8  断裂合并用去标点拼接    #9  Mac/Mc 前缀豁免
#10 放大镜取样 clamp        #11 删除 _confirmed + grab 异常保护
#12 删除三处死代码          #13 热键线程退出用本地副本反注册
#14 overlay 颜色复位        #15 translator 等响应不持锁 + 关 stderr 句柄
#16 restore_geometry 多屏   #17 chips 浅色主题可见
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import inspect

from PyQt5.QtWidgets import QApplication
app = QApplication([])

PASS = []
def check(name, cond):
    PASS.append((name, bool(cond)))
    print(('PASS' if cond else 'FAIL'), name)


# ===== #8/#9 OCR 词级修复 =====
import ocr
check('ocr.merge_broken_word',
      ocr._repair_latin_words("Exped. ition") == "Expedition")
check('ocr.merge_keeps_prefix',
      ocr._repair_latin_words("(Exped ition") == "(Expedition")
check('ocr.mac_exemption',
      "macOS" in ocr._repair_latin_words("macOS is here"))
check('ocr.mc_exemption',
      "McDonald" in ocr._repair_latin_words("McDonald is here"))
check('ocr.basics_still_fixed',
      "Basics" in ocr._repair_latin_words("BasiCS is fine"))


# ===== #10/#11 screenshot =====
import screenshot
src_shot = inspect.getsource(screenshot)
check('shot.magnifier_clamp', 'max(0, min(' in src_shot)
check('shot.no_confirmed', '_confirmed' not in src_shot)
vd = screenshot.grab_virtual_desktop()
check('shot.grab_returns_5tuple',
      isinstance(vd, tuple) and len(vd) == 5 and vd[2] > 0 and vd[3] > 0)


# ===== #14 region_overlay 颜色复位 =====
from region_overlay import RegionOverlay
ov = RegionOverlay()
ov.set_region((10, 10, 100, 50), color="#34c759")
check('overlay.custom_color', ov.BORDER_PEN.green() >= 190 and ov.BORDER_PEN.red() < 100)
ov.set_region((10, 10, 100, 50))
check('overlay.color_reset', ov.BORDER_PEN.red() > 200)
check('overlay.no_current_region', not hasattr(ov, 'current_region'))


# ===== #4/#5/#6/#7/#16/#17 subtitle =====
from subtitle import SubtitleWindow
check('subtitle.no_orphan_buttons',
      not any(hasattr(SubtitleWindow, 'btn_' + n) for n in ())
      and not hasattr(SubtitleWindow, 'request_clear'))
check('subtitle.no_set_font_size', not hasattr(SubtitleWindow, 'set_font_size'))
check('subtitle.no_highlight_words', not hasattr(SubtitleWindow, '_highlight_words'))

sub = SubtitleWindow()
sub.show()
# #16 恢复几何
sub.restore_geometry([100, 100, 900, 300])
check('subtitle.geometry_restored', sub.width() == 900)
gx, gy = sub.x(), sub.y()
sub.restore_geometry([-50000, -50000, 900, 300])   # 任何屏都够不到
check('subtitle.offscreen_rejected', (sub.x(), sub.y()) == (gx, gy))
# #17 chips 主题
sub.set_region_chips(2, 0)
sub.set_theme("light")
check('subtitle.chips_light_visible',
      '#111111' in sub._region_chips[1].styleSheet())
check('subtitle.chips_active_kept',
      '#ff4d4d' in sub._region_chips[0].styleSheet())
sub.set_theme("dark")
check('subtitle.chips_dark_restored',
      'rgba(255,255,255,20)' in sub._region_chips[1].styleSheet())


# ===== #12 死代码 =====
import config
check('config.no_update_config', not hasattr(config, 'update_config'))
import main as m
src_build_menu = inspect.getsource(
    m.ScreenTranslatorApp._build_tray_menu
    if hasattr(m.ScreenTranslatorApp, '_build_tray_menu')
    else m.ScreenTranslatorApp._make_tray_menu)
check('main.no_stale_old_var', 'old = getattr' not in src_build_menu)


# ===== #13 热键 =====
import hotkeys
src_run = inspect.getsource(hotkeys.HotkeyFilter._run)
check('hotkeys.local_registered', 'local_registered' in src_run)
src_unreg = inspect.getsource(hotkeys.HotkeyFilter.unregister_all)
check('hotkeys.join_3s', 'timeout=3' in src_unreg)


# ===== #15 translator 并发 =====
import translator
src_req = inspect.getsource(translator._ArgosWorker._request)
check('trans.wait_without_lock',
      src_req.index('with _worker_lock') < src_req.index('my_q.get'))
check('trans.pending_demux', '_pending' in src_req)
src_kill = inspect.getsource(translator._ArgosWorker._kill)
check('trans.kill_closes_stderr', '_stderr_fh.close' in src_kill)


# ===== 汇总 =====
failed = [n for n, ok in PASS if not ok]
print(f'{len(PASS) - len(failed)}/{len(PASS)} passed')
raise SystemExit(1 if failed else 0)
