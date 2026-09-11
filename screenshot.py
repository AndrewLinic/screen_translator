"""屏幕截图 + 区域选择"""
from __future__ import annotations

from typing import Optional, Tuple

import mss
from PIL import Image

Region = Tuple[int, int, int, int]  # (x, y, w, h) 屏幕绝对坐标


def _sct():
    """mss 实例（新版推荐 MSS()，但兼容旧 API）"""
    return mss.mss() if hasattr(mss, "mss") else mss.MSS()


def get_virtual_screen() -> Tuple[int, int, int, int]:
    """返回虚拟桌面 (x, y, w, h)，多显示器时仍正确。"""
    with _sct() as sct:
        m = sct.monitors[0]  # 0 = 整个虚拟桌面
        return int(m["left"]), int(m["top"]), int(m["width"]), int(m["height"])


def get_primary_screen_size() -> Tuple[int, int]:
    """主屏幕尺寸 (w, h)"""
    with _sct() as sct:
        m = sct.monitors[1]
        return int(m["width"]), int(m["height"])


def grab_region(region: Optional[Region]) -> Optional[Image.Image]:
    """截取指定屏幕区域，返回 PIL Image。region=None 截全屏（主屏）。"""
    try:
        with _sct() as sct:
            if region is None:
                monitor = sct.monitors[1]
                img = sct.grab(monitor)
            else:
                x, y, w, h = region
                if w <= 0 or h <= 0:
                    return None
                monitor = {"left": int(x), "top": int(y), "width": int(w), "height": int(h)}
                img = sct.grab(monitor)
            pil_img = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
            return pil_img
    except Exception:
        return None


def grab_virtual_desktop() -> Tuple[int, int, int, int, Image.Image]:
    """截整个虚拟桌面，返回 (vx, vy, vw, vh, image)。"""
    try:
        with _sct() as sct:
            m = sct.monitors[0]
            img = sct.grab(m)
            pil_img = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
            return int(m["left"]), int(m["top"]), int(m["width"]), int(m["height"]), pil_img
    except Exception:
        pass
    # 兼容底: mss 偶发失败（显卡驱动/睡眠唤醒）时退回 Qt 抓主屏，
    # 避免整个选区流程直接崩掉
    try:
        from PyQt5.QtGui import QGuiApplication, QImage
        scr = QGuiApplication.primaryScreen()
        if scr is not None:
            pm = scr.grabWindow(0)
            qimg = pm.toImage().convertToFormat(QImage.Format_ARGB32)
            data = qimg.constBits().asstring(qimg.byteCount())
            pil_img = Image.frombytes(
                "RGBA", (qimg.width(), qimg.height()), data, "raw", "BGRA"
            ).convert("RGB")
            return 0, 0, qimg.width(), qimg.height(), pil_img
    except Exception:
        pass
    return 0, 0, 1, 1, Image.new("RGB", (1, 1), (0, 0, 0))


def select_region_interactive(parent=None) -> Optional[Region]:
    """弹出全屏区域选择器，让用户拖框选区域。

    交互:
        拖动框选 -> 松开后进入微调模式:
            - 拖动四角手柄微调选区
            - 在框外重新按下可重选
            - Enter / 双击选区内 确认
            - Esc 取消
    返回屏幕绝对坐标 (x, y, w, h)，取消返回 None。
    """
    from PyQt5.QtCore import Qt, QRect, QPoint
    from PyQt5.QtGui import (QPainter, QColor, QPen, QCursor, QPixmap, QBrush,
                             QRegion)
    from PyQt5.QtWidgets import QDialog

    CORNER_R = 14  # 角手柄命中半径（逻辑像素）

    class _Selector(QDialog):
        def __init__(self, bg_pixmap: QPixmap, sx: int, sy: int, sw: int, sh: int):
            super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
            self.setCursor(QCursor(Qt.CrossCursor))
            self.setModal(True)
            self._origin = QPoint()
            self._bg = bg_pixmap
            self._sx, self._sy = sx, sy
            self._sw, self._sh = sw, sh
            self._rect: Optional[QRect] = None   # 当前选区（逻辑坐标）
            self._cursor_pos = QPoint(0, 0)
            self._corner = None                  # 正在拖动的角: tl/tr/bl/br
            self._dragging = False
            self._masked_cache: Optional[QPixmap] = None  # 背景+蒙层合成缓存
            self.setGeometry(sx, sy, sw, sh)
            self.showFullScreen()

        # ---- 性能 ----
        def _masked(self) -> QPixmap:
            """背景+全屏蒙层合成缓存（只做一次）。

            之前每次 paintEvent 都重新缩放绘制整屏背景+填蒙层，
            这是拖动卡顿的主因；现在只需一次快速 blit。
            """
            if (self._masked_cache is None
                    or self._masked_cache.size() != self.size()):
                pm = QPixmap(self.size())
                p = QPainter(pm)
                p.drawPixmap(self.rect(), self._bg)
                p.fillRect(self.rect(), QColor(0, 0, 0, 50))
                p.end()
                self._masked_cache = pm
            return self._masked_cache

        def _mag_pos(self, pos: QPoint) -> Optional[QRect]:
            """放大镜显示矩形（逻辑坐标）；屏幕边缘放不下时返回 None。"""
            cx, cy = pos.x(), pos.y()
            mx, my = cx + 24, cy + 24
            if mx + 180 > self.width():
                mx = cx - 24 - 180
            if my + 180 > self.height():
                my = cy - 24 - 180
            if mx < 0 or my < 0:
                return None
            return QRect(mx, my, 180, 180)

        def _smart_update(self, old_rect, old_pos: QPoint):
            """只刷新选区和放大镜涉及的屏幕区域（全屏重绘是卡顿根因）。"""
            reg = QRegion()
            for r in (old_rect, self._rect):
                if r is not None:
                    reg += QRegion(r.adjusted(-12, -12, 12, 12))
            for pos in (old_pos, self._cursor_pos):
                box = self._mag_pos(pos)
                if box is not None:
                    reg += QRegion(box.adjusted(-4, -4, 4, 4))
            self.update(reg)

        # ---- 坐标换算 ----
        def _k(self):
            """逻辑 -> 物理 缩放系数。"""
            return (self._sw / max(1, self.width()),
                    self._sh / max(1, self.height()))

        def _to_physical(self, r: QRect) -> QRect:
            kx, ky = self._k()
            return QRect(
                int(round(r.x() * kx)), int(round(r.y() * ky)),
                int(round(r.width() * kx)), int(round(r.height() * ky)),
            )

        @staticmethod
        def _corner_at(rect: QRect, pos: QPoint) -> Optional[str]:
            """pos 在矩形哪个角的手柄上（逻辑坐标）。"""
            if rect is None:
                return None
            for name, cp in (("tl", rect.topLeft()), ("tr", rect.topRight()),
                             ("bl", rect.bottomLeft()), ("br", rect.bottomRight())):
                if (cp - pos).manhattanLength() <= CORNER_R:
                    return name
            return None

        # ---- 绘制 ----
        def paintEvent(self, event):
            p = QPainter(self)
            # 只绘制失效区域；背景直接贴合成缓存（缩放+蒙层只算一次）
            p.setClipRect(event.rect())
            p.drawPixmap(0, 0, self._masked())
            rect = self._rect
            if rect is not None and rect.width() > 0:
                # 选区外再压暗一层，突出选区
                p.setClipRegion(QRegion_limited(rect, self.rect()))
                p.fillRect(self.rect(), QColor(0, 0, 0, 40))
                p.setClipping(False)
                # 边框
                pen = QPen(QColor(45, 125, 246), 2)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawRect(rect)
                # 四角手柄
                p.setBrush(QBrush(QColor(45, 125, 246)))
                p.setPen(QPen(QColor("white"), 1))
                for cp in (rect.topLeft(), rect.topRight(),
                           rect.bottomLeft(), rect.bottomRight()):
                    p.drawRoundedRect(
                        QRect(cp.x() - 6, cp.y() - 6, 12, 12), 3, 3)
            # 提示文字（顶部居中）
            p.setPen(QPen(QColor("white")))
            f = p.font()
            f.setPointSize(11)
            p.setFont(f)
            hint = ("拖动框选区域 · 松开后拖动四角微调 · Enter / 双击确认 · Esc 取消")
            p.fillRect(0, 8, self.width(), 30, QColor(0, 0, 0, 140))
            p.drawText(QRect(0, 8, self.width(), 30), Qt.AlignCenter, hint)

            # 放大镜（拖动中）
            if self._dragging:
                self._draw_magnifier(p)
            p.end()

        def _draw_magnifier(self, p: QPainter):
            """光标处 3x 放大镜，方便对齐小字区域。"""
            kx, ky = self._k()
            cx, cy = self._cursor_pos.x(), self._cursor_pos.y()
            src_w, src_h = int(60 * kx), int(60 * ky)
            # clamp 到背景图范围内（屏幕边缘为负时 QPixmap.copy 越界
            # 区域内容未定义，放大镜会闪出雪花块）
            sx = max(0, min(int(cx * kx) - src_w // 2, self._bg.width() - src_w))
            sy = max(0, min(int(cy * ky) - src_h // 2, self._bg.height() - src_h))
            src = self._bg.copy(sx, sy, src_w, src_h)
            mag = src.scaled(180, 180)
            box = self._mag_pos(self._cursor_pos)
            if box is None:
                return
            mx, my = box.x(), box.y()
            p.drawPixmap(mx, my, mag)
            # 边框: drawRect 会用当前画刷填充！前面画四角手柄设了蓝画刷，
            # 不复位会把放大镜整个糊成蓝色（实测踩坑）
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("white"), 2))
            p.drawRect(box)
            # 十字线
            p.setPen(QPen(QColor(255, 80, 80), 1))
            p.drawLine(mx + 90, my + 70, mx + 90, my + 110)
            p.drawLine(mx + 70, my + 90, mx + 110, my + 90)

        # ---- 鼠标 ----
        def mousePressEvent(self, event):
            if event.button() != Qt.LeftButton:
                return
            pos = event.pos()
            self._cursor_pos = pos
            # 已有选区且点到角手柄 -> 微调
            c = self._corner_at(self._rect, pos)
            if c:
                self._corner = c
                self._dragging = True
                return
            # 点在选区内部: 不重选（留给双击确认）
            if self._rect is not None and self._rect.contains(pos):
                return
            # 选区外 -> 重新框选
            self._origin = pos
            self._rect = QRect(pos, pos)
            self._dragging = True
            # 蒙层深浅（选区外多压暗一层）是全屏效果：选区出现/替换后，
            # 远离拖动路径的区域不会被局部刷新触达，停留在旧底色上，会
            # 在拖动路径上留下深浅不一的锯齿状"痕迹"（实测踩坑）。
            # 选区创建/替换必须全屏重绘一次（现在只是一次缓存 blit，代价极低）
            self.update()

        def mouseMoveEvent(self, event):
            pos = event.pos()
            old_rect = QRect(self._rect) if self._rect is not None else None
            old_pos = QPoint(self._cursor_pos)
            self._cursor_pos = pos
            if self._dragging and self._corner:
                r = QRect(self._rect)
                if self._corner == "tl":
                    r.setTopLeft(pos)
                elif self._corner == "tr":
                    r.setTopRight(pos)
                elif self._corner == "bl":
                    r.setBottomLeft(pos)
                elif self._corner == "br":
                    r.setBottomRight(pos)
                self._rect = r.normalized()
            elif self._dragging and not self._origin.isNull():
                self._rect = QRect(self._origin, pos).normalized()
            else:
                # 悬停在角上时提示可拖动
                c = self._corner_at(self._rect, pos)
                if c in ("tl", "br"):
                    self.setCursor(QCursor(Qt.SizeFDiagCursor))
                elif c in ("tr", "bl"):
                    self.setCursor(QCursor(Qt.SizeBDiagCursor))
                else:
                    self.setCursor(QCursor(Qt.CrossCursor))
            self._smart_update(old_rect, old_pos)

        def mouseReleaseEvent(self, event):
            if event.button() != Qt.LeftButton:
                return
            self._corner = None
            self._dragging = False
            if self._rect is not None and (self._rect.width() <= 3
                                           or self._rect.height() <= 3):
                self._rect = None
            # 手势结束统一全屏重绘: 抹掉放大镜、把选区外的压暗层铺满全屏，
            # 保证下一次局部刷新的底色与当前状态一致（否则又留痕迹）
            self.update()

        def mouseDoubleClickEvent(self, event):
            if (self._rect is not None and self._rect.contains(event.pos())
                    and self._rect.width() > 3):
                self.accept()

        def keyPressEvent(self, event):
            if event.key() == Qt.Key_Escape:
                self._rect = None
                self.reject()
            elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
                if self._rect is not None and self._rect.width() > 3:
                    self.accept()

        def get_selected(self) -> Optional[QRect]:
            return self._rect

    def QRegion_limited(rect: QRect, bounds: QRect):
        """矩形外的区域（用于把选区外再压暗）。"""
        from PyQt5.QtGui import QRegion
        full = QRegion(bounds)
        return full.subtracted(QRegion(rect))

    # 先截好桌面再弹窗，避免被自身遮罩挡住
    sx, sy, sw, sh, bg_img = grab_virtual_desktop()
    pixmap = QPixmap.fromImage(_pil_to_qimage(bg_img))

    dlg = _Selector(pixmap, sx, sy, sw, sh)
    if dlg.exec_() == QDialog.Accepted:
        rect = dlg.get_selected()
        if rect is None:
            return None
        # DPI 缩放换算: rect 是选择器窗口内的逻辑坐标（Qt 在 125%/150% 缩放
        # 下鼠标事件是逻辑像素），必须先经 _to_physical 放大到物理像素，
        # 再加虚拟桌面的物理偏移 (sx, sy)（mss 返回/接收的都是物理坐标）。
        # 之前直接 rect + (sx, sy)，高缩放下选区整体偏移且尺寸偏小。
        phys = dlg._to_physical(rect)
        x = phys.x() + sx
        y = phys.y() + sy
        return (int(x), int(y), int(phys.width()), int(phys.height()))
    return None


def _pil_to_qimage(img: Image.Image):
    """PIL Image -> QImage"""
    from PyQt5.QtGui import QImage
    img = img.convert("RGBA")
    data = img.tobytes("raw", "BGRA")
    qimg = QImage(data, img.width, img.height, QImage.Format_ARGB32)
    return qimg
