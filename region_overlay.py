"""选区常驻指示框

在用户划定的识别区域四周画一个醒目的彩色边框（红色描边 + 四角标记），
让人随时知道当前正在识别屏幕的哪一块。

特性:
- 无边框、置顶、半透明、不抢焦点
- 点击穿透 (WindowTransparentForInput): 不挡住底下窗口的任何操作
- region=None (整屏模式) 时隐藏
"""
from __future__ import annotations

from typing import Optional, Tuple

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import QWidget

Region = Tuple[int, int, int, int]  # (x, y, w, h) 屏幕物理坐标


class RegionOverlay(QWidget):
    """常驻的选区边框指示窗。"""

    # 边框参数（默认红色；多区域模式会实例化覆盖为各自颜色）
    DEFAULT_BORDER = QColor(255, 70, 70, 230)   # 半透明红
    DEFAULT_FILL = QColor(255, 70, 70, 14)      # 极淡的填充
    DEFAULT_CORNER = QColor(255, 90, 90, 255)   # 角标亮红
    BORDER_PEN = QColor(255, 70, 70, 230)      # 半透明红
    BORDER_FILL = QColor(255, 70, 70, 14)      # 极淡的填充
    CORNER_PEN = QColor(255, 90, 90, 255)      # 角标亮红
    BORDER_W = 3          # 边框线宽
    CORNER_LEN = 18       # 角标线长
    MARGIN = 0            # 边框相对选区的外扩像素

    def __init__(self):
        super().__init__(None)
        # 置顶 + 无边框 + 工具窗(不在任务栏出现) + 点击穿透 + 不抢焦点
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowOpacity(0.9)
        self._region: Optional[Region] = None
        # 初始隐藏，选区后 show
        self.hide()

    # ===== 公共接口 =====
    def set_region(self, region: Optional[Region], color=None):
        """更新选区。region=None 表示整屏模式 -> 隐藏指示框。

        color: QColor/str，多区域模式下每个区域用不同颜色。
        """
        self._region = region
        if color is not None:
            c = QColor(color)
            self.BORDER_PEN = QColor(c.red(), c.green(), c.blue(), 230)
            self.BORDER_FILL = QColor(c.red(), c.green(), c.blue(), 14)
            self.CORNER_PEN = QColor(c.red(), c.green(), c.blue(), 255)
        else:
            # 单区域模式: 复位默认红（实例属性可能残留上次多区域分配的颜色）
            self.BORDER_PEN = QColor(self.DEFAULT_BORDER)
            self.BORDER_FILL = QColor(self.DEFAULT_FILL)
            self.CORNER_PEN = QColor(self.DEFAULT_CORNER)
        if not region:
            self.hide()
            return
        x, y, w, h = region
        m = self.MARGIN
        # region 是物理像素（mss 坐标系），Qt 窗口几何用逻辑坐标；
        # 高 DPI 缩放（125%/150%）下必须除以 devicePixelRatio，
        # 否则指示框会整体偏移且比实际识别区域大。
        dpr = self.devicePixelRatioF() or 1.0
        self.setGeometry(
            round(x / dpr) - m, round(y / dpr) - m,
            max(1, round(w / dpr) + m * 2), max(1, round(h / dpr) + m * 2))
        self.show()
        self.raise_()

    # ===== 绘制 =====
    def paintEvent(self, event):
        if not self._region:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        w = self.width()
        h = self.height()

        # 淡填充
        p.fillRect(self.rect(), self.BORDER_FILL)

        # 主边框
        pen = QPen(self.BORDER_PEN)
        pen.setWidth(self.BORDER_W)
        p.setPen(pen)
        # 画在矩形中间，避免被窗口边缘裁掉一半
        off = self.BORDER_W // 2 + 1
        p.drawRect(off, off, w - off * 2 - 1, h - off * 2 - 1)

        # 四角加粗标记（十字角），更醒目
        pen2 = QPen(self.CORNER_PEN)
        pen2.setWidth(self.BORDER_W + 2)
        p.setPen(pen2)
        L = self.CORNER_LEN
        o = self.BORDER_W
        # 左上
        p.drawLine(o, o, o + L, o)
        p.drawLine(o, o, o, o + L)
        # 右上
        p.drawLine(w - o - L, o, w - o, o)
        p.drawLine(w - o, o, w - o, o + L)
        # 左下
        p.drawLine(o, h - o, o + L, h - o)
        p.drawLine(o, h - o - L, o, h - o)
        # 右下
        p.drawLine(w - o - L, h - o, w - o, h - o)
        p.drawLine(w - o, h - o - L, w - o, h - o)
        p.end()
