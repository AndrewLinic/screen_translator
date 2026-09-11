"""单词查询结果弹窗

- 顶部：单词 + [复制][🔊 朗读][+ 生词本]
- 中部：IPA 音标 · 词性 · 标签 · Collins 词频
- 主体：词形变化 + 多义项 + 英文释义 + 在线发音链接
- 底部：状态信息
"""
from __future__ import annotations

import logging
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextBrowser, QFrame,
)

from dict_lookup import Dict, split_translations, pos_order_index
import tts
import vocab

log = logging.getLogger(__name__)


class DictWindow(QDialog):
    """单词词典结果弹窗。"""

    def __init__(self, word: str, parent=None):
        super().__init__(parent)
        self._word = word.strip()
        self.setWindowTitle(f"查词 - {self._word}")
        self.setMinimumSize(560, 440)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        # 标题行
        title_row = QHBoxLayout()
        title = QLabel(self._word, self)
        title.setFont(QFont("Microsoft YaHei UI", 26, QFont.Bold))
        title_row.addWidget(title)
        title_row.addStretch(1)

        self.btn_speak = QPushButton("🔊 朗读", self)
        self.btn_speak.setFont(QFont("Microsoft YaHei UI", 11))
        self.btn_speak.setMinimumHeight(32)
        self.btn_speak.clicked.connect(self._on_speak)
        title_row.addWidget(self.btn_speak)

        self.btn_vocab = QPushButton("+ 生词本", self)
        self.btn_vocab.setFont(QFont("Microsoft YaHei UI", 11))
        self.btn_vocab.setMinimumHeight(32)
        self.btn_vocab.clicked.connect(self._on_add_vocab)
        title_row.addWidget(self.btn_vocab)

        self.copy_btn = QPushButton("复制", self)
        self.copy_btn.setFont(QFont("Microsoft YaHei UI", 11))
        self.copy_btn.setMinimumHeight(32)
        self.copy_btn.clicked.connect(self._on_copy)
        title_row.addWidget(self.copy_btn)
        layout.addLayout(title_row)

        # 音标 / 词性 / 标签
        self.meta_label = QLabel("", self)
        self.meta_label.setFont(QFont("Microsoft YaHei UI", 12))
        self.meta_label.setStyleSheet("color: #555;")
        self.meta_label.setWordWrap(True)
        layout.addWidget(self.meta_label)

        # 分隔
        line = QFrame(self)
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        layout.addWidget(line)

        # 翻译正文
        self.body = QTextBrowser(self)
        self.body.setOpenExternalLinks(True)
        self.body.setFont(QFont("Microsoft YaHei UI", 13))
        self.body.setStyleSheet(
            "QTextBrowser { background: transparent; border: none; }"
        )
        layout.addWidget(self.body, 1)

        # 底部状态
        self.status_label = QLabel("", self)
        self.status_label.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(self.status_label)

        self._render(self._word)
        self._refresh_vocab_btn()

    # ===== 渲染 =====
    def _render(self, word: str):
        d = Dict.get().lookup(word)
        if not d:
            self.meta_label.setText("(未在离线词典中找到)")
            self.body.setHtml(f"<p style='color:#888'>词库未收录 <b>{word}</b></p>")
            self.copy_btn.setEnabled(False)
            return

        bits = []
        if d["phonetic"]:
            bits.append(f"IPA: <code>{d['phonetic']}</code>")
        if d["pos"]:
            bits.append("词性: " + " · ".join(d["pos"]))
        if d["tag"]:
            bits.append("标签: " + " · ".join(d["tag"]))
        if d["collins"]:
            stars = "★" * d["collins"] + "☆" * (5 - d["collins"])
            bits.append(f"Collins: {stars}")
        self.meta_label.setText("    ".join(bits))
        self.meta_label.setTextFormat(Qt.RichText)

        html_parts = []
        # 1. 词形变化
        if d["exchange"]:
            exchange_lines = []
            for kv in d["exchange"].split("/"):
                if ":" not in kv:
                    continue
                k, v = kv.split(":", 1)
                if v and v.lower() != word.lower():
                    exchange_lines.append(f"<b>{k}</b>: {v}")
            if exchange_lines:
                html_parts.append(
                    "<div style='color:#888; font-size:12px; margin-bottom:8px;'>"
                    + " · ".join(exchange_lines) + "</div>"
                )

        # 2. 多义项（词性按"常用义项优先"排序：动词/形容词为主的词，
        #    对应的义项排到名词前面；表外的生僻词保持词典原序）
        all_trans = d.get("all_translations") or [d["translation"]]
        seen = set()
        gathered = []      # (排序位次, 原序, item)
        _seq = 0
        for trans in all_trans:
            if not trans or trans in seen:
                continue
            seen.add(trans)
            for item in split_translations(trans):
                gathered.append(
                    (pos_order_index(self._word, item["pos"]), _seq, item))
                _seq += 1
        # 稳定排序：位次相同则保持词典原序（表外词全部为 0 => 原序不变）
        gathered.sort(key=lambda x: (x[0], x[1]))
        for _, _, item in gathered:
            pos_html = ""
            if item["pos"]:
                pos_html = (
                    f"<span style='color:#2d7df6; font-weight:bold; font-size:14px;'>{item['pos']}</span> "
                )
            means_html = "<br>".join(
                f"<span style='font-size:14px;'>&nbsp;&nbsp;• {m}</span>" for m in item["means"]
            )
            html_parts.append(f"<div style='margin:6px 0; line-height:1.6;'>{pos_html}{means_html}</div>")

        if not html_parts:
            html_parts.append("<p style='color:#888'>（无翻译内容）</p>")

        # 3. 网络释义链接
        extras = []
        if d["definition"]:
            extras.append(
                f"<details><summary style='color:#888; cursor:pointer;'>英文释义</summary>"
                f"<div style='color:#666; margin:8px 0; font-size:13px;'>{d['definition']}</div></details>"
            )
        if d["audio"]:
            extras.append(
                f"<a href='{d['audio']}' style='color:#2d7df6;'>🔊 在线发音</a>"
            )
        if extras:
            html_parts.append(
                "<hr style='border:none; border-top:1px solid #eee; margin:10px 0;'>"
                + "<br>".join(extras)
            )

        self.body.setHtml("\n".join(html_parts))
        self.status_label.setText(
            f"数据来源: ECDICT 离线词典 · 词条序号 {len(seen)}/{len(all_trans)}"
        )

    # ===== 按钮处理 =====
    def _on_copy(self):
        from PyQt5.QtWidgets import QApplication
        d = Dict.get().lookup(self._word)
        if d and d["translation"]:
            QApplication.clipboard().setText(d["translation"])
            self.copy_btn.setText("已复制 ✓")
            QTimer_single_shot(1500, self._reset_copy_btn)
            self.copy_btn.setEnabled(False)

    def _reset_copy_btn(self):
        self.copy_btn.setText("复制")
        self.copy_btn.setEnabled(True)

    def _on_speak(self):
        if not tts.available():
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "朗读", "本机未检测到 SAPI 语音引擎，无法朗读。"
                            "\n(Win10/11 默认自带 Zira/Huihui 语音)"
            )
            return
        tts.speak(self._word)

    def _on_add_vocab(self):
        d = Dict.get().lookup(self._word)
        v = vocab.Vocab.get()
        phonetic = d.get("phonetic", "") if d else ""
        translation = d.get("translation", "") if d else ""
        pos = " · ".join(d.get("pos", [])) if d else ""
        definition = d.get("definition", "") if d else ""
        v.add_or_update(
            self._word,
            phonetic=phonetic,
            translation=translation,
            pos=pos,
            definition=definition,
        )
        self.btn_vocab.setText("已加入 ✓")
        self.btn_vocab.setEnabled(False)
        QTimer_single_shot(1500, self._refresh_vocab_btn)

    def _refresh_vocab_btn(self):
        if vocab.Vocab.get().has(self._word):
            self.btn_vocab.setText("已在生词本")
            self.btn_vocab.setEnabled(False)
        else:
            self.btn_vocab.setText("+ 生词本")
            self.btn_vocab.setEnabled(True)

        if not tts.available():
            self.btn_speak.setEnabled(False)
            self.btn_speak.setToolTip("未安装 SAPI 语音")
        else:
            self.btn_speak.setEnabled(True)
            self.btn_speak.setToolTip("朗读单词")


def QTimer_single_shot(ms: int, fn):
    """小工具：在 QDialog 里用，避免每次都 import QtCore。"""
    from PyQt5.QtCore import QTimer
    QTimer.singleShot(ms, fn)
