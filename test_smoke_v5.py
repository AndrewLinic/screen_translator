"""offscreen 冒烟测试 v5 —— 验证代码审查 3 个 P1 修复

P1-1: DPI 选区坐标错位（screenshot.py 返回物理坐标 + region_overlay 逻辑换算）
P1-2: 反馈诊断包缺 app.log（main.py 改用 _LOG_DIR）
P1-3: _region_state 跨线程无锁（静态断言 dispatch/reset 均加锁 + gen 校验）
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import inspect
import tempfile
import zipfile
from pathlib import Path

from PyQt5.QtCore import Qt, QRect
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QApplication, QDialog, QMessageBox

app = QApplication([])

PASS = []
def check(name, cond):
    PASS.append((name, bool(cond)))
    print(('PASS' if cond else 'FAIL'), name)


# ===== P1-1: DPI 选区坐标 =====
from screenshot import select_region_interactive, grab_virtual_desktop

sx, sy, sw, sh, _ = grab_virtual_desktop()
captured = {}

def fake_exec(self):
    self._rect = QRect(100, 80, 200, 150)
    kx, ky = self._k()
    phys = self._to_physical(self._rect)
    captured['phys'] = phys
    # 独立复算: 物理坐标 = 逻辑坐标 * (物理桌面尺寸/窗口逻辑尺寸)
    captured['expect'] = QRect(round(100 * kx), round(80 * ky),
                               round(200 * kx), round(150 * ky))
    self.accept()
    return QDialog.Accepted

QDialog.exec_ = fake_exec
region = select_region_interactive()
del QDialog.exec_

check('dpi.to_physical_math',
      captured.get('phys') == captured.get('expect'))
p = captured.get('phys')
check('dpi.select_returns_physical',
      region == (p.x() + sx, p.y() + sy, p.width(), p.height()))
check('dpi.select_not_raw_logical',
      region != (100 + sx, 80 + sy, 200, 150) or (sx == 0 and sy == 0 and p == captured['expect']))

# RegionOverlay: 物理坐标 -> Qt 逻辑坐标（dpr=1.5 模拟 150% 缩放）
from region_overlay import RegionOverlay
ov = RegionOverlay()
ov.devicePixelRatioF = lambda: 1.5
ov.set_region((300, 240, 450, 300))
g = ov.geometry()
check('dpi.overlay_logical',
      (g.x(), g.y(), g.width(), g.height()) == (200, 160, 300, 200))


# ===== P1-2: 反馈诊断包含日志 =====
import main as m

tmp = Path(tempfile.mkdtemp(prefix='fbtest_'))
(tmp / 'app.log').write_text('INFO fake log line\n', encoding='utf-8')
m._LOG_DIR = tmp
shown = []
QMessageBox.information = lambda *a, **k: shown.append(a)

class FakeSelf:
    cfg = {'engine_mode': 'mock'}
    _active_region = 0
    _region_state = [{'img': None, 'orig': 'hello', 'trans': 'hi'}]

m.ScreenTranslatorApp._export_feedback(FakeSelf(), 'fix hello')
zips = list((tmp / 'feedback').glob('feedback_*.zip'))
check('feedback.zip_created', len(zips) == 1)
if zips:
    with zipfile.ZipFile(zips[0]) as z:
        names = z.namelist()
    check('feedback.has_log_tail', 'app.log.tail' in names)
    check('feedback.has_texts',
          'ocr_text.txt' in names and 'corrected_text.txt' in names)
else:
    check('feedback.has_log_tail', False)
    check('feedback.has_texts', False)


# ===== P1-3: _region_state 加锁（多线程逻辑不进冒烟，静态断言防回归） =====
src_dispatch = inspect.getsource(m.ScreenTranslatorApp._dispatch_loop)
check('dispatch.uses_state_lock', '_state_lock' in src_dispatch)
check('dispatch.gen_stale_check', 'gen"] != gen' in src_dispatch
      or 'gen"] == gen' in src_dispatch)
src_reset = inspect.getsource(m.ScreenTranslatorApp._reset_region_states)
check('reset.uses_state_lock', '_state_lock' in src_reset)
src_tick = inspect.getsource(m.ScreenTranslatorApp._tick)
check('tick.rebind_locked', '_state_lock' in src_tick)


# ===== 汇总 =====
failed = [n for n, ok in PASS if not ok]
print(f'{len(PASS) - len(failed)}/{len(PASS)} passed')
raise SystemExit(1 if failed else 0)
