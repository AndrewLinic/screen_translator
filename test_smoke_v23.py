"""回归测试：选区蒙层"灰色痕迹"bug（v23）。

用持久画布 + QWidget.render(region) 模拟真实屏幕的局部更新：
只把 update 区域渲染到画布上，其余区域保留旧像素（与真屏一致）。

判定方式（修正版）：
  旧版把"远处"与"边缘外"两点互比亮度差——但两点落在真实桌面不同
  背景上（任务栏白底 vs 游戏画面），背景差恒大于阈值，导致误报。
  新版对每个采样点，用 widget 内部 _masked_cache（bg+浅蒙层）推导
  选区外理论值 = masked × 0.843（暗蒙层 alpha 40），比较实际值与
  理论值的偏差。偏差过大 = 有痕迹（局部刷新未覆盖到该点）。
"""
import os
import sys
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import Qt, QPoint, QRect, QEvent
from PyQt5.QtGui import QImage, QColor, QPainter, QRegion
from PyQt5.QtWidgets import QApplication, QDialog
from PyQt5.QtTest import QTest

app = QApplication([])

from screenshot import select_region_interactive

W, H = 1000, 700
RET = {}

# 选区外理论值系数: 浅蒙层 alpha50 已在 _masked_cache 中 (×0.804)，
# 暗蒙层 alpha40 再压一层 (×0.843)。
_DARK_OVERLAY_FACTOR = 0.843
# 实际值与理论值的亮度偏差容忍阈值（抗锯齿/圆角/边框等小偏差）。
_TRACE_THRESHOLD = 15.0


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

    # 用 _masked_cache（bg 缩放到 widget 尺寸 + 浅蒙层 alpha50）推导
    # 选区外各点的理论值 = masked_pixel × 0.843（暗蒙层 alpha40）。
    masked_img = self._masked().toImage()

    def expected_outside(px, py):
        """选区外某点的理论亮度（masked_cache × 暗蒙层系数）。"""
        c = QColor(masked_img.pixel(px, py))
        return (c.red() + c.green() + c.blue()) / 3 * _DARK_OVERLAY_FACTOR

    def lum(c):
        return (c.red() + c.green() + c.blue()) / 3

    far_x, far_y = ww - 40, wh - 40
    edge_x, edge_y = min(rect.right() + 6, ww - 1), rect.center().y()

    p_far = QColor(canvas.pixel(far_x, far_y))          # 远离拖动路径
    p_edge = QColor(canvas.pixel(edge_x, edge_y))        # 选区边缘外
    p_in = QColor(canvas.pixel(rect.center()))            # 选区内
    print('远处像素  :', p_far.name())
    print('边缘外像素:', p_edge.name())
    print('选区内像素:', p_in.name())

    far_dev = abs(lum(p_far) - expected_outside(far_x, far_y))
    edge_dev = abs(lum(p_edge) - expected_outside(edge_x, edge_y))
    print(f'远处偏差  : {far_dev:.1f} (阈值 {_TRACE_THRESHOLD})')
    print(f'边缘外偏差: {edge_dev:.1f} (阈值 {_TRACE_THRESHOLD})')

    # 痕迹判定: 任一点实际值与理论值偏差超阈值 = 局部刷新未覆盖（有痕迹）
    trace = far_dev > _TRACE_THRESHOLD or edge_dev > _TRACE_THRESHOLD
    print('痕迹判定:', '有痕迹(BUG)' if trace else '无痕迹(PASS)')
    if not trace:
        print('通过 1/1')
    RET['trace'] = trace
    RET['devs'] = (far_dev, edge_dev)
    self.accept()
    return QDialog.Accepted


QDialog.exec_ = fake_exec
select_region_interactive()
sys.exit(1 if RET.get('trace') else 0)
