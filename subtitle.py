"""半透明悬浮字幕窗口 V4

UI 自上而下:
    [⠿拖动柄][⬜选区][⏸暂停][✏编辑][Aa显示][🌐语言][1 2 3][⠋][●][⋯更多][❌退出]
    ╭───────────────────────────────╮
    │ 原文 (灰, 较小, 可右键拖选查词)  │
    │ 译文 (白, 加粗, 纯文本渲染)      │
    ╰───────────────────────────────╯

交互:
- 拖动: 左键按住工具栏/空白处直接拖（无需按 Alt）；松手时自动贴边吸附
- 左键点击译文里的英文单词: 查词
- 右键长按拖动选择单词/词组: 松开即查词（原文区和译文区都支持）
- 普通右键: 弹 查词 / 朗读 / 加入生词本 / 复制 菜单
- 双击原文 / 点 ✏编辑: 修改 OCR 识别文本后重新翻译（可一键导出误识反馈）
- ⋯更多菜单: 复制原文/译文、朗读译文、历史、生词本、点击穿透、重扫
- Aa 显示弹窗: 显示原文 / 双语逐行对照 / 深浅主题 / 字号 / 不透明度 / 朗读语速
- 1 2 3 按钮: 多区域轮询模式下切换显示区域
- ⠋ spinner: 翻译进行中；● 状态灯: 离线引擎就绪状态
"""
from __future__ import annotations

import logging
import re
from collections import deque
from typing import Optional, Deque

from PyQt5.QtCore import Qt, QPoint, QTimer, QEvent
from PyQt5.QtGui import (
    QColor, QFont, QCursor, QMouseEvent, QTextCursor, QTextOption,
)
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QTextBrowser, QMenu,
    QAction, QApplication, QLabel, QPushButton, QToolButton, QSizePolicy,
    QDialog, QPlainTextEdit, QSizeGrip, QSlider, QCheckBox,
)

from PyQt5.QtCore import pyqtSignal

import tts
import vocab
import dict_lookup

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*[A-Za-z]")

# 选择文本两侧要剥掉的标点
_STRIP_PUNCT = " \t\n.,!?;:\"'`()[]{}<>。，！？；：、""''（）《》【】…-"


class _SelectBrowser(QTextBrowser):
    """文本区（原文/译文共用），支持：

    - 左键点高亮链接查词（anchorClicked）
    - 右键长按拖动选择文字 -> 松开即查词（用户要的核心交互）
    - 普通右键（未拖动）-> 弹上下文菜单
    - 双击 -> 回调（用于打开"修改识别文本"编辑器）
    """

    def __init__(self, parent, on_lookup, on_context_menu, on_double_click=None):
        super().__init__(parent)
        self._on_lookup = on_lookup
        self._on_context = on_context_menu
        self._on_double_click = on_double_click
        self._rpress_pos: Optional[QPoint] = None
        self._rmoved = False

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self._rpress_pos = QPoint(e.pos())
            self._rmoved = False
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if (e.buttons() & Qt.RightButton) and self._rpress_pos is not None:
            if (e.pos() - self._rpress_pos).manhattanLength() > 8:
                self._rmoved = True
                anchor = self.cursorForPosition(self._rpress_pos)
                cur = self.cursorForPosition(e.pos())
                c = QTextCursor(anchor)
                c.setPosition(cur.position(), QTextCursor.KeepAnchor)
                self.setTextCursor(c)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.RightButton:
            moved = self._rmoved
            sel = self.textCursor().selectedText().strip()
            self._rpress_pos = None
            self._rmoved = False
            if moved and sel:
                self._on_lookup(sel)
            elif not moved:
                self._on_context(e.pos())
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        if self._on_double_click is not None:
            self._on_double_click()
            e.accept()
            return
        super().mouseDoubleClickEvent(e)


class SubtitleWindow(QWidget):
    request_select_region = pyqtSignal()
    request_quit = pyqtSignal()
    request_history = pyqtSignal()
    request_manual_translate = pyqtSignal(str)  # 用户修改原文后要求重译
    request_set_target_lang = pyqtSignal(str)   # 用户在工具栏切换翻译语言
    request_rescan = pyqtSignal()               # 一键重扫（重新识别翻译当前内容）
    request_toggle_click_through = pyqtSignal() # 切换鼠标点击穿透
    request_toggle_paused = pyqtSignal(bool)    # 暂停/恢复 -> main 停启轮询
    request_switch_region = pyqtSignal(int)     # 多区域: 切换当前显示的区域
    request_feedback = pyqtSignal(str)          # OCR 误识反馈（导出诊断包）
    # 显示偏好变化（dict: show_original/font_size_original/font_size/
    # display_mode/theme/opacity/tts_rate）-> main 存配置
    request_display_prefs = pyqtSignal(dict)
    # 字幕窗口几何变化 -> main 持久化 [x, y, w, h]
    request_save_geometry = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowOpacity(0.92)

        self._drag_pos: Optional[QPoint] = None
        self._history: Deque[dict] = deque(maxlen=8)  # 最近 8 条

        # 圆角背景容器（背景区域可拖动，给出移动光标提示）
        self._frame = QFrame(self)
        self._frame.setObjectName("SubFrame")
        self._frame.setCursor(Qt.SizeAllCursor)
        self._frame.setStyleSheet(
            "QFrame#SubFrame {"
            "  background-color: rgba(20, 20, 20, 220);"
            "  border: 1px solid rgba(255,255,255,40);"
            "  border-radius: 10px;"
            "}"
        )

        # ===== 工具栏 =====
        self._toolbar = QFrame(self._frame)
        self._toolbar.setStyleSheet(
            "QFrame { background: transparent; }"
            "QPushButton { color: white; background: rgba(255,255,255,20);"
            " border: 1px solid rgba(255,255,255,30);"
            " border-radius: 4px; padding: 4px 10px; }"
            "QPushButton:hover { background: rgba(255,255,255,40); }"
            "QPushButton:pressed { background: rgba(45,125,246,180); }"
        )
        tb = QHBoxLayout(self._toolbar)
        tb.setContentsMargins(8, 4, 8, 4)
        tb.setSpacing(4)

        # 拖动柄: 提示窗口可以直接拖动
        self._grip = QLabel("⠿", self._toolbar)
        self._grip.setToolTip("按住这里（或工具栏空白处）拖动窗口")
        self._grip.setStyleSheet("color: #999; font-size: 14px; padding: 0 4px;")
        self._grip.setCursor(Qt.SizeAllCursor)
        tb.addWidget(self._grip)

        self.btn_select = self._tb_btn("⬜ 选区", "选择识别区域", self.request_select_region.emit)
        self.btn_pause = self._tb_btn("⏸ 暂停", "暂停/恢复识别", self._toggle_pause)
        self.btn_edit = self._tb_btn("✏ 编辑", "修改识别文本后重新翻译", self.open_editor)
        # 低频功能（复制/历史/生词本/朗读）收进"⋯更多"菜单，
        # 不再创建工具栏按钮（之前创建后从未加入布局，成了孤儿控件）
        self.btn_rescan = self._tb_btn("🔁 重扫", "强制重新识别翻译当前内容 (Ctrl+Alt+R)",
                                       self.request_rescan.emit)
        # 显示设置: 字号/显示原文/逐行对照/主题/透明度/朗读语速
        self.btn_display = self._tb_btn("Aa 显示", "显示设置（字号/原文/对照/主题/透明度）",
                                        self._open_display_popup)
        self.btn_quit = self._tb_btn("❌ 退出", "关闭程序", self.request_quit.emit)

        # 语言切换按钮（下拉菜单）
        self.btn_lang = QToolButton(self._toolbar)
        self.btn_lang.setText("🌐 中文")
        self.btn_lang.setToolTip("切换翻译目标语言")
        self.btn_lang.setCursor(Qt.PointingHandCursor)
        self.btn_lang.setPopupMode(QToolButton.InstantPopup)
        self.btn_lang.setStyleSheet(
            "QToolButton { color: white; background: rgba(255,255,255,20);"
            " border: 1px solid rgba(255,255,255,30);"
            " border-radius: 4px; padding: 4px 10px; }"
            "QToolButton:hover { background: rgba(255,255,255,40); }"
            "QToolButton::menu-indicator { image: none; }"
        )
        self._lang_menu = QMenu(self.btn_lang)
        self.btn_lang.setMenu(self._lang_menu)
        self._lang_pairs = [("zh", "中文"), ("en", "英语"), ("ja", "日语"), ("ko", "韩语")]

        # 低频按钮收进"⋯更多"菜单（工具栏只保留常用项，小窗口不挤爆）
        self.btn_more = QToolButton(self._toolbar)
        self.btn_more.setText("⋯ 更多")
        self.btn_more.setToolTip("复制 / 历史 / 生词本 / 朗读 / 穿透")
        self.btn_more.setCursor(Qt.PointingHandCursor)
        self.btn_more.setPopupMode(QToolButton.InstantPopup)
        self.btn_more.setStyleSheet(self.btn_lang.styleSheet())
        self.btn_more.setStyleSheet(
            "QToolButton { color: white; background: rgba(255,255,255,20);"
            " border: 1px solid rgba(255,255,255,30);"
            " border-radius: 4px; padding: 4px 10px; }"
            "QToolButton:hover { background: rgba(255,255,255,40); }"
            "QToolButton::menu-indicator { image: none; }"
        )
        self._more_menu = QMenu(self.btn_more)
        self._more_menu.addAction("📋 复制原文", self._copy_original)
        self._more_menu.addAction("📋 复制译文", self._copy_translated)
        self._more_menu.addAction("🔊 朗读原文", self._speak_original)
        self._more_menu.addAction("🔊 朗读译文", self._speak_translated)
        self._more_menu.addSeparator()
        self._more_menu.addAction("⌚ 最近字幕…", self.request_history.emit)
        self._more_menu.addAction("📚 生词本…", self._open_vocab)
        self._more_menu.addSeparator()
        self.act_click_through = self._more_menu.addAction("📌 点击穿透")
        self.act_click_through.setCheckable(True)
        self.act_click_through.toggled.connect(
            lambda on: self.request_toggle_click_through.emit())
        self.act_rescan = self._more_menu.addAction("🔁 重新识别",
                                                    self.request_rescan.emit)
        self.btn_more.setMenu(self._more_menu)

        # 状态指示: 翻译中 spinner + 引擎状态灯
        self._spinner = QLabel("", self._toolbar)
        self._spinner.setStyleSheet("color: #e8b34b; font-size: 15px; padding: 0 2px;")
        self._spinner.setToolTip("正在翻译…")
        self._spinner.hide()
        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(120)
        self._spin_timer.timeout.connect(self._spin_step)
        self._spin_frame = 0
        self._spin_frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

        self._engine_dot = QLabel("●", self._toolbar)
        self._engine_dot.setStyleSheet("color: #888; font-size: 13px; padding: 0 2px;")
        self._engine_dot.setToolTip("翻译引擎状态")
        self._engine_dot.hide()

        # 多区域切换按钮 1/2/3（多区域模式才显示）
        self._region_chips = []
        for i in range(3):
            chip = QToolButton(self._toolbar)
            chip.setText(str(i + 1))
            chip.setToolTip(f"查看区域 {i + 1}")
            chip.setCursor(Qt.PointingHandCursor)
            chip.setFixedSize(26, 26)
            chip.setStyleSheet(
                "QToolButton { color: white; background: rgba(255,255,255,20);"
                " border: 1px solid rgba(255,255,255,30); border-radius: 4px; }"
                "QToolButton:hover { background: rgba(255,255,255,40); }")
            chip.clicked.connect(
                lambda checked, idx=i: self.request_switch_region.emit(idx))
            chip.hide()
            self._region_chips.append(chip)

        tb.addWidget(self.btn_select)
        tb.addWidget(self.btn_pause)
        tb.addWidget(self.btn_edit)
        tb.addWidget(self.btn_display)
        tb.addWidget(self.btn_lang)
        for chip in self._region_chips:
            tb.addWidget(chip)
        tb.addWidget(self._spinner)
        tb.addWidget(self._engine_dot)
        tb.addWidget(self.btn_more)
        tb.addStretch(1)
        tb.addWidget(self.btn_quit)
        self._paused = False
        self._trans_token = 0  # "翻译中"提示的代数（译文到达后失效）
        # 显示偏好状态（由 main 用配置初始化）
        self._show_original = True
        self._orig_font_size = 14
        self._trans_font_size = 16
        self._display_mode = "block"   # block / interleave
        self._theme = "dark"           # dark / light
        self._tts_rate = 0             # 整段朗读语速 -5~5
        self._click_through = False
        # 实际生效的全局热键名（main 注册后回填）
        self._ct_hotkey = "Ctrl+Alt+T"
        self._rs_hotkey = "Ctrl+Alt+R"
        # 窗口几何持久化（拖动/缩放后防抖保存）
        self._geo_timer = QTimer(self)
        self._geo_timer.setSingleShot(True)
        self._geo_timer.setInterval(800)
        self._geo_timer.timeout.connect(self._emit_geometry)

        # placeholder
        self._placeholder = QLabel("等待屏幕识别…", self._frame)
        self._placeholder.setStyleSheet("color: #888; font-style: italic;")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setFont(QFont("Microsoft YaHei UI", 10))

        # 原文（可右键拖选查词、双击打开编辑器）
        self._original_view = _SelectBrowser(
            self._frame,
            on_lookup=self._lookup_selection,
            on_context_menu=self._on_original_context_menu,
            on_double_click=self.open_editor,
        )
        self._original_view.setStyleSheet(
            "QTextBrowser { background: transparent; border: none; color: #cccccc; }"
        )
        # 自动换行 + 关闭横向滚动条：长文本只往纵向扩展（纵向出滚动条）
        self._original_view.setWordWrapMode(QTextOption.WordWrap)
        self._original_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # 译文（右键拖选查词 + 单词高亮链接）
        self._translated_view = _SelectBrowser(
            self._frame,
            on_lookup=self._lookup_selection,
            on_context_menu=self._on_text_context_menu,
            on_double_click=self.open_editor,
        )
        self._translated_view.setOpenExternalLinks(False)
        self._translated_view.setOpenLinks(False)
        self._translated_view.setStyleSheet(
            "QTextBrowser { background: transparent; border: none; color: white; }"
            "a { color: #6ab7ff; text-decoration: none; }"
            "a:hover { text-decoration: underline; }"
        )
        self._translated_view.setFont(QFont("Microsoft YaHei UI", 12, QFont.Bold))
        self._translated_view.setWordWrapMode(QTextOption.WordWrap)
        self._translated_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._translated_view.viewport().setCursor(Qt.ArrowCursor)
        self._translated_view.anchorClicked.connect(self._on_word_anchor_clicked)

        # 布局
        layout = QVBoxLayout(self._frame)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(2)
        layout.addWidget(self._toolbar)
        layout.addWidget(self._placeholder)
        layout.addWidget(self._original_view, 1)
        layout.addWidget(self._translated_view, 2)

        # 右下角尺寸手柄：窗口可自由拖动大小
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.addStretch(1)
        self._size_grip = QSizeGrip(self._frame)
        self._size_grip.setFixedSize(16, 16)
        self._size_grip.setToolTip("拖动调整窗口大小")
        bottom.addWidget(self._size_grip)
        layout.addLayout(bottom)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._frame)
        self.setLayout(outer)

        self._placeholder.show()
        self._original_view.hide()
        self._translated_view.hide()

        self.resize(820, 240)
        self._move_to_default_position()
        self._update_min_size()

        self._current_original = ""
        self._current_translated = ""

    # ===== 工具栏按钮工厂 =====
    def _tb_btn(self, text: str, tip: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip(tip)
        b.clicked.connect(slot)
        return b

    # ===== 公共接口 =====
    def set_font_sizes(self, orig_size: int, trans_size: int):
        """分别设置原文/译文字号。"""
        orig_size = max(8, int(orig_size))
        trans_size = max(8, int(trans_size))
        self._orig_font_size = orig_size
        self._trans_font_size = trans_size
        self._original_view.setFont(QFont("Microsoft YaHei UI", orig_size))
        self._translated_view.setFont(
            QFont("Microsoft YaHei UI", trans_size, QFont.Bold))
        # 工具栏按钮按译文字号缩放
        fb = QFont("Microsoft YaHei UI", max(9, int(trans_size * 0.65)))
        for b in self._toolbar.findChildren(QPushButton):
            b.setFont(fb)
        self.btn_lang.setFont(fb)
        # 字号变了按钮尺寸也变，重算最小窗口尺寸
        self._update_min_size()

    def set_show_original(self, show: bool):
        """设置是否显示原文（并即时应用到当前内容）。"""
        self._show_original = bool(show)
        self._apply_show_original()

    def _apply_show_original(self):
        if self._display_mode == "interleave":
            # 对照模式下原文/译文合并在译文区渲染
            self._original_view.hide()
            return
        if self._current_original and self._show_original:
            self._original_view.show()
        else:
            self._original_view.hide()

    # ===== 显示偏好（apply_prefs 由 main 用配置整体初始化） =====
    def apply_prefs(self, prefs: dict):
        """启动时按配置批量应用显示偏好（不回发信号）。"""
        self.set_font_sizes(
            prefs.get("font_size_original", 18), prefs.get("font_size", 22))
        self._show_original = bool(prefs.get("show_original", True))
        self.set_display_mode(prefs.get("display_mode", "block"))
        self.set_theme(prefs.get("theme", "dark"))
        self.set_opacity(float(prefs.get("window_opacity", 0.92)))
        self._tts_rate = int(prefs.get("tts_rate", 0))

    def set_display_mode(self, mode: str):
        """显示模式: block=分块 / interleave=双语逐行对照。"""
        self._display_mode = "interleave" if mode == "interleave" else "block"
        # 重新渲染当前内容
        if self._current_original or self._current_translated:
            self.show_text(self._current_original, self._current_translated)
        else:
            self._apply_show_original()

    def set_theme(self, theme: str):
        """字幕主题: dark / light。"""
        self._theme = "light" if theme == "light" else "dark"
        self._apply_theme()

    def _apply_theme(self):
        if self._theme == "light":
            self._frame.setStyleSheet(
                "QFrame#SubFrame {"
                "  background-color: rgba(248, 248, 248, 242);"
                "  border: 1px solid rgba(0,0,0,40);"
                "  border-radius: 10px;"
                "}"
            )
            self._toolbar.setStyleSheet(
                "QFrame { background: transparent; }"
                "QPushButton { color: #222; background: rgba(0,0,0,12);"
                " border: 1px solid rgba(0,0,0,30);"
                " border-radius: 4px; padding: 4px 10px; }"
                "QPushButton:hover { background: rgba(0,0,0,24); }"
                "QPushButton:pressed { background: rgba(45,125,246,120); }"
            )
            for w in (self.btn_lang, self.btn_more):
                w.setStyleSheet(
                    "QToolButton { color: #222; background: rgba(0,0,0,12);"
                    " border: 1px solid rgba(0,0,0,30);"
                    " border-radius: 4px; padding: 4px 10px; }"
                    "QToolButton:hover { background: rgba(0,0,0,24); }"
                    "QToolButton::menu-indicator { image: none; }")
            for chip in self._region_chips:
                chip.setStyleSheet("")
            # chips 样式交给 set_region_chips（含激活态高亮，避免覆盖丢失）
            count, active = getattr(self, "_chips_state", (0, -1))
            if count:
                self.set_region_chips(count, active)
            self._original_view.setStyleSheet(
                "QTextBrowser { background: transparent; border: none; color: #555555; }")
            self._translated_view.setStyleSheet(
                "QTextBrowser { background: transparent; border: none; color: #111111; }"
                "a { color: #1f66d0; text-decoration: none; }"
                "a:hover { text-decoration: underline; }")
            self._grip.setStyleSheet("color: #999; font-size: 14px; padding: 0 4px;")
        else:
            self._frame.setStyleSheet(
                "QFrame#SubFrame {"
                "  background-color: rgba(20, 20, 20, 220);"
                "  border: 1px solid rgba(255,255,255,40);"
                "  border-radius: 10px;"
                "}"
            )
            self._toolbar.setStyleSheet(
                "QFrame { background: transparent; }"
                "QPushButton { color: white; background: rgba(255,255,255,20);"
                " border: 1px solid rgba(255,255,255,30);"
                " border-radius: 4px; padding: 4px 10px; }"
                "QPushButton:hover { background: rgba(255,255,255,40); }"
                "QPushButton:pressed { background: rgba(45,125,246,180); }"
            )
            for w in (self.btn_lang, self.btn_more):
                w.setStyleSheet(
                    "QToolButton { color: white; background: rgba(255,255,255,20);"
                    " border: 1px solid rgba(255,255,255,30);"
                    " border-radius: 4px; padding: 4px 10px; }"
                    "QToolButton:hover { background: rgba(255,255,255,40); }"
                    "QToolButton::menu-indicator { image: none; }")
            for chip in self._region_chips:
                chip.setStyleSheet("")
            count, active = getattr(self, "_chips_state", (0, -1))
            if count:
                self.set_region_chips(count, active)
            self._original_view.setStyleSheet(
                "QTextBrowser { background: transparent; border: none; color: #cccccc; }")
            self._translated_view.setStyleSheet(
                "QTextBrowser { background: transparent; border: none; color: white; }"
                "a { color: #6ab7ff; text-decoration: none; }"
                "a:hover { text-decoration: underline; }")
            self._grip.setStyleSheet("color: #999; font-size: 14px; padding: 0 4px;")
        # 当前内容重新渲染以套用新颜色
        if self._current_original or self._current_translated:
            self.show_text(self._current_original, self._current_translated)

    def set_hotkey_names(self, clickthrough: str, rescan: str):
        """main 注册热键后回填实际生效的组合键（Ctrl+Alt+T 被占用时可能回退）。"""
        if clickthrough:
            self._ct_hotkey = clickthrough
            self.act_click_through.setText(f"📌 点击穿透 ({clickthrough})")
        if rescan:
            self._rs_hotkey = rescan
            self.act_rescan.setText(f"🔁 重新识别 ({rescan})")

    def set_click_through(self, on: bool):
        """鼠标点击穿透: 点击直接落到下层窗口（托盘/热键关闭）。

        必须用原生窗口标志 Qt.WindowTransparentForInput（Windows 上映射为
        WS_EX_TRANSPARENT|WS_EX_LAYERED）。仅设 WA_TransparentForMouseEvents
        只会让 Qt 忽略事件，点击仍被窗口截住不往下传——穿透"没生效"的根因。
        改窗口标志会重建原生窗口：先 hide 再改再 show，且不能在菜单
        triggered 事件栈内直接调用（main 侧已用 singleShot 延迟）。
        """
        self._click_through = bool(on)
        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        if self._click_through:
            flags |= Qt.WindowTransparentForInput
        self.hide()
        self.setWindowFlags(flags)
        # 注意: 不要再 setAttribute(WA_TransparentForMouseEvents)——该属性
        # 在窗口层面会同步改写 WindowTransparentForInput 标志，把刚去除的
        # flag 又加回来，导致穿透关不掉；原生 flag 本身已足够穿透。
        # 阻止 setChecked 再触发 toggled -> request_toggle_click_through 死循环
        self.act_click_through.blockSignals(True)
        self.act_click_through.setChecked(self._click_through)
        self.act_click_through.blockSignals(False)
        self.show()  # setWindowFlags 隐藏了窗口，重新显示
        if self._click_through:
            self._toast(f"已开启点击穿透\n关闭: {self._ct_hotkey} 或托盘右键菜单")
        else:
            self._toast("已关闭点击穿透")

    def set_engine_status(self, state: str, tip: str = ""):
        """引擎状态灯: ok=绿 pending=黄 off=灰。"""
        color = {"ok": "#34c759", "pending": "#e8b34b"}.get(state, "#888888")
        self._engine_dot.setStyleSheet(
            f"color: {color}; font-size: 13px; padding: 0 2px;")
        self._engine_dot.setToolTip(tip or "翻译引擎状态")
        self._engine_dot.show()

    # ===== 翻译中 spinner =====
    def set_busy(self, on: bool):
        if on:
            self._spinner.show()
            self._spin_timer.start()
        else:
            self._spin_timer.stop()
            self._spinner.hide()
            self._spinner.setText("")

    def _spin_step(self):
        self._spin_frame = (self._spin_frame + 1) % len(self._spin_frames)
        self._spinner.setText(self._spin_frames[self._spin_frame])

    # ===== 多区域切换 chips =====
    def set_region_chips(self, count: int, active: int):
        """多区域模式下显示 1/2/3 切换按钮。"""
        self._chips_state = (count, active)   # 供主题切换后重刷样式
        colors = ["#ff4d4d", "#ff9f0a", "#34c759"]
        for i, chip in enumerate(self._region_chips):
            if i < count:
                chip.show()
                if i == active:
                    chip.setStyleSheet(
                        f"QToolButton {{ color: white; background: {colors[i]};"
                        " border: none; border-radius: 4px; }")
                elif self._theme == "light":
                    # 浅色主题: 白色半透明底在浅色字幕窗上看不见，换深色系
                    chip.setStyleSheet(
                        "QToolButton { color: #111111; background: rgba(0,0,0,15);"
                        " border: 1px solid rgba(0,0,0,40); border-radius: 4px; }"
                        "QToolButton:hover { background: rgba(0,0,0,30); }")
                else:
                    chip.setStyleSheet(
                        "QToolButton { color: white; background: rgba(255,255,255,20);"
                        " border: 1px solid rgba(255,255,255,30); border-radius: 4px; }"
                        "QToolButton:hover { background: rgba(255,255,255,40); }")
            else:
                chip.hide()

    # ===== 整段朗读 =====
    def _speak_original(self):
        text = self._current_original.strip()
        if not text:
            self._toast("没有可朗读的原文")
            return
        if not tts.speak(text, rate=self._tts_rate):
            self._toast("朗读失败: " + (tts.error_message() or "本机无 TTS 引擎"))

    def _speak_translated(self):
        text = self._current_translated.strip()
        if not text:
            return
        if not tts.speak(text, rate=self._tts_rate):
            self._toast("朗读失败: " + (tts.error_message() or "本机无 TTS 引擎"))

    def _toast(self, msg: str):
        """光标处浮动小提示（不抢焦点）。"""
        from PyQt5.QtWidgets import QToolTip
        QToolTip.showText(QCursor.pos(), msg, self)

    def _text_color(self) -> str:
        return "#111111" if self._theme == "light" else "white"

    def _orig_color(self) -> str:
        return "#777777" if self._theme == "light" else "#9a9a9a"

    def _interleave_html(self, original: str, translated: str) -> str:
        """双语逐行对照 HTML。

        行数一一对应时逐行交错（原文/译文交替）；
        行数不齐时退化为 整块原文 + 整块译文。
        """
        esc = lambda s: (s.replace("&", "&amp;").replace("<", "&lt;")
                         .replace(">", "&gt;"))
        orig_lines = [l for l in (original or "").splitlines() if l.strip()]
        trans_lines = [l for l in (translated or "").splitlines() if l.strip()]
        oc, tc = self._orig_color(), self._text_color()
        osz, tsz = self._orig_font_size, self._trans_font_size
        parts = []
        if len(orig_lines) == len(trans_lines) and orig_lines:
            for o, t in zip(orig_lines, trans_lines):
                parts.append(
                    f"<div style='color:{oc}; font-size:{osz}pt;"
                    f" margin-top:5px;'>{esc(o)}</div>"
                    f"<div style='color:{tc}; font-size:{tsz}pt;"
                    f" font-weight:bold;'>{esc(t)}</div>")
        else:
            parts.append(
                f"<div style='color:{oc}; font-size:{osz}pt;'>"
                + "<br>".join(esc(l) for l in orig_lines) + "</div>"
                f"<div style='color:{tc}; font-size:{tsz}pt; font-weight:bold;'>"
                + "<br>".join(esc(l) for l in trans_lines) + "</div>")
        return "".join(parts)

    # ===== 显示设置弹窗（字号滑动条 + 显示原文开关） =====
    #
    # 注意: 这里不能用 Qt.Popup 标志（之前版本用了 Qt.Popup + dlg.popup()，
    # Popup 抓取鼠标的机制与 QDialog 的模态处理冲突，实测点击后主程序
    # 卡死数秒后闪退）。Qt.Tool + show() + 失活自动关闭是安全等价做法。
    def _open_display_popup(self):
        # 已打开就不再叠加
        old = getattr(self, "_pref_dlg", None)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        dlg = QDialog(self, Qt.Tool | Qt.FramelessWindowHint
                      | Qt.WindowStaysOnTopHint)
        dlg.setStyleSheet(
            "QDialog { background: rgba(30,30,30,245); border-radius: 8px; }"
            "QLabel { color: white; }"
            "QCheckBox { color: white; }"
            "QSlider::groove:horizontal { height: 4px; background: rgba(255,255,255,60);"
            " border-radius: 2px; }"
            "QSlider::handle:horizontal { width: 14px; margin: -6px 0;"
            " background: #2d7df6; border-radius: 7px; }"
        )
        v = QVBoxLayout(dlg)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(8)

        cb = QCheckBox("显示原文", dlg)
        cb.setChecked(self._show_original)
        cb.toggled.connect(self._on_pref_changed)
        v.addWidget(cb)

        cb_il = QCheckBox("双语逐行对照（原文译文逐行交错）", dlg)
        cb_il.setChecked(self._display_mode == "interleave")
        cb_il.toggled.connect(self._on_pref_changed)
        v.addWidget(cb_il)

        cb_theme = QCheckBox("深色主题（取消为浅色）", dlg)
        cb_theme.setChecked(self._theme != "light")
        cb_theme.toggled.connect(self._on_pref_changed)
        v.addWidget(cb_theme)

        def font_row(label, value, lo=8, hi=40):
            row = QHBoxLayout()
            lab = QLabel(label, dlg)
            lab.setFixedWidth(80)
            s = QSlider(Qt.Horizontal, dlg)
            s.setRange(lo, hi)
            s.setValue(int(value))
            num = QLabel(str(int(value)), dlg)
            num.setFixedWidth(28)
            s.valueChanged.connect(lambda val: num.setText(str(val)))
            s.valueChanged.connect(self._on_pref_changed)
            row.addWidget(lab)
            row.addWidget(s, 1)
            row.addWidget(num)
            v.addLayout(row)
            return s

        self._slider_orig = font_row("原文字号", self._orig_font_size)
        self._slider_trans = font_row("译文字号", self._trans_font_size)
        self._slider_opacity = font_row("不透明度%",
                                        int(self.windowOpacity() * 100), 30, 100)
        self._slider_rate = font_row("朗读语速", self._tts_rate, -5, 5)
        self._pref_cb = cb
        self._pref_cb_il = cb_il
        self._pref_cb_theme = cb_theme
        self._pref_dlg = dlg
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.installEventFilter(self)
        # 定位到按钮下方，并夹在屏幕范围内
        dlg.adjustSize()
        pos = self.btn_display.mapToGlobal(self.btn_display.rect().bottomLeft())
        screen = QApplication.screenAt(pos)
        if screen is None:
            screen = QApplication.primaryScreen()
        avail = screen.availableGeometry()
        pos.setX(max(avail.left(), min(pos.x(), avail.right() - dlg.width())))
        pos.setY(max(avail.top(), min(pos.y(), avail.bottom() - dlg.height())))
        dlg.move(pos)
        dlg.show()   # 不用 exec_()：非模态，点别处自动关闭

    def eventFilter(self, obj, ev):
        dlg = getattr(self, "_pref_dlg", None)
        if dlg is not None and obj is dlg:
            # 窗口失活（点击程序外其他地方）自动关闭
            if ev.type() == QEvent.WindowDeactivate:
                dlg.close()
            elif ev.type() == QEvent.Close:
                self._pref_cb = None
                self._pref_cb_il = None
                self._pref_cb_theme = None
                self._slider_orig = None
                self._slider_trans = None
                self._slider_opacity = None
                self._slider_rate = None
                self._pref_dlg = None
        return super().eventFilter(obj, ev)

    def _on_pref_changed(self, *_):
        cb = getattr(self, "_pref_cb", None)
        s1 = getattr(self, "_slider_orig", None)
        s2 = getattr(self, "_slider_trans", None)
        if cb is None or s1 is None or s2 is None:
            return  # 弹窗尚未创建（测试直接调用时）
        show = cb.isChecked()
        o, t = s1.value(), s2.value()
        mode = "interleave" if getattr(self, "_pref_cb_il", None) and self._pref_cb_il.isChecked() else "block"
        theme = "dark" if getattr(self, "_pref_cb_theme", None) and self._pref_cb_theme.isChecked() else "light"
        op = getattr(self, "_slider_opacity", None)
        rate = getattr(self, "_slider_rate", None)
        opacity = (op.value() / 100.0) if op else self.windowOpacity()
        tts_rate = rate.value() if rate else self._tts_rate
        self.set_font_sizes(o, t)
        self.set_show_original(show)
        self.set_display_mode(mode)
        self.set_theme(theme)
        self.set_opacity(opacity)
        self._tts_rate = int(tts_rate)
        self.request_display_prefs.emit({
            "show_original": show,
            "font_size_original": int(o),
            "font_size": int(t),
            "display_mode": mode,
            "theme": theme,
            "window_opacity": float(opacity),
            "tts_rate": int(tts_rate),
        })

    def set_opacity(self, op: float):
        self.setWindowOpacity(max(0.2, min(1.0, op)))

    # ===== 语言切换按钮 =====
    def set_languages(self, pairs, current: str):
        """填充语言下拉菜单。pairs: [(code, 中文名), ...]"""
        self._lang_pairs = list(pairs) or [("zh", "中文")]
        self._lang_menu.clear()
        for code, name in self._lang_pairs:
            mark = "● " if code == current else "　"
            act = self._lang_menu.addAction(mark + name)
            act.setData(code)
            act.triggered.connect(
                lambda checked, c=code, n=name: self.request_set_target_lang.emit(c))
        self.set_current_lang(current)

    def set_current_lang(self, code: str):
        """更新按钮上显示的当前语言。"""
        for c, n in self._lang_pairs:
            if c == code:
                self.btn_lang.setText(f"🌐 {n}")
                return
        self.btn_lang.setText(f"🌐 {code}")

    def show_text(self, original: str, translated: str, show_original: bool = None):
        if show_original is not None:
            self._show_original = bool(show_original)
        self._trans_token += 1  # 使"翻译中"的慢速提示失效
        self.set_busy(False)
        if (not original and not translated) or self._paused:
            self._placeholder.show()
            self._original_view.hide()
            self._translated_view.hide()
            return
        self._placeholder.hide()
        self._current_original = original or ""
        self._current_translated = translated or ""
        if (self._display_mode == "interleave" and self._show_original
                and self._current_original and self._current_translated
                and self._current_translated != "(无译文)"):
            # 双语逐行对照: 原文灰小字在上、译文在下逐行交错
            self._original_view.hide()
            self._translated_view.setHtml(
                self._interleave_html(self._current_original,
                                      self._current_translated))
            self._translated_view.show()
        else:
            if self._show_original and self._current_original:
                self._original_view.setPlainText(self._current_original)
                self._original_view.show()
            else:
                self._original_view.hide()
            # 译文用纯文本渲染。之前这里会给未翻译的英文词加蓝色可点击链接
            # （单击查词典），游戏里极易误触弹出字典窗口，已移除；
            # 查词/朗读/生词本仍可通过右键菜单使用。
            plain = (self._current_translated or "(无译文)")
            plain = (plain.replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;").replace("\n", "<br>"))
            html = f"<div style='color:{self._text_color()};'>{plain}</div>"
            self._translated_view.setHtml(html)
            self._translated_view.show()
        # 内容过长由纵向滚动条处理，不再自动改变窗口大小

        # 入历史（去重）
        if self._current_original or self._current_translated:
            self._record_history(self._current_original, self._current_translated)

    def show_translating(self, original: str, show_original: bool = True):
        """OCR 一命中就立即显示原文 + "翻译中"，避免长时间无反馈像死机。

        译文到达后由 show_text 覆盖。中间态不入历史。
        """
        if self._paused:
            return
        self.set_busy(True)
        self._placeholder.hide()
        self._current_original = original or ""
        if self._display_mode == "interleave":
            self._original_view.hide()
            esc = (self._current_original.replace("&", "&amp;")
                   .replace("<", "&lt;").replace(">", "&gt;")
                   .replace("\n", "<br>"))
            self._translated_view.setHtml(
                f"<div style='color:{self._orig_color()};"
                f"font-size:{self._orig_font_size}pt;'>{esc}</div>"
                "<div style='color:#e8b34b;'>⏳ 翻译中…（首次加载离线模型约需 10~30 秒）</div>"
            )
        else:
            if self._show_original and self._current_original:
                self._original_view.setPlainText(self._current_original)
                self._original_view.show()
            else:
                self._original_view.hide()
            self._translated_view.setHtml(
                "<div style='color:#e8b34b;'>⏳ 翻译中…（首次加载离线模型约需 10~30 秒）</div>"
            )
        self._translated_view.show()
        # 超过 15 秒还没出结果，提示一次"仅首次慢"，避免误以为无响应。
        # 用父属 QTimer 而非 singleShot+lambda: 后者强引用 self，
        # 关窗后 15s 内无法释放（P2 #5）；timer 随窗口销毁自动失效
        self._trans_token += 1
        token = self._trans_token
        hint_timer = QTimer(self)
        hint_timer.setSingleShot(True)
        hint_timer.timeout.connect(lambda: self._slow_hint(token))
        hint_timer.timeout.connect(hint_timer.deleteLater)
        hint_timer.start(15000)

    def _slow_hint(self, token: int):
        if token == self._trans_token and not self._paused:
            self._translated_view.setHtml(
                "<div style='color:#e8b34b;'>⏳ 仍在翻译…首次加载离线模型较慢（仅此一次），"
                "之后会秒出结果。</div>"
            )

    def set_hint(self, msg: str):
        """显示提示信息（如"未识别到文字"），用于给用户即时状态反馈。"""
        if self._paused:
            return
        self.set_busy(False)
        self._placeholder.setText(msg)
        self._placeholder.setStyleSheet(
            "color: #e8b34b; font-style: italic; font-size: 11px;")
        self._placeholder.show()
        self._original_view.hide()
        self._translated_view.hide()

    def clear(self):
        self.set_busy(False)
        self._placeholder.setText("等待屏幕识别…")
        self._placeholder.setStyleSheet("color: #888; font-style: italic;")
        self._placeholder.show()
        self._original_view.hide()
        self._translated_view.hide()
        self._current_original = ""
        self._current_translated = ""

    # ===== 历史记录 =====
    def _record_history(self, original: str, translated: str):
        key = (original or "") + "||" + (translated or "")
        # 顶掉重复
        if self._history and self._history[-1].get("key") == key:
            return
        self._history.append({
            "key": key,
            "original": original,
            "translated": translated,
        })

    def get_history(self) -> list[dict]:
        return list(self._history)

    def show_history_window(self):
        """弹出最近字幕窗口。"""
        dlg = _HistoryDialog(self._history, self)
        dlg.exec_()

    # ===== 工具栏动作 =====
    def _toggle_pause(self):
        self._paused = not self._paused
        self.btn_pause.setText("▶ 继续" if self._paused else "⏸ 暂停")
        # 必须通知 main: 否则轮询照跑，暂停期间译文被丢弃、恢复后
        # 内容没变去重键相同不触发翻译 -> "恢复后长时间不出结果"
        self.request_toggle_paused.emit(self._paused)
        if self._paused:
            self.set_busy(False)   # spinner 定时器暂停期间别再空转
            self._placeholder.setText("已暂停（点击继续）")
            self._placeholder.setStyleSheet(
                "color: #e8b34b; font-style: italic;")
            self._placeholder.show()
            self._original_view.hide()
            self._translated_view.hide()
        else:
            self._placeholder.setText("等待屏幕识别…")
            self._placeholder.setStyleSheet(
                "color: #888; font-style: italic;")

    def _copy_original(self):
        if not self._current_original:
            return
        QApplication.clipboard().setText(self._current_original)
        self._toast("已复制原文")

    def _copy_translated(self):
        if not self._current_translated:
            return
        QApplication.clipboard().setText(self._current_translated)
        self._toast("已复制译文")

    def _open_vocab(self):
        from vocab_window import VocabWindow
        VocabWindow(self).exec_()

    def open_editor(self):
        """打开"修改识别文本"编辑器（工具栏 ✏编辑 / 双击原文）。"""
        dlg = _EditDialog(self._current_original, self)
        if dlg.exec_() == QDialog.Accepted:
            new = dlg.get_text().strip()
            if new:
                self.request_manual_translate.emit(new)

    def _lookup_selection(self, text: str):
        """右键拖选的文字 -> 查词。单词直接查；词组先试整句，
        ECDICT 查不到时退回第一个英文单词。"""
        text = (text or "").strip().strip(_STRIP_PUNCT)
        if not text or not re.search(r"[A-Za-z]", text):
            return  # 选中的没有英文，不查词典
        if " " in text or "\u2029" in text or "\n" in text:
            d = dict_lookup.Dict.get().lookup(text)
            if d is None:
                m = _WORD_RE.search(text)
                if not m:
                    return
                text = m.group(0)
        self._lookup_word(text)

    def _on_original_context_menu(self, pos: QPoint):
        """右键原文区 - 弹 查词/复制 菜单"""
        cursor = self._original_view.cursorForPosition(pos)
        word = self._word_under_cursor(cursor)
        menu = QMenu(self._original_view)
        if word:
            act = QAction(f"📖 查词  \"{word}\"", menu)
            act.triggered.connect(lambda: self._lookup_word(word))
            menu.addAction(act)
            act_spk = QAction(f"🔊 朗读  \"{word}\"", menu)
            act_spk.triggered.connect(lambda: tts.speak(word))
            menu.addAction(act_spk)
            menu.addSeparator()
        act_co = QAction("📋 复制原文", menu)
        act_co.triggered.connect(self._copy_original)
        menu.addAction(act_co)
        act_ed = QAction("✏ 修改识别文本…", menu)
        act_ed.triggered.connect(self.open_editor)
        menu.addAction(act_ed)
        menu.exec_(self._original_view.mapToGlobal(pos))

    # ===== 鼠标交互 =====
    def _on_word_anchor_clicked(self, url):
        """用户点了 QTextBrowser 里的链接: dict://word"""
        word = url.toString().replace("dict://", "")
        if word:
            self._lookup_word(word)

    def _on_text_context_menu(self, pos: QPoint):
        """右键译文 - 弹 查词/朗读/加入生词本/复制 菜单"""
        global_pos = self._translated_view.mapToGlobal(pos)
        cursor = self._translated_view.cursorForPosition(
            self._translated_view.mapFromGlobal(global_pos)
        )
        word = self._word_under_cursor(cursor)
        menu = QMenu(self._translated_view)
        if word:
            act = QAction(f"📖 查词  \"{word}\"", menu)
            act.triggered.connect(lambda: self._lookup_word(word))
            menu.addAction(act)

            act_spk = QAction(f"🔊 朗读  \"{word}\"", menu)
            act_spk.triggered.connect(lambda: tts.speak(word))
            menu.addAction(act_spk)

            act_vb = QAction(f"+ 加入生词本  \"{word}\"", menu)
            act_vb.triggered.connect(lambda: self._add_to_vocab(word))
            menu.addAction(act_vb)

            menu.addSeparator()
            act_cw = QAction("复制单词", menu)
            act_cw.triggered.connect(lambda: QApplication.clipboard().setText(word))
            menu.addAction(act_cw)

            menu.addSeparator()

        act_co = QAction("📋 复制原文", menu)
        act_co.triggered.connect(self._copy_original)
        menu.addAction(act_co)
        act_ct = QAction("📋 复制译文", menu)
        act_ct.triggered.connect(self._copy_translated)
        menu.addAction(act_ct)
        menu.addSeparator()

        act_oh = QAction("⌚ 查看最近字幕", menu)
        act_oh.triggered.connect(self.show_history_window)
        menu.addAction(act_oh)
        act_vb = QAction("📚 打开生词本", menu)
        act_vb.triggered.connect(self._open_vocab)
        menu.addAction(act_vb)
        menu.addSeparator()

        act_q = QAction("❌ 退出程序", menu)
        act_q.triggered.connect(self.request_quit.emit)
        menu.addAction(act_q)

        menu.exec_(global_pos)

    def _word_under_cursor(self, cursor: QTextCursor) -> Optional[str]:
        if cursor is None or cursor.isNull():
            return None
        block_text = cursor.block().text()
        pos_in_block = cursor.positionInBlock()
        if not block_text or pos_in_block < 0 or pos_in_block >= len(block_text):
            return None
        start = pos_in_block
        while start > 0 and (block_text[start - 1].isalpha() or block_text[start - 1] in "'-"):
            start -= 1
        end = pos_in_block
        while end < len(block_text) and (block_text[end].isalpha() or block_text[end] in "'-"):
            end += 1
        word = block_text[start:end].strip("'-")
        if not word or not word[0].isalpha():
            return None
        return word

    def _lookup_word(self, word: str):
        from dict_window import DictWindow
        w = DictWindow(word, self)
        w.exec_()

    def _add_to_vocab(self, word: str):
        d = dict_lookup.Dict.get().lookup(word)
        v = vocab.Vocab.get()
        v.add_or_update(
            word,
            phonetic=d.get("phonetic", "") if d else "",
            translation=d.get("translation", "") if d else "",
            pos=" · ".join(d.get("pos", []) if d else []),
            definition=d.get("definition", "") if d else "",
        )
        QMessageBox_quick(self, "已加入生词本", f"「{word}」已加入生词本")

    # ===== 尺寸管理 =====
    def _update_min_size(self):
        """最小窗口尺寸 = 工具栏所有按钮完整展示所需的大小。

        用户可以把窗口拖大，但不能拖到按钮被裁掉。
        """
        try:
            hint = self._toolbar.minimumSizeHint()
            frm = self._frame.layout().contentsMargins()
            min_w = hint.width() + frm.left() + frm.right() + 12
            min_h = (hint.height() + frm.top() + frm.bottom()
                     + 90)  # 工具栏 + 尺寸手柄 + 至少两三行文本
            self.setMinimumSize(max(480, int(min_w)), max(150, int(min_h)))
        except Exception:
            self.setMinimumSize(480, 150)

    def _move_to_default_position(self):
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x = geo.x() + (geo.width() - self.width()) // 2
        y = geo.y() + geo.height() - self.height() - 60
        self.move(x, y)

    # ===== 拖动 =====
    # 左键按住任意非交互区域（工具栏空白、拖动柄、边框）即可拖动窗口。
    # 按钮/译文区自己消费鼠标事件，不受影响。
    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._drag_pos is not None and (event.buttons() & Qt.LeftButton):
            self.move(event.globalPos() - self._drag_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._drag_pos = None
            self._snap_to_screen_edge()
        super().mouseReleaseEvent(event)

    def _snap_to_screen_edge(self, threshold: int = 24):
        """拖动结束时贴边吸附（靠近屏幕边缘 24px 内自动贴合）。"""
        try:
            center = self.frameGeometry().center()
            screen = QApplication.screenAt(center)
            if screen is None:
                screen = QApplication.primaryScreen()
            if screen is None:
                return
            geo = screen.availableGeometry()
            g = self.frameGeometry()
            x, y = g.x(), g.y()
            if abs(g.left() - geo.left()) <= threshold:
                x = geo.left()
            elif abs(g.right() - geo.right()) <= threshold:
                x = geo.right() - self.width() + 1
            if abs(g.top() - geo.top()) <= threshold:
                y = geo.top()
            elif abs(g.bottom() - geo.bottom()) <= threshold:
                y = geo.bottom() - self.height() + 1
            if (x, y) != (g.x(), g.y()):
                self.move(x, y)
        except Exception:
            pass

    # ===== 几何持久化（拖动/缩放后防抖保存） =====
    def moveEvent(self, event):
        self._geo_timer.start()
        super().moveEvent(event)

    def resizeEvent(self, event):
        self._geo_timer.start()
        super().resizeEvent(event)

    def _emit_geometry(self):
        try:
            g = self.frameGeometry()
            if self.isVisible() and g.width() > 50:
                self.request_save_geometry.emit([g.x(), g.y(), g.width(), g.height()])
        except Exception:
            pass

    def restore_geometry(self, geo: Optional[list]):
        """启动时恢复上次窗口位置与大小（校验在任一屏幕范围内）。

        之前只按主屏校验: 窗口在副屏时，启动时序里副屏未就绪/拔掉副屏
        都会让位置白白丢失。改为遍历所有 screen，任一屏覆盖即恢复。
        """
        try:
            if not geo or len(geo) != 4:
                return
            x, y, w, h = (int(v) for v in geo)
            if w < 200 or h < 80 or w > 4000 or h > 3000:
                return
            on_any_screen = False
            for screen in QApplication.screens():
                avail = screen.availableGeometry()
                # 与任一屏幕的可用区域重叠 >= 60px 即恢复
                ow = min(x + w, avail.right()) - max(x, avail.left())
                oh = min(y + h, avail.bottom()) - max(y, avail.top())
                if ow >= 60 and oh >= 60:
                    on_any_screen = True
                    break
            if QApplication.screens() and not on_any_screen:
                return
            self.resize(w, h)
            self.move(x, y)
        except Exception:
            pass


def QMessageBox_quick(parent, title: str, msg: str):
    from PyQt5.QtWidgets import QMessageBox
    QMessageBox.information(parent, title, msg)


class _EditDialog(QDialog):
    """修改 OCR 识别文本，确定后重新翻译。附带 OCR 误识一键反馈。"""
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("修改识别文本")
        self.resize(560, 260)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        tip = QLabel("OCR 识别如有错漏，请直接修改，点击\"重新翻译\"生效：")
        tip.setStyleSheet("color: #555;")
        layout.addWidget(tip)

        self._edit = QPlainTextEdit(self)
        self._edit.setPlainText(text or "")
        self._edit.setFont(QFont("Microsoft YaHei UI", 12))
        layout.addWidget(self._edit, 1)

        row = QHBoxLayout()
        btn_feedback = QPushButton("📮 反馈识别错误")
        btn_feedback.setToolTip("导出诊断包（截图+识别结果+日志）到 logs/feedback，"
                                "方便排查识别问题")
        btn_feedback.clicked.connect(self._send_feedback)
        row.addWidget(btn_feedback)
        row.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton("重新翻译")
        btn_ok.setStyleSheet("QPushButton { background: #2d7df6; color: white;"
                             " border: none; border-radius: 4px; padding: 6px 18px; }"
                             "QPushButton:hover { background: #1f66d0; }")
        btn_ok.clicked.connect(self.accept)
        row.addWidget(btn_cancel)
        row.addWidget(btn_ok)
        layout.addLayout(row)

    def _send_feedback(self):
        p = self.parent()
        if hasattr(p, "request_feedback"):
            p.request_feedback.emit(self._edit.toPlainText())

    def get_text(self) -> str:
        return self._edit.toPlainText()


class _HistoryDialog(QDialog):
    """最近字幕窗口"""
    def __init__(self, history_deque, parent=None):
        from PyQt5.QtWidgets import QDialog, QVBoxLayout, QTextBrowser, QPushButton, QHBoxLayout
        super().__init__(parent)
        self.setWindowTitle("最近字幕")
        self.resize(680, 360)
        self._history = list(history_deque)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        text = QTextBrowser(self)
        text.setOpenExternalLinks(False)
        text.setOpenLinks(False)
        text.setStyleSheet(
            "QTextBrowser { background: #1e1e1e; color: #ddd; border: none; }"
            "a { color: #6ab7ff; }"
        )
        text.setFont(QFont("Microsoft YaHei UI", 11))
        layout.addWidget(text, 1)

        # 渲染历史
        html_parts = []
        if not self._history:
            html_parts.append("<p style='color:#888'>暂无历史字幕</p>")
        else:
            for i, item in enumerate(reversed(self._history), 1):
                o = (item.get("original") or "").replace("<", "&lt;")
                t = item.get("translated") or ""
                t_html = self._highlight(t)
                html_parts.append(
                    f"<div style='margin:6px 0; padding:6px; "
                    f"background:rgba(255,255,255,5%); border-radius:6px;'>"
                    f"<div style='color:#888; font-size:10px;'>第 {i} 条</div>"
                    f"<div style='color:#ccc; margin:2px 0;'>{o}</div>"
                    f"<div style='color:white;'>{t_html}</div>"
                    f"</div>"
                )
        text.setHtml("\n".join(html_parts))

        # 按钮
        btn_row = QHBoxLayout()
        btn_txt = QPushButton("📄 导出 TXT…", self)
        btn_txt.clicked.connect(lambda: self._export("txt"))
        btn_row.addWidget(btn_txt)
        btn_md = QPushButton("📝 导出 Markdown…", self)
        btn_md.clicked.connect(lambda: self._export("md"))
        btn_row.addWidget(btn_md)
        btn_row.addStretch(1)
        btn_close = QPushButton("关闭", self)
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

    def _export(self, fmt: str):
        """历史字幕导出为 TXT / Markdown。"""
        from PyQt5.QtWidgets import QFileDialog, QMessageBox
        if not self._history:
            QMessageBox.information(self, "导出", "暂无历史字幕。")
            return
        default = f"字幕历史.{fmt}"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出历史字幕", default,
            f"{'文本' if fmt == 'txt' else 'Markdown'}文件 (*.{fmt})")
        if not path:
            return
        try:
            lines = []
            if fmt == "md":
                lines.append("# 屏幕翻译历史\n")
            for i, item in enumerate(reversed(self._history), 1):
                o = item.get("original") or ""
                t = item.get("translated") or ""
                if fmt == "md":
                    lines.append(f"## {i}. {t}\n")
                    lines.append(f"> 原文: {o}\n")
                else:
                    lines.append(f"── {i} ──")
                    lines.append(f"原文: {o}")
                    lines.append(f"译文: {t}")
                    lines.append("")
            from pathlib import Path
            Path(path).write_text("\n".join(lines), encoding="utf-8")
            QMessageBox.information(self, "导出完成",
                                    f"已导出 {len(self._history)} 条到:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def _highlight(self, text: str) -> str:
        """高亮历史里的单词。"""
        def repl(m):
            w = m.group(0)
            return f"<a href='dict://{w}' style='color:#6ab7ff;'>{w}</a>"
        return _WORD_RE.sub(repl, (text or "").replace("<", "&lt;"))
