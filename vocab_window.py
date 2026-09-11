"""生词本查看窗口"""
from __future__ import annotations

import time
from typing import List, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QLabel, QFileDialog,
)

from vocab import Vocab
import tts


class VocabWindow(QDialog):
    """生词本列表 + 搜索 + 朗读 + 删除 + 导出 Anki"""

    COL_WORD = 0
    COL_PHONETIC = 1
    COL_TRANSLATION = 2
    COL_COUNT = 3
    COL_LAST = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("生词本")
        self.resize(820, 540)
        self.setMinimumSize(640, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # 顶部统计
        stat_row = QHBoxLayout()
        self.stat_label = QLabel("", self)
        self.stat_label.setStyleSheet("color: #555; font-weight: bold;")
        stat_row.addWidget(self.stat_label)
        stat_row.addStretch(1)
        layout.addLayout(stat_row)

        # 搜索栏
        search_row = QHBoxLayout()
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("搜索单词或释义…")
        self.search_edit.textChanged.connect(self._on_search_changed)
        search_row.addWidget(self.search_edit, 1)
        layout.addLayout(search_row)

        # 表格
        self.table = QTableWidget(self)
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            ["单词", "音标", "翻译", "查询次数", "最近查询"]
        )
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.doubleClicked.connect(self._on_row_dblclick)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(self.COL_WORD, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(self.COL_PHONETIC, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(self.COL_TRANSLATION, QHeaderView.Stretch)
        h.setSectionResizeMode(self.COL_COUNT, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(self.COL_LAST, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)

        # 操作按钮
        btn_row = QHBoxLayout()
        self.btn_speak = QPushButton("🔊 朗读", self)
        self.btn_speak.clicked.connect(self._speak_selected)
        btn_row.addWidget(self.btn_speak)

        self.btn_lookup = QPushButton("📖 查词", self)
        self.btn_lookup.clicked.connect(self._lookup_selected)
        btn_row.addWidget(self.btn_lookup)

        self.btn_remove = QPushButton("🗑 删除", self)
        self.btn_remove.clicked.connect(self._remove_selected)
        btn_row.addWidget(self.btn_remove)

        btn_row.addStretch(1)

        self.btn_export = QPushButton("导出 Anki CSV…", self)
        self.btn_export.clicked.connect(self._export_csv)
        btn_row.addWidget(self.btn_export)

        self.btn_open_folder = QPushButton("打开数据目录", self)
        self.btn_open_folder.clicked.connect(Vocab.get().open_export_folder)
        btn_row.addWidget(self.btn_open_folder)

        layout.addLayout(btn_row)

        # 状态栏
        self.status_label = QLabel("", self)
        self.status_label.setStyleSheet("color: #888; font-size: 10px;")
        layout.addWidget(self.status_label)

        self._refresh()

    def _refresh(self, keyword: str = ""):
        vocab = Vocab.get()
        rows = vocab.search(keyword=keyword, limit=500)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            self._fill_row(r, row)
        self.stat_label.setText(f"共 {vocab.count()} 个生词，当前显示 {len(rows)} 个")
        if not tts.available():
            self.btn_speak.setEnabled(False)
            self.btn_speak.setToolTip("本机未安装离线 TTS 语音引擎")
        else:
            self.btn_speak.setEnabled(True)
            self.btn_speak.setToolTip("朗读选中的单词")

    def _fill_row(self, r: int, row: dict):
        items = [
            QTableWidgetItem(row["word"]),
            QTableWidgetItem(row.get("phonetic") or ""),
            QTableWidgetItem(row.get("translation") or ""),
            QTableWidgetItem(str(row.get("query_count") or 1)),
            QTableWidgetItem(self._fmt_time(row.get("last_queried") or 0)),
        ]
        items[self.COL_WORD].setFont(QFont("Microsoft YaHei UI", 13, QFont.Bold))
        items[self.COL_PHONETIC].setFont(QFont("Microsoft YaHei UI", 12))
        items[self.COL_TRANSLATION].setFont(QFont("Microsoft YaHei UI", 12))
        items[self.COL_PHONETIC].setStyleSheet("color: #2d7df6;")
        for col, it in enumerate(items):
            it.setData(Qt.UserRole, row["word"])
            self.table.setItem(r, col, it)

    def _fmt_time(self, ts: float) -> str:
        if not ts:
            return ""
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
        except Exception:
            return ""

    def _on_search_changed(self, text: str):
        self._refresh(text.strip())

    def _selected_word(self) -> Optional[str]:
        items = self.table.selectedItems()
        if not items:
            return None
        return items[0].data(Qt.UserRole)

    def _speak_selected(self):
        w = self._selected_word()
        if not w:
            return
        tts.speak(w)

    def _lookup_selected(self):
        w = self._selected_word()
        if not w:
            return
        from dict_window import DictWindow
        DictWindow(w, self).exec_()

    def _remove_selected(self):
        w = self._selected_word()
        if not w:
            return
        ret = QMessageBox.question(
            self, "删除生词",
            f"确定要从生词本移除「{w}」？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ret == QMessageBox.Yes:
            Vocab.get().remove(w)
            self._refresh(self.search_edit.text().strip())

    def _export_csv(self):
        v = Vocab.get()
        if v.count() == 0:
            QMessageBox.information(self, "导出", "生词本为空，没东西可导出。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 Anki CSV", "vocab_export.csv",
            "CSV 文件 (*.csv)",
        )
        if not path:
            return
        try:
            from pathlib import Path
            out = v.export_anki_csv(Path(path))
            QMessageBox.information(
                self, "导出完成",
                f"已导出 {v.count()} 个生词到：\n{out}\n\n"
                "打开 Anki → 文件 → 导入 即可。",
            )
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def _on_row_dblclick(self, index):
        col = index.column()
        if col == self.COL_WORD or col == self.COL_TRANSLATION:
            self._lookup_selected()
