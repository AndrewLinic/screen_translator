"""设置对话框"""
from __future__ import annotations

import os

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QComboBox, QSpinBox,
    QDialogButtonBox, QCheckBox, QVBoxLayout, QLabel, QHBoxLayout,
    QPushButton, QGroupBox, QMessageBox,
)

from config import load_config, save_config
from ocr import LANG_MAP
from translator import TARGET_LANGS, OFFLINE_LANG_NAMES, engine_status


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("屏幕翻译 - 设置")
        self.setMinimumWidth(480)
        self._cfg = load_config()
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        # 全局字号加大
        font = self.font()
        font.setPointSize(max(font.pointSize(), 11))
        self.setFont(font)

        # ===== 翻译引擎 =====
        engine_box = QGroupBox("翻译引擎")
        form = QFormLayout(engine_box)

        self.engine_combo = QComboBox()
        # 离线优先 / 在线优先 / 仅离线 / 仅在线 / Mock
        self.engine_combo.addItem("离线优先 (Argos, 推荐)", "offline_first")
        self.engine_combo.addItem("在线优先 (百度 API)", "online_first")
        self.engine_combo.addItem("仅离线 (Argos)", "offline_only")
        self.engine_combo.addItem("仅在线 (百度 API)", "online_only")
        self.engine_combo.addItem("Mock 翻译 (调试用)", "mock")
        idx = self._find_index(self.engine_combo, self._cfg.get("engine_mode", "offline_first"))
        self.engine_combo.setCurrentIndex(idx)
        form.addRow("引擎模式:", self.engine_combo)

        # 状态显示
        st = engine_status()
        pairs = st.get("argos_pairs") or []
        installed = sorted({c for p in pairs for c in p})
        st_text = []
        if st["argos_available"]:
            names = "、".join(OFFLINE_LANG_NAMES.get(c, c) for c in installed) or "无"
            st_text.append(f"✓ Argos 离线: 已就绪（{names}）")
        else:
            st_text.append("✗ Argos 离线: 未安装模型（运行 install_models.py）")
        st_text.append("✓ Mock 翻译: 总是可用")
        self.engine_status = QLabel("  |  ".join(st_text))
        self.engine_status.setWordWrap(True)
        self.engine_status.setStyleSheet("color: #555; font-size: 10px;")
        form.addRow("", self.engine_status)

        # 已装模型 / 打开模型目录
        if installed:
            tip = QLabel("已装模型（任意两种语言互译，缺直达模型时自动经英语中转）:\n  "
                         + "  ".join(f"{a}⇄{b}" for a, b in sorted(pairs) if a != b))
            tip.setWordWrap(True)
            tip.setStyleSheet("color: #2d7df6; font-size: 10px;")
            form.addRow("", tip)
        btn_row = QHBoxLayout()
        btn_open = QPushButton("📂 打开模型目录")
        btn_open.clicked.connect(self._open_model_dir)
        btn_install = QPushButton("⬇ 安装更多语言模型")
        btn_install.clicked.connect(self._install_more)
        btn_row.addWidget(btn_open)
        btn_row.addWidget(btn_install)
        form.addRow("", btn_row)
        root.addWidget(engine_box)

        # ===== 百度 API（仅在选择在线相关模式时使用） =====
        baidu_box = QGroupBox("百度翻译 API (在线备用)")
        baidu_form = QFormLayout(baidu_box)
        self.appid_edit = QLineEdit(self._cfg.get("baidu_app_id", ""))
        self.appid_edit.setPlaceholderText("百度翻译开放平台 AppID")
        baidu_form.addRow("AppID:", self.appid_edit)

        self.secret_edit = QLineEdit(self._cfg.get("baidu_secret", ""))
        self.secret_edit.setEchoMode(QLineEdit.Password)
        self.secret_edit.setPlaceholderText("百度翻译开放平台 Secret")
        baidu_form.addRow("Secret:", self.secret_edit)

        link = QLabel('<a href="https://api.fanyi.baidu.com/manage/developer">注册百度翻译开放平台 →</a>')
        link.setOpenExternalLinks(True)
        baidu_form.addRow("", link)
        root.addWidget(baidu_box)

        # ===== 语种 =====
        lang_box = QGroupBox("语种")
        lang_form = QFormLayout(lang_box)
        # 只列出已安装的语言；离线不可用时退回常见语言列表
        if installed:
            lang_items = [(c, OFFLINE_LANG_NAMES.get(c, c)) for c in installed]
        else:
            lang_items = list(TARGET_LANGS.items())
        self.from_combo = QComboBox()
        self.from_combo.addItem("自动检测", "auto")
        for code, name in lang_items:
            self.from_combo.addItem(name, code)
        idx = self._find_index(self.from_combo, self._cfg.get("source_lang", "auto"))
        self.from_combo.setCurrentIndex(idx)
        lang_form.addRow("源语言:", self.from_combo)

        self.to_combo = QComboBox()
        for code, name in lang_items:
            self.to_combo.addItem(name, code)
        idx = self._find_index(self.to_combo, self._cfg.get("target_lang", "zh"))
        self.to_combo.setCurrentIndex(idx)
        lang_form.addRow("目标语言:", self.to_combo)

        self.ocr_combo = QComboBox()
        for code, name in LANG_MAP.items():
            self.ocr_combo.addItem(f"{name} ({code})", code)
        idx = self._find_index(self.ocr_combo, self._cfg.get("ocr_lang", "zh-Hans-CN"))
        self.ocr_combo.setCurrentIndex(idx)
        lang_form.addRow("OCR 语言:", self.ocr_combo)

        ocr_tip = QLabel("屏幕上是什么语言就选什么（看日剧选 日本語，看韩综选 한국어）；"
                         "选错会识别成乱码。缺语言包请到 Windows 设置 → 语言 里添加。")
        ocr_tip.setWordWrap(True)
        ocr_tip.setStyleSheet("color: #888; font-size: 10px;")
        lang_form.addRow("", ocr_tip)

        # OCR 引擎（识别精度 vs 速度）
        self.ocr_engine_combo = QComboBox()
        self.ocr_engine_combo.addItem("RapidOCR 高精度 (推荐)", "rapidocr")
        self.ocr_engine_combo.addItem("Windows 快速 (最省资源)", "windows")
        self.ocr_engine_combo.addItem("自动择优 (两个都跑)", "auto")
        idx = self._find_index(self.ocr_engine_combo,
                               self._cfg.get("ocr_engine", "rapidocr"))
        self.ocr_engine_combo.setCurrentIndex(idx)
        lang_form.addRow("OCR 引擎:", self.ocr_engine_combo)

        try:
            from ocr_rapid import preflight
            rapid_ok = preflight()
        except Exception:
            rapid_ok = False
        engine_tip = QLabel(
            ("✓ RapidOCR 可用（子进程，识别更准：实测同一张游戏截图 "
             "CER 0.00~0.01，Windows 引擎 0.02~0.04）。\n"
             if rapid_ok else
             "✗ 未检测到 rapidocr（缺 Python 依赖），选 RapidOCR 会自动回退 "
             "Windows 引擎。安装: pip install rapidocr\n")
            + "RapidOCR 用内置中英多语言模型（其他语种建议用 Windows 引擎）；"
              "不可用时自动回退，不会中断识别。")
        engine_tip.setWordWrap(True)
        engine_tip.setStyleSheet(
            f"color: {'#2d7df6' if rapid_ok else '#c0392b'}; font-size: 10px;")
        lang_form.addRow("", engine_tip)
        root.addWidget(lang_box)

        # ===== 显示 =====
        view_box = QGroupBox("显示与刷新")
        view_form = QFormLayout(view_box)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(200, 10000)
        self.interval_spin.setSingleStep(100)
        self.interval_spin.setSuffix(" ms")
        self.interval_spin.setValue(int(self._cfg.get("interval_ms", 1000)))
        view_form.addRow("刷新间隔:", self.interval_spin)

        self.font_spin = QSpinBox()
        self.font_spin.setRange(8, 48)
        self.font_spin.setValue(int(self._cfg.get("font_size", 16)))
        view_form.addRow("字幕字号:", self.font_spin)

        self.show_original_chk = QCheckBox("显示原文")
        self.show_original_chk.setChecked(bool(self._cfg.get("show_original", True)))
        view_form.addRow("", self.show_original_chk)
        root.addWidget(view_box)

        # ===== 区域 =====
        region_box = QGroupBox("识别区域")
        region_form = QFormLayout(region_box)
        region = self._cfg.get("region")
        if region:
            text = f"({region[0]}, {region[1]})  {region[2]} × {region[3]}"
        else:
            text = "整个主屏幕"
        self.region_lbl = QLabel(text)
        region_form.addRow("当前区域:", self.region_lbl)
        root.addWidget(region_box)

        # ===== 按钮 =====
        btns = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel,
            parent=self,
        )
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def _find_index(self, combo: QComboBox, value: str) -> int:
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                return i
        return 0

    def _open_model_dir(self):
        """打开模型目录（可手动增删语言模型）。"""
        import subprocess
        from translator import runtime_model_dir
        d = runtime_model_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                subprocess.Popen(["explorer", str(d)])
            else:
                subprocess.Popen(["xdg-open", str(d)])
        except Exception as e:
            QMessageBox.warning(self, "打不开", f"{d}\n{e}")

    def _install_more(self):
        """提示如何安装更多语言。"""
        QMessageBox.information(
            self, "安装更多语言模型",
            "在程序目录（或项目目录）打开命令行，运行：\n\n"
            "    python install_models.py ja ko fr\n\n"
            "即可下载并安装日语、韩语、法语（可替换为任意语言代码）。\n"
            "查看全部可用语言：python install_models.py --list\n\n"
            "安装后重启本程序即可在新语言间互译。\n"
            "（下载源在国外，脚本自带多个镜像，失败可重试）",
        )

    def _on_save(self):
        cfg = load_config()
        cfg["engine_mode"] = self.engine_combo.currentData()
        cfg["baidu_app_id"] = self.appid_edit.text().strip()
        cfg["baidu_secret"] = self.secret_edit.text().strip()
        cfg["source_lang"] = self.from_combo.currentData()
        cfg["target_lang"] = self.to_combo.currentData()
        cfg["ocr_lang"] = self.ocr_combo.currentData()
        cfg["ocr_engine"] = self.ocr_engine_combo.currentData()
        cfg["interval_ms"] = int(self.interval_spin.value())
        cfg["font_size"] = int(self.font_spin.value())
        cfg["show_original"] = self.show_original_chk.isChecked()
        save_config(cfg)
        self.accept()
