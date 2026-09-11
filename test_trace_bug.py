"""复现/验证选区蒙层"灰色痕迹"bug。

用持久画布 + QWidget.render(region) 模拟真实屏幕的局部更新：
只把 update 区域渲染到画布上，其余区域保留旧像素（与真屏一致）。
判定: 选区边缘外(会被局部刷新)与远离拖动路径处(只有全屏重绘才能触达)
在最终状态下应同为"选区外深色蒙层"。不一致 = 有痕迹。
"""
import os
import sys
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import Qt, QPoint, QRect, QEvent
from PyQt5.QtGui import QImage, QColor, QPainter, QRegion, QLinearGradient, QBrush, QPen
from PyQt5.QtWidgets import QApplication, QDialog
from PyQt5.QtTest import QTest

app = QApplication([])

from screenshot import select_region_interactive

W, H = 1000, 700
RET = {}

def fake_exec(self):
    self.show()
    # 持久画布模拟真实屏幕
    canvas = QImage(W, H, QImage.Format_RGB32)
    canvas.fill(QColor(255, 255, 255))  # 假设初始未被程序覆盖

    # 捕获 update 调用，模拟局部刷新
    captured = []
    orig_update = self.update
    def cap_update(reg=None):
        captured.append(QRegion(self.rect()) if reg is None else QRegion(reg))
        orig_update()  # 保持 Qt 自身调度（offscreen 无副作用）
    self.update = cap_update

    def paint_regions():
        p = QPainter(canvas)
        for reg in captured:
            self.render(p, QPoint(0, 0), reg)
        p.end()
        captured.clear()

    # 1. 初始全屏绘制（此时无选区 -> 整屏浅色蒙层）
    self.repaint()
    paint_regions()

    def mouse(kind, x, y):
        t = {'press': QEvent.MouseButtonPress, 'move': QEvent.MouseMove,
             'release': QEvent.MouseButtonRelease}[kind]
        btn = Qt.LeftButton if kind in ('press', 'release') else Qt.NoButton
        buttons = Qt.LeftButton if kind != 'release' else Qt.NoButton
        QTest.mouseMove(self, QPoint(x, y)) if kind == 'move' else None
        from PyQt5.QtGui import QMouseEvent
        ev = QMouseEvent(t, QPoint(x, y), btn, buttons, Qt.NoModifier)
        if kind == 'press':
            self.mousePressEvent(ev)
        elif kind == 'move':
            self.mouseMoveEvent(ev)
        else:
            self.mouseReleaseEvent(ev)

    # 2. 拖框: 从 (150,120) 斜拖到 (650,430)（对角路径 -> 每帧并集呈锯齿）
    mouse('press', 150, 120)
    for i in range(1, 8):
        mouse('move', 150 + i * 71, 120 + i * 44)
    paint_regions()
    mouse('release', 650, 430)
    paint_regions()

    # 3. 判读（采样点必须在窗口内: 窗口逻辑尺寸可能小于画布）
    rect = self._rect
    ww, wh = self.width(), self.height()
    print('窗口逻辑尺寸:', ww, wh, '| 最终选区:', rect)
    p_far = QColor(canvas.pixel(ww - 40, wh - 40))          # 远离拖动路径
    p_edge = QColor(canvas.pixel(min(rect.right() + 6, ww - 1),
                                 rect.center().y()))        # 选区边缘外
    p_in = QColor(canvas.pixel(rect.center()))              # 选区内
    print('远处像素  :', p_far.name())
    print('边缘外像素:', p_edge.name())
    print('选区内像素:', p_in.name())

    def lum(c):
        return (c.red() + c.green() + c.blue()) / 3
    trace = abs(lum(p_far) - lum(p_edge)) > 6
    print('痕迹判定:', '有痕迹(BUG)' if trace else '无痕迹(PASS)')
    RET['trace'] = trace
    RET['lums'] = (lum(p_far), lum(p_edge), lum(p_in))
    canvas.save('_dbg_trace.png')
    self.accept()
    return QDialog.Accepted

QDialog.exec_ = fake_exec
select_region_interactive()
sys.exit(1 if RET.get('trace') else 0)
