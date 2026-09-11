"""屏幕翻译 V3 - 主入口

工作流：
    启动 -> 加载配置 -> 自动配置离线模型 -> 显示悬浮字幕 -> 系统托盘驻留
    定时器每秒：截图 -> OCR -> 去重 -> 翻译 -> 更新字幕
    字幕按钮 / 右键 -> 查词 / 朗读 / 加入生词本 / 复制 / 历史 / 退出

托盘菜单 / 字幕工具栏 都提供选区、查词、生词本、退出。
"""
from __future__ import annotations

import collections
import difflib
import logging
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import Qt, QTimer, QObject, pyqtSignal
from PyQt5.QtGui import QIcon, QPixmap, QPainter, QColor, QFont
from PyQt5.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QMessageBox,
)

from config import load_config, save_config, data_dir
from ocr import ocr_image, LANG_MAP
from screenshot import grab_region, select_region_interactive, get_primary_screen_size
from settings_dialog import SettingsDialog
from subtitle import SubtitleWindow
from region_overlay import RegionOverlay
from translator import (
    translate_with_fallback, init_argos, engine_status,
    query_installed_pairs, shutdown_worker, warmup_worker,
    OFFLINE_LANG_NAMES,
)
import trans_cache
import vocab
import tts
from hotkeys import HotkeyFilter

# 注意: 不能在模块顶层 logging.basicConfig() —— PyInstaller 无控制台模式
# 下 sys.stderr 是 None，basicConfig 会挂一个写 None 的 handler，
# 之后每条日志都报 "Logging error ... NoneType has no attribute write"。
# 配置移到 _setup_file_logging()（main() 最先调用），那里按环境安全配置。
log = logging.getLogger("screen_translator")

# 近似去重用: 去掉空白和标点后小写比较
_NOISE_RE = re.compile(r"[\s，。．,.:：;；!！?？'\"“”‘’()（）\-—*_]+")


def _text_key(s: str) -> str:
    return _NOISE_RE.sub("", s).lower()


# ===== 多帧投票 =====
# 字幕叠在动态场景上时每帧截图都有细微差异（背景在动、抗锯齿抖动），
# OCR 结果随之浮动 —— 同一个词这帧读对、下帧读错。把同一画面连续识别的
# 几帧结果按"词"做多数投票，能把这类偶发误识压下去。
# 代价可控: 第 1 帧照常出结果，后续帧只是"补票"，投票结果没变就不重译。
_VOTE_FRAMES = 3


def _vote_texts(texts: list) -> str:
    """词级多数投票。

    逐位置取出现次数最多的词；并列时取最早出现的（第一帧优先）。
    各帧词数差异过大说明对齐不可靠（某帧多识别/漏识别了一行），
    退回最长的一条（信息最全）。
    """
    texts = [t for t in (texts or []) if t]
    if not texts:
        return ""
    if len(texts) == 1:
        return texts[0]
    toks = [t.split() for t in texts]
    lens = [len(t) for t in toks]
    if max(lens) - min(lens) > max(2, max(lens) * 0.4):
        return max(texts, key=len)
    out = []
    for i in range(max(lens)):
        cnt = collections.Counter()
        for tl in toks:
            if i < len(tl):
                cnt[tl[i]] += 1
        if cnt:
            out.append(cnt.most_common(1)[0][0])
    return " ".join(out)


def make_tray_icon() -> QIcon:
    """生成托盘图标。"""
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#2d7df6"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 12, 12)
    p.setPen(QColor("white"))
    f = QFont("Microsoft YaHei UI", 32, QFont.Bold)
    p.setFont(f)
    p.drawText(pix.rect(), Qt.AlignCenter, "译")
    p.end()
    return QIcon(pix)


# ===== 自动配置离线模型 =====
def ensure_offline_models():
    """首次启动检查 Argos 离线模型是否就绪。

    模型分发在 assets/argos_models/（项目目录 / exe 同级 / 打包内部），
    但因为 CTranslate2 打不开含中文的路径，运行时会同步到
    %LOCALAPPDATA%\\ScreenTranslator\\models 后再加载。
    """
    from translator import (
        argos_packages_dir, runtime_model_dir, sync_bundled_models,
        bundled_model_dir,
    )
    rt = runtime_model_dir()
    src = bundled_model_dir()
    log.info("模型运行目录: %s", rt)

    # 已就绪
    try:
        if rt.exists() and any(rt.glob("translate-*")):
            added = sync_bundled_models(rt)  # 补上新放进去的模型
            if added:
                log.info("新增模型: %s", added)
            log.info("Argos 模型已就绪 (%d 个)",
                     len(list(rt.glob("translate-*"))))
            argos_packages_dir()
            return True
    except Exception as e:
        log.debug("检查模型目录失败: %s", e)

    # 首次运行 -> 从分发源同步
    if src is None:
        log.warning("未发现 bundled Argos 模型，离线翻译不可用"
                    "（可运行 install_models.py 下载）")
        return False

    added = sync_bundled_models(rt)
    log.info("已同步 %d 个模型到 %s", len(added), rt)
    argos_packages_dir()
    return bool(added) or any(rt.glob("translate-*"))


class ScreenTranslatorApp(QObject):
    # 工作线程 -> GUI 线程 投递译文。必须用信号: QTimer.singleShot 不能在
    # 非 GUI 线程调用（startTimer 静默失败，回调永远不执行）——
    # 这正是此前"翻译完成但界面永远停在翻译中"的根本原因。
    # 参数: (区域id, 原文, 译文, 是否显示原文)
    sig_show_text = pyqtSignal(int, str, str, bool)

    # OCR 工作线程 -> GUI 线程 投递识别结果。
    # RapidOCR 单次 0.5~0.9 秒，若在 GUI 线程同步调用会每秒卡顿一次，
    # 所以识别放进 _ocr_loop 线程，结果经信号回主线程再走去重/翻译。
    # 参数: (区域id, 识别文本)
    sig_ocr_done = pyqtSignal(int, str)

    # 离线模型自动下载进度 -> GUI 线程（更新状态灯提示文字）。
    # 必须 connect：pyqtSignal 漏接会静默丢弃（曾因此让整条识别链路停死）。
    # 参数: (状态, 提示)；状态="ready" 表示下载完成，需刷新语言菜单
    sig_model_status = pyqtSignal(str, str)

    # 多区域并发安全设计:
    # - 每个区域独立状态（key/gen/pending/结果），tick 轮流扫描（每轮一个区域），
    #   OCR 耗时不会随区域数线性叠加
    # - 翻译请求进入全局 FIFO 队列，由单一调度线程串行消费 ->
    #   翻译 worker 永远只有一个请求在飞，天然无竞态
    # - 区域在队列中已有 pending 请求时不重复入队，多区域同时变化也不会堆积
    # - 结果经信号回 GUI 线程，界面渲染只在主线程发生
    def __init__(self):
        super().__init__()
        self.sig_show_text.connect(self._show_text_slot)
        self.sig_model_status.connect(self._on_model_status)
        self.cfg = load_config()

        # 模型下载由本进程统一负责，禁止 offline_translate 子进程也去下
        # （否则两边同时写同一个 .part 文件会互相写坏）
        os.environ["SCREEN_TRANSLATOR_NO_AUTO_FETCH"] = "1"

        # 0. 首次运行配置模型（后台）。完成前预热线程等待，避免两个进程同时拷模型
        self._models_ready_event = threading.Event()

        def _ensure_models():
            try:
                ensure_offline_models()
            finally:
                self._models_ready_event.set()
        threading.Thread(target=_ensure_models, daemon=True).start()

        # 1. 初始化字幕窗口 + 选区指示框
        self.subtitle = SubtitleWindow()
        self.subtitle.apply_prefs({
            "font_size_original": self.cfg.get("font_size_original", 18),
            "font_size": self.cfg.get("font_size", 22),
            "show_original": self.cfg.get("show_original", True),
            "display_mode": self.cfg.get("display_mode", "block"),
            "theme": self.cfg.get("theme", "dark"),
            "window_opacity": self.cfg.get("window_opacity", 0.92),
            "tts_rate": self.cfg.get("tts_rate", 0),
        })
        geo = self.cfg.get("subtitle_geometry")
        if geo:
            self.subtitle.restore_geometry(geo)
        self.subtitle.show()
        # 恢复点击穿透（穿透开启时窗口不响应鼠标，只能托盘/热键关闭）
        if self.cfg.get("click_through"):
            self.subtitle.set_click_through(True)
        # 把字幕的信号接到 main
        self.subtitle.request_select_region.connect(self.choose_region)
        self.subtitle.request_quit.connect(self.quit_app)
        self.subtitle.request_history.connect(self.show_history)
        self.subtitle.request_manual_translate.connect(self._manual_translate)
        self.subtitle.request_set_target_lang.connect(self._set_target_lang)
        self.subtitle.request_display_prefs.connect(self._on_display_prefs)
        self.subtitle.request_rescan.connect(self._rescan)
        self.subtitle.request_toggle_click_through.connect(self._toggle_click_through)
        self.subtitle.request_toggle_paused.connect(self._on_paused_changed)
        self.subtitle.request_switch_region.connect(self._switch_region)
        self.subtitle.request_feedback.connect(self._export_feedback)
        self.subtitle.request_save_geometry.connect(self._save_geometry)
        # 字幕工具栏语言按钮：初始 4 个常用语言，查明已装模型后再刷新
        self.subtitle.set_languages(
            [(c, OFFLINE_LANG_NAMES.get(c, c))
             for c in ("zh", "en", "ja", "ko")],
            self.cfg.get("target_lang", "zh"),
        )
        self.subtitle.set_engine_status("pending", "离线引擎加载中…")

        # 选区常驻指示框（红框标记当前识别的屏幕区域）。
        # 多区域模式每个区域一个指示框，颜色区分。
        self._tick_round = 0             # 多区域轮询轮次
        self._active_region = 0          # 字幕窗当前显示的区域
        self._region_colors = ["#ff4d4d", "#ff9f0a", "#34c759"]
        self._overlays: list[RegionOverlay] = []
        self._sync_overlays()

        # 2. 预热 Argos 离线引擎
        self._argos_inited = False
        if self.cfg.get("engine_mode", "offline_first") != "mock":
            threading.Thread(target=self._warmup_argos, daemon=True).start()

        # 3. 预加载词典
        from dict_lookup import Dict
        threading.Thread(target=Dict.get().ensure_loaded, daemon=True).start()

        # 4. 初始化生词本
        vocab.Vocab.get()

        self._running = False
        self._empty_hint_shown = False   # "未识别到文字"提示只显示一次
        # 翻译缓存（跨会话持久化，sqlite）
        try:
            trans_cache.init(data_dir() / "translations.db")
        except Exception:
            pass
        # 多区域状态表（实际数量随 _active_regions 动态调整）
        self._region_state: list[dict] = [self._new_region_state()]
        # 翻译请求队列 + 单调度线程（串行消费 -> worker 永远只有一个请求在飞）
        self._trans_queue: "queue.Queue" = queue.Queue()
        # 保护 _region_state: 调度线程与 GUI 线程都会读写
        self._state_lock = threading.Lock()
        threading.Thread(target=self._dispatch_loop, daemon=True).start()

        # OCR 队列 + 单工作线程（识别比翻译快得多，串行足够且无竞态）。
        # 同一时刻只允许一个识别在飞: 识别期间 _tick 直接跳过本轮，
        # 免得慢引擎把队列堆爆（RapidOCR 约 0.6s / 轮询间隔 1s）。
        self._ocr_queue: "queue.Queue" = queue.Queue()
        self._ocr_inflight = False
        self._ocr_started_at = 0.0
        # 识别结果必须接回 GUI 线程 —— 漏了这句 _ocr_inflight 会永远停在
        # True，tick 从第二轮起就被自己挡住，表现为"识别一次后彻底不动、
        # 翻译永远不出来"。
        self.sig_ocr_done.connect(self._on_ocr_done)
        threading.Thread(target=self._ocr_loop, daemon=True).start()
        # 预热高精度引擎（起子进程 + 加载模型，约 1~2 秒，后台做不挡启动）
        if self.cfg.get("ocr_engine", "rapidocr") in ("rapidocr", "auto"):
            try:
                from ocr_rapid import warmup_async
                warmup_async()
            except Exception as e:
                log.debug("RapidOCR 预热启动失败: %s", e)

        # 全局热键（失败静默降级；优先用上次会话实际生效的组合键）。
        # 不用 installNativeEventFilter——PyInstaller 冻结环境下 sip 转换崩溃，
        # hotkeys.py 改为 QTimer 轮询线程消息队列
        self._hotkeys = HotkeyFilter()
        if self._hotkeys.register_all(
                preferred_ct=self.cfg.get("hotkey_clickthrough", ""),
                preferred_rs=self.cfg.get("hotkey_rescan", "")):
            self._hotkeys.hotkey_toggle_clickthrough.connect(self._toggle_click_through)
            self._hotkeys.hotkey_rescan.connect(self._rescan)
            self.cfg["hotkey_clickthrough"] = self._hotkeys.clickthrough_name
            self.cfg["hotkey_rescan"] = self._hotkeys.rescan_name
            save_config(self.cfg)
            # 回填字幕菜单与托盘菜单的实际热键名（可能已回退为 G/P/Y）
            self.subtitle.set_hotkey_names(
                self._hotkeys.clickthrough_name, self._hotkeys.rescan_name)

        # 5. 定时器
        self._timer = QTimer()
        self._timer.timeout.connect(self._tick)

        # 6. 托盘菜单
        # 注意: 必须用 menu.addAction("文字") 创建菜单项（menu 持有所有权）。
        # 之前用 QAction(text, None) 存局部变量，__init__ 结束后就被 Python
        # 垃圾回收，C++ 对象销毁 -> 菜单项全部消失（只剩存了实例属性的"开始识别"）。
        self.tray = QSystemTrayIcon(make_tray_icon())
        self.tray.setToolTip("屏幕翻译 - 左键双击切换识别 / 右键菜单")
        self._installed_langs = set()  # 后台查明后填充
        self._build_tray_menu()
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

        # 7. 启动提示（延迟 1s 等待模型准备）
        QTimer.singleShot(1200, self._post_startup_hint)

    # ===== 托盘菜单 =====
    def _build_tray_menu(self):
        """构建/重建托盘右键菜单。所有 action 由 menu 持有，不会被 GC。"""
        menu = QMenu()

        self.act_start = menu.addAction("▶ 开始识别")
        self.act_start.setCheckable(True)
        self.act_start.setChecked(self._running)
        self.act_start.triggered.connect(self.toggle_running)

        menu.addSeparator()

        # 当前翻译语言（可切换）
        self.lang_menu = menu.addMenu("🌐 当前翻译语言")
        self._rebuild_lang_menu()

        # 选区
        act_select = menu.addAction("⬜ 选择识别区域…")
        act_select.triggered.connect(self.choose_region)

        act_fullscreen = menu.addAction("🖥 识别整个主屏幕")
        act_fullscreen.triggered.connect(self.use_fullscreen)

        # 多区域轮询
        act_add_region = menu.addAction("➕ 添加识别区域（多区域轮询，最多3个）")
        act_add_region.triggered.connect(self.add_region)
        if self.cfg.get("regions"):
            n = len(self.cfg["regions"])
            act_clear_regions = menu.addAction(
                f"🗑 清除多区域（当前 {n} 个，恢复单区域）")
            act_clear_regions.triggered.connect(self.clear_regions)

        act_rescan = menu.addAction(
            f"🔁 重新识别当前内容 ({self._hotkeys.rescan_name or '热键不可用'})")
        act_rescan.triggered.connect(self._rescan)

        act_ct = menu.addAction(
            f"📌 点击穿透 ({self._hotkeys.clickthrough_name or '热键不可用'})")
        act_ct.setCheckable(True)
        act_ct.setChecked(self.cfg.get("click_through", False))
        act_ct.triggered.connect(self._toggle_click_through)

        # OCR 语言（识别不到文字时第一个该查的）
        ocr_menu = menu.addMenu("🔍 OCR 识别语言")
        cur_ocr = self.cfg.get("ocr_lang", "zh-Hans-CN")
        for code, name in LANG_MAP.items():
            act = ocr_menu.addAction(("● " if code == cur_ocr else "　") + name)
            act.setData(code)
            act.triggered.connect(lambda checked, c=code: self._set_ocr_lang(c))

        menu.addSeparator()
        act_settings = menu.addAction("⚙ 设置…")
        act_settings.triggered.connect(self.open_settings)

        act_dict = menu.addAction("📖 手动查词…")
        act_dict.triggered.connect(self.manual_dict_lookup)

        act_vocab = menu.addAction("📚 生词本…")
        act_vocab.triggered.connect(self.open_vocab)

        act_history = menu.addAction("⌚ 最近字幕…")
        act_history.triggered.connect(self.show_history)

        act_clear = menu.addAction("🧹 清空字幕")
        act_clear.triggered.connect(self._clear_all)

        menu.addSeparator()
        act_quit = menu.addAction("❌ 退出程序")
        act_quit.triggered.connect(self.quit_app)

        # 打开菜单时刷新"开始识别"勾选与语言显示
        menu.aboutToShow.connect(self._refresh_tray_menu)
        self.tray.setContextMenu(menu)
        self._tray_menu = menu

    def _rebuild_lang_menu(self):
        """填充"当前翻译语言"子菜单。"""
        lm = self.lang_menu
        lm.clear()
        cur = self.cfg.get("target_lang", "zh")
        # 已安装语言优先；未知时给出四个常用语言
        langs = sorted(self._installed_langs) if self._installed_langs else ["zh", "en", "ja", "ko"]
        if cur not in langs:
            langs = sorted(set(langs) | {cur})
        lm.setTitle(f"🌐 当前翻译语言: {OFFLINE_LANG_NAMES.get(cur, cur)}")
        for code in langs:
            name = OFFLINE_LANG_NAMES.get(code, code)
            installed = code in self._installed_langs if self._installed_langs else True
            act = lm.addAction(("● " if code == cur else "　") + name
                               + ("" if installed else " (未装模型)"))
            act.setData(code)
            act.triggered.connect(lambda checked, c=code: self._set_target_lang(c))

    def _set_target_lang(self, code: str):
        """切换目标翻译语言。"""
        if self.cfg.get("source_lang", "auto") == code:
            self.cfg["source_lang"] = "auto"  # 避免源=目标
        self.cfg["target_lang"] = code
        save_config(self.cfg)
        self._rebuild_lang_menu()
        # 字幕工具栏按钮同步显示当前语言
        try:
            self.subtitle.set_current_lang(code)
        except Exception:
            pass
        name = OFFLINE_LANG_NAMES.get(code, code)
        self.tray.showMessage("已切换翻译语言", f"屏幕文字将翻译为 {name}",
                              QSystemTrayIcon.Information, 2000)

    def _set_ocr_lang(self, code: str):
        """切换 OCR 识别语言。"""
        self.cfg["ocr_lang"] = code
        save_config(self.cfg)
        for st in self._region_state:
            st["key"] = ""  # 重新识别
        name = LANG_MAP.get(code, code)
        self.tray.showMessage("已切换 OCR 语言",
                              f"识别语言: {name}\n看日文内容选 日本語，韩文选 한국어",
                              QSystemTrayIcon.Information, 2500)

    def _refresh_tray_menu(self):
        """每次弹出菜单前刷新状态显示。"""
        self.act_start.setChecked(self._running)
        self.act_start.setText("⏸ 停止识别" if self._running else "▶ 开始识别")
        self._rebuild_lang_menu()

    def _post_startup_hint(self):
        # deep=False: 只做文件级快速检查，不在 GUI 线程拉起 worker 加载模型
        st = engine_status(deep=False)
        if st["argos_available"]:
            if self.cfg.get("auto_start", True):
                self.start()
            self.tray.showMessage(
                "屏幕翻译已启动",
                "正在实时识别屏幕并翻译。\n"
                "右键托盘图标可 切换语言 / 选区 / 退出。",
                QSystemTrayIcon.Information,
                4000,
            )
        else:
            # 离线引擎未就绪。开箱即用的功能还是会工作（OCR + 词典查词）。
            self.start()
            # 精简版发布包不带 842MB 模型 -> 后台自动下载中英双向（约 165MB）。
            # 下完会刷新语言菜单；期间 OCR/查词/在线翻译都不受影响。
            if self.cfg.get("auto_fetch_models", True):
                threading.Thread(target=self._auto_fetch_models,
                                 daemon=True).start()
            # 仅当：勾了 auto_start + Argos 未就绪 + 用户从未确认过这条提示
            # 时弹一次。已确认过（Yes/No/关窗口）就不再弹，避免每次启动骚扰。
            if (self.cfg.get("auto_start", True)
                    and not self.cfg.get("first_run_hinted", False)):
                QTimer.singleShot(2000, self._show_first_run_hint)
        # 后台预热: 拉起离线 worker 加载模型 + 查已安装语言对，
        # 完成后刷新托盘语言菜单（首次约 10~30 秒）
        threading.Thread(target=self._warmup_status, daemon=True).start()

    def _warmup_status(self):
        """后台: 预热离线引擎并查询语言对（不阻塞 GUI）。"""
        try:
            # 等主进程的模型同步先完成，避免与 worker 的同步并发
            self._models_ready_event.wait(timeout=90)
            pairs = query_installed_pairs(refresh=True)
            langs = sorted({c for p in pairs for c in p}) if pairs else []
            log.info("已安装语言: %s", langs or "(未查明)")
            QTimer.singleShot(0, lambda: self._on_pairs_ready(langs))
        except Exception as e:
            log.debug("预热查询失败: %s", e)

    def _on_pairs_ready(self, langs: list):
        """预热完成: 更新语言菜单（回到 GUI 线程执行）。"""
        self._installed_langs = set(langs)
        self._rebuild_lang_menu()
        self.subtitle.set_engine_status(
            "ok" if langs else "off",
            "离线翻译就绪" if langs else "离线模型未安装（可用 OCR+查词）")
        # 字幕工具栏语言按钮也同步为已安装语言
        cur = self.cfg.get("target_lang", "zh")
        shown = set(langs) if langs else {"zh", "en", "ja", "ko"}
        shown.add(cur)
        pairs = [(c, OFFLINE_LANG_NAMES.get(c, c))
                 for c in sorted(shown) if c in OFFLINE_LANG_NAMES]
        self.subtitle.set_languages(pairs, cur)

    # ===== 离线模型自动下载（精简版发布包不带 842MB 模型）=====
    def _on_model_status(self, state: str, tip: str):
        """模型下载进度回调（GUI 线程执行）。"""
        if state == "ready":
            # 下载完成 -> 重新查询已装语言对并刷新菜单/状态灯
            threading.Thread(target=self._warmup_status, daemon=True).start()
            return
        try:
            self.subtitle.set_engine_status(state, tip)
        except Exception:
            pass

    def _auto_fetch_models(self):
        """一套模型都没有时，后台自动下载中英双向（约 165MB）。

        发布包为了控制体积不带模型；这里保证"下载解压后直接能用"。
        下载期间 OCR / 查词 / 在线翻译照常工作，进度打在状态灯 tooltip 上。
        """
        try:
            from translator import runtime_model_dir
            from model_fetch import ensure_langs, models_missing, human, CORE_LANGS
        except Exception as e:
            log.debug("自动下载模型不可用: %s", e)
            return
        rt = runtime_model_dir()
        try:
            if not models_missing(rt):
                return                      # 已有模型（完整版 / 上次已下过）
        except Exception:
            return

        log.info("未发现离线模型，开始自动下载核心模型 %s -> %s", CORE_LANGS, rt)
        self.sig_model_status.emit("pending", "正在下载离线翻译模型…")
        last_pct = [-1]

        def _on_progress(got: int, total: int):
            if not total:
                return
            pct = int(got * 100 / total)
            if pct != last_pct[0] and pct % 5 == 0:     # 每 5% 刷一次，别刷屏
                last_pct[0] = pct
                self.sig_model_status.emit(
                    "pending",
                    f"正在下载离线翻译模型 {pct}%（{human(got)}/{human(total)}）")

        try:
            done = ensure_langs(CORE_LANGS, rt, progress=_on_progress,
                                log=lambda m: log.info("[model] %s", m))
        except Exception as e:
            log.warning("自动下载离线模型失败: %s", e)
            self.sig_model_status.emit(
                "off", "离线模型下载失败：可手动运行 install_models.py 重试")
            return
        if done:
            log.info("离线模型下载完成: %s", done)
            self.sig_model_status.emit(
                "ready", "离线模型已就绪")
        else:
            log.warning("离线模型下载未获得任何包")
            self.sig_model_status.emit(
                "off", "离线模型下载失败：可手动运行 install_models.py 重试")

    def _show_first_run_hint(self):
        """首次启动提示，告知安装离线翻译的方法。
        不论用户选 Yes / No / 关窗口，都把 first_run_hinted 置 True 并
        持久化，后续启动不再弹（避免每次开机都被打扰）。
        装好 Argos 之后这条分支也不会进，所以天然免疫重复弹窗。
        """
        ret = QMessageBox.question(
            None,
            "屏幕翻译 - 首次启动",
            "✓ 已可用:  OCR + 单词查询 + 字幕显示 + 历史记录 + 生词本\n\n"
            "✗ 未可用:  离线翻译（需安装 Argos）\n\n"
            "想启用离线翻译？点击 [是] 打开安装说明（install_offline_translate.bat）。\n"
            "当前字幕会用 mock 模式（[mock]原文），OCR 和查词不受影响。\n\n"
            "下次想装也可以右键托盘 → 退出 再运行安装脚本。",
            QMessageBox.Yes | QMessageBox.No,
        )
        # 一次性标记：写回配置，下次启动不再弹
        self.cfg["first_run_hinted"] = True
        save_config(self.cfg)
        if ret == QMessageBox.Yes:
            import subprocess
            bat = Path(sys.executable).parent / "install_offline_translate.bat"
            if bat.exists():
                try:
                    subprocess.Popen(["cmd", "/c", "start", "", str(bat)], shell=False)
                except Exception:
                    pass

    def _warmup_argos(self):
        ok = init_argos()
        log.info("Argos 预热: %s", "OK" if ok else "失败")

    # ===== 控制 =====
    def toggle_running(self):
        if self._running:
            self.stop()
        else:
            self.start()

    def start(self):
        if self._running:
            return
        self._running = True
        self.act_start.setChecked(True)
        interval = int(self.cfg.get("interval_ms", 1000))
        self._timer.start(interval)
        log.info("识别已启动，间隔 %d ms", interval)

    def stop(self):
        if not self._running:
            return
        self._running = False
        self.act_start.setChecked(False)
        self._timer.stop()
        log.info("识别已停止")
        self.subtitle.clear()

    # ===== 区域管理 =====
    @staticmethod
    def _new_region_state() -> dict:
        return {"key": "", "text": "", "gen": 0, "shown_gen": 0,
                "pending": False, "img": None, "orig": "", "trans": "",
                "frame_fp": None,
                # 多帧投票: 同一画面已投递的帧数 + 各帧识别文本
                "vote_tries": 0, "vote_texts": []}

    def _active_regions(self) -> list:
        """返回 [(区域id, (x,y,w,h) 或 None), ...]。

        多区域模式: cfg["regions"] 列表非空时启用；否则单区域（cfg["region"]，None=全屏）。
        """
        regions = self.cfg.get("regions")
        if regions:
            return [(i, tuple(r)) for i, r in enumerate(regions[:3])]
        region = self.cfg.get("region")
        return [(0, tuple(region) if region else None)]

    def _sync_overlays(self):
        """按当前区域配置同步指示框（每个区域一个，颜色区分）。"""
        regions = self._active_regions()
        multi = bool(self.cfg.get("regions"))
        while len(self._overlays) < len(regions):
            self._overlays.append(RegionOverlay())
        for i, ov in enumerate(self._overlays):
            if i < len(regions):
                color = self._region_colors[i % len(self._region_colors)] if multi else None
                ov.set_region(regions[i][1], color=color)
            else:
                ov.set_region(None)
        # 字幕窗区域切换按钮
        self.subtitle.set_region_chips(
            len(regions) if multi else 0, self._active_region)

    def choose_region(self):
        self.subtitle.hide()
        for ov in self._overlays:
            ov.hide()
        try:
            region = select_region_interactive()
        finally:
            self.subtitle.show()
        if region is not None:
            self.cfg["region"] = list(region)
            self.cfg["regions"] = []   # 单区域选择清除多区域模式
            save_config(self.cfg)
            self._reset_region_states()
            self._sync_overlays()
            x, y, w, h = region
            self.tray.showMessage(
                "已选区域",
                f"位置: ({x}, {y})  尺寸: {w} × {h}\n"
                f"屏幕上会用红框标出识别范围",
                QSystemTrayIcon.Information,
                2500,
            )
        else:
            # 取消选择：恢复显示原有区域指示框
            self._sync_overlays()
            self.tray.showMessage("已取消", "未选择区域", QSystemTrayIcon.Information, 1500)

    def add_region(self):
        """多区域模式: 追加一个识别区域（最多 3 个）。"""
        self.subtitle.hide()
        for ov in self._overlays:
            ov.hide()
        try:
            regions = list(self.cfg.get("regions") or [])
            region = select_region_interactive()
        finally:
            self.subtitle.show()
        if region is not None:
            if len(regions) >= 3:
                self.tray.showMessage("已达上限",
                                      "最多支持 3 个识别区域\n"
                                      "请先用\"清除多区域\"重置",
                                      QSystemTrayIcon.Warning, 2500)
                self._sync_overlays()
                return
            regions.append(list(region))
            self.cfg["regions"] = regions
            if len(regions) == 1:
                self.cfg["region"] = list(region)
            save_config(self.cfg)
            self._reset_region_states()
            self._active_region = len(regions) - 1
            self._sync_overlays()
            self.tray.showMessage(
                "已添加区域",
                f"第 {len(regions)} 个区域: {region[2]} × {region[3]}\n"
                f"字幕窗顶部 1/2/3 按钮可切换查看",
                QSystemTrayIcon.Information, 3000)
        else:
            self._sync_overlays()
            self.tray.showMessage("已取消", "未选择区域", QSystemTrayIcon.Information, 1500)

    def clear_regions(self):
        """退出多区域模式，保留单区域。"""
        regions = self.cfg.get("regions") or []
        if regions:
            first = regions[0]
            self.cfg["region"] = list(first)
        self.cfg["regions"] = []
        save_config(self.cfg)
        self._active_region = 0
        self._reset_region_states()
        self._sync_overlays()
        self.tray.showMessage("已恢复单区域", "多区域轮询已关闭",
                              QSystemTrayIcon.Information, 2000)

    def _reset_region_states(self, n: Optional[int] = None):
        """区域变化后重置状态表（强制重新识别）。"""
        if n is None:
            n = len(self._active_regions())
        # 加锁重绑定: 防止调度线程正拿着旧表写入 in-flight 结果
        with self._state_lock:
            self._region_state = [self._new_region_state()
                                  for _ in range(max(1, n))]
        self._empty_hint_shown = False

    def _switch_region(self, idx: int):
        """用户点击字幕窗 1/2/3 按钮: 切换当前显示的区域。"""
        regions = self._active_regions()
        if idx < 0 or idx >= len(regions):
            return
        self._active_region = idx
        self._show_region_content(idx)

    def _show_region_content(self, idx: int):
        st = self._region_state[idx] if idx < len(self._region_state) \
            else self._new_region_state()
        if st["orig"] or st["trans"]:
            self.subtitle.show_text(
                st["orig"], st["trans"],
                show_original=self.cfg.get("show_original", True))
        else:
            self.subtitle.clear()
        self._sync_chips()

    def _sync_chips(self):
        multi = bool(self.cfg.get("regions"))
        self.subtitle.set_region_chips(
            len(self._active_regions()) if multi else 0, self._active_region)

    def use_fullscreen(self):
        self.cfg["region"] = None
        self.cfg["regions"] = []
        save_config(self.cfg)
        self._reset_region_states()
        self._sync_overlays()
        self.tray.showMessage("已切换", "现在识别整个主屏幕", QSystemTrayIcon.Information, 1500)

    def _rescan(self):
        """一键重扫: 强制重新识别翻译当前内容（按钮/热键 Ctrl+Alt+R）。"""
        for st in self._region_state:
            st["key"] = ""
            st["frame_fp"] = None   # 同时清帧指纹，否则画面没动会被去重挡掉
            st["vote_texts"] = []   # 同时清投票缓冲，重扫要拿到新的多数结果
            st["vote_tries"] = 0
        log.info("手动重扫触发")
        QTimer.singleShot(0, self._tick)

    def _on_paused_changed(self, paused: bool):
        """字幕窗暂停/恢复 -> 真正停启轮询。

        暂停: 停定时器（OCR/翻译全部停止，不再浪费 CPU）。
        恢复: 清去重键强制重译当前内容（翻译缓存命中则秒出），
        立即扫描一次，不等到屏幕内容变化。
        """
        if paused:
            self._timer.stop()
            log.info("暂停: 识别轮询已停止")
        else:
            for st in self._region_state:
                st["key"] = ""
                st["frame_fp"] = None   # 清帧指纹，恢复后必定重扫一次
                st["vote_texts"] = []   # 投票缓冲一并清掉
                st["vote_tries"] = 0
            self._timer.start(int(self.cfg.get("interval_ms", 1000)))
            QTimer.singleShot(0, self._tick)
            log.info("恢复: 重新扫描当前内容")

    def _on_display_prefs(self, prefs: dict):
        """字幕窗口"显示"弹窗的偏好变化 -> 持久化。"""
        self.cfg.update(prefs)
        save_config(self.cfg)

    def open_settings(self):
        dlg = SettingsDialog()
        if dlg.exec_() == SettingsDialog.Accepted:
            self.cfg = load_config()
            self.subtitle.apply_prefs({
                "font_size_original": self.cfg.get("font_size_original", 18),
                "font_size": self.cfg.get("font_size", 22),
                "show_original": self.cfg.get("show_original", True),
                "display_mode": self.cfg.get("display_mode", "block"),
                "theme": self.cfg.get("theme", "dark"),
                "window_opacity": self.cfg.get("window_opacity", 0.92),
                "tts_rate": self.cfg.get("tts_rate", 0),
            })
            if self._running:
                self._timer.setInterval(int(self.cfg.get("interval_ms", 1000)))
            # 目标语言可能在设置里改了，刷新菜单显示
            self._rebuild_lang_menu()

    def manual_dict_lookup(self):
        from PyQt5.QtWidgets import QInputDialog
        word, ok = QInputDialog.getText(None, "手动查词", "输入英文单词:")
        if ok and word.strip():
            from dict_window import DictWindow
            DictWindow(word.strip(), None).exec_()

    def open_vocab(self):
        from vocab_window import VocabWindow
        VocabWindow(None).exec_()

    def show_history(self):
        self.subtitle.show_history_window()

    def _clear_all(self):
        self.subtitle.clear()

    def _manual_translate(self, text: str):
        """用户手动修改/输入文本后重译（OCR 识别错误的补救入口）。"""
        text = (text or "").strip()
        if not text:
            return
        rid = self._active_region
        st = self._region_state[rid] if rid < len(self._region_state) \
            else self._new_region_state()
        st["key"] = _text_key(text)
        st["text"] = text
        st["gen"] += 1
        gen = st["gen"]
        st["pending"] = True
        self._empty_hint_shown = False
        show_original = self.cfg.get("show_original", True)
        QTimer.singleShot(0, lambda: self.subtitle.show_translating(
            text, show_original=show_original))
        self._trans_queue.put((rid, text, dict(self.cfg), gen))

    def quit_app(self):
        log.info("用户退出")
        tts.stop()
        self.stop()
        try:
            self._hotkeys.unregister_all()
        except Exception:
            pass
        for ov in self._overlays:
            ov.hide()
        self.tray.hide()
        self._trans_queue.put(None)  # 让调度线程收尾
        self._ocr_queue.put(None)    # 让 OCR 线程收尾
        try:
            from ocr_rapid import get_client
            get_client().close()     # 杀掉 RapidOCR 常驻子进程
        except Exception:
            pass
        shutdown_worker()  # 杀掉常驻离线翻译子进程
        QApplication.quit()

    # ===== 点击穿透 / 几何持久化 =====
    def _toggle_click_through(self):
        on = not self.cfg.get("click_through", False)
        self.cfg["click_through"] = on
        save_config(self.cfg)
        # 必须延迟到当前事件栈返回之后: setWindowFlags 会销毁重建原生窗口，
        # 在托盘/更多菜单的 triggered 处理中直接调用会连带菜单的 transient
        # 窗口关系一起崩（用户实测托盘点穿透必崩的根因）
        QTimer.singleShot(0, lambda: self._safe_set_click_through(on))
        if on:
            QTimer.singleShot(200, lambda: self.tray.showMessage(
                "点击穿透已开启",
                "字幕窗口不再响应鼠标，不挡游戏/网页点击。\n"
                f"关闭方式: 快捷键 {self._hotkeys.clickthrough_name or '托盘菜单'}，"
                "或右键托盘图标。",
                QSystemTrayIcon.Information, 4000))

    def _safe_set_click_through(self, on: bool):
        try:
            self.subtitle.set_click_through(on)
        except Exception:
            log.exception("切换点击穿透失败")
            self.cfg["click_through"] = not on
            save_config(self.cfg)

    def _save_geometry(self, geo: list):
        self.cfg["subtitle_geometry"] = geo
        save_config(self.cfg)

    # ===== OCR 误识反馈 =====
    def _export_feedback(self, corrected: str):
        """把当前区域截图 + OCR 原始结果 + 用户修正 + 日志打包到 logs/feedback。"""
        try:
            import io
            import zipfile
            from datetime import datetime
            # 日志目录: 优先用启动时解析好的 _LOG_DIR；兜底从 root logger
            # 的 FileHandler 反查（FileHandler 挂在 root 上，具名 logger
            # 的 handlers 为空，之前查错对象导致 zip 里不含 app.log）
            log_dir = _LOG_DIR
            if log_dir is None:
                for h in logging.getLogger().handlers:
                    if isinstance(h, logging.FileHandler):
                        log_dir = Path(h.baseFilename).parent
                        break
            if log_dir is None:
                log_dir = data_dir()
            fb_dir = log_dir / "feedback"
            fb_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            zip_path = fb_dir / f"feedback_{stamp}.zip"
            rid = min(self._active_region, max(0, len(self._region_state) - 1))
            st = self._region_state[rid]
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                if st.get("img") is not None:
                    buf = io.BytesIO()
                    st["img"].save(buf, format="PNG")
                    z.writestr("screenshot.png", buf.getvalue())
                # writestr 没有 encoding 参数，文本需自行编码为字节
                z.writestr("ocr_text.txt", (st.get("orig") or "").encode("utf-8"))
                z.writestr("corrected_text.txt", (corrected or "").encode("utf-8"))
                # 配置（脱敏: 去掉百度密钥）
                safe_cfg = {k: v for k, v in self.cfg.items()
                            if k not in ("baidu_app_id", "baidu_secret")}
                z.writestr("config.json", __import__("json").dumps(
                    safe_cfg, ensure_ascii=False, indent=2).encode("utf-8"))
                # 最近日志
                for name in ("app.log", "stderr.log"):
                    f = log_dir / name
                    if f.exists():
                        try:
                            tail = f.read_text(encoding="utf-8", errors="replace"
                                               ).splitlines()[-200:]
                            z.writestr(name + ".tail",
                                       "\n".join(tail).encode("utf-8"))
                        except Exception:
                            pass
            log.info("误识反馈已导出: %s", zip_path)
            QMessageBox.information(
                None, "反馈已保存",
                f"诊断包已导出到:\n{zip_path}\n\n"
                "包含: 区域截图 / OCR 原始结果 / 修正文本 / 最近日志。")
        except Exception as e:
            log.exception("导出反馈失败: %s", e)
            QMessageBox.warning(None, "反馈失败", f"导出诊断包失败: {e}")

    # ===== 定时任务 =====
    def _tick(self):
        """定时轮询: 抓图 -> 交给 OCR 线程（识别结果由 _on_ocr_done 接续）。

        识别是异步的（RapidOCR 约 0.6 秒），所以这里只做"抓图 + 投递"，
        真正的去重/翻译在 _on_ocr_done 里做，避免卡住 GUI 线程。
        """
        if not self._running:
            return
        if self._ocr_inflight:
            # 看门狗: 正常识别 0.3~3 秒。超过 20 秒还没回来说明引擎卡死
            # （子进程挂起 / 通信断），强制放行本轮，避免整条链路永久停摆。
            if time.time() - self._ocr_started_at > 20.0:
                log.warning("OCR 超过 20 秒未返回，强制复位识别标记")
                self._ocr_inflight = False
            else:
                return  # 上一次识别还没回来，跳过本轮（不堆积）
        try:
            regions = self._active_regions()
            # 状态表长度对齐（区域数可能在运行中被修改）
            with self._state_lock:
                if len(self._region_state) != len(regions):
                    self._region_state = [self._new_region_state()
                                          for _ in range(len(regions))]
            # 每轮只扫一个区域（轮流），OCR 耗时不会随区域数叠加
            rid, region = regions[self._tick_round % len(regions)]
            self._tick_round += 1
            st = self._region_state[rid]
            img = grab_region(region)
            if img is None or img.size[0] < 5 or img.size[1] < 5:
                return
            # 帧内容去重: 画面逐像素没变就没必要再跑一次 OCR。
            # 静止画面（看剧时台词停留好几秒）下这一层把识别开销直接砍到 0，
            # 既省 CPU 也消除了"旧帧排队等识别"造成的延迟感。
            # 用 80x45 灰度缩略图做指纹: 任何文字变化都必然改变它，
            # 而计算只要几毫秒（对整幅图做哈希要 15ms+）。
            try:
                fp = img.convert("L").resize((80, 45)).tobytes()
            except Exception:
                fp = None
            # 画面没变时默认跳过（省 CPU）；但若该画面还没攒够投票帧数，
            # 就再识别一次做多帧投票（字幕静止时同一画面最多识别
            # _VOTE_FRAMES 次，投满即停）。
            vote_frames = max(1, int(self.cfg.get("vote_frames", _VOTE_FRAMES)))
            if fp is not None and fp == st.get("frame_fp"):
                if st.get("vote_tries", 0) >= vote_frames:
                    return  # 本画面已投满票，跳过识别
            elif fp is not None:
                st["frame_fp"] = fp        # 新画面 -> 重开一轮投票
                st["vote_texts"] = []
                st["vote_tries"] = 0
            if fp is not None:
                st["vote_tries"] = st.get("vote_tries", 0) + 1
            st["img"] = img
            self._ocr_inflight = True
            self._ocr_started_at = time.time()
            self._ocr_queue.put((
                rid, img,
                self.cfg.get("ocr_lang", "zh-Hans-CN"),
                self.cfg.get("ocr_engine", "rapidocr"),
            ))
        except Exception as e:
            self._ocr_inflight = False
            log.exception("tick 出错: %s", e)

    def _ocr_loop(self):
        """OCR 工作线程: 串行识别，结果经信号回 GUI 线程。"""
        while True:
            item = self._ocr_queue.get()
            if item is None:
                return
            rid, img, lang, engine = item
            try:
                text = ocr_image(img, lang=lang, engine=engine).strip()
            except Exception as e:
                log.exception("OCR 出错: %s", e)
                text = ""
            self.sig_ocr_done.emit(rid, text)

    def _on_ocr_done(self, rid: int, text: str):
        """识别结果回到 GUI 线程: 多帧投票 -> 去重 -> 投翻译队列。"""
        self._ocr_inflight = False
        try:
            regions = self._active_regions()
            if rid < 0 or rid >= len(self._region_state):
                return  # 区域在识别期间被重建，结果作废
            st = self._region_state[rid]
            # 多帧投票: 累积本画面各帧的识别结果，按词取多数。第 1 帧照常
            # 往下走（用户立刻看到结果）；后续帧的投票结果若与已送出的内容
            # 一致，会被下面的去重挡掉，不会重复翻译。
            if text:
                st.setdefault("vote_texts", []).append(text)
            text = _vote_texts(st.get("vote_texts"))
            if not text:
                # 识别为空: 给一次可见提示（多区域时只提示当前显示的区域，
                # 避免某个区域暂时无文字就抢屏）
                if not self._empty_hint_shown and rid == self._active_region:
                    self._empty_hint_shown = True
                    self.subtitle.set_hint(
                        "未识别到文字 —— 请检查:\n"
                        "1. 选区内是否真有文字\n"
                        "2. 右键托盘 → OCR 识别语言 是否匹配（日文选日本語、韩文选한국어）")
                return
            self._empty_hint_shown = False
            key = _text_key(text)
            if key == st["key"]:
                return  # 内容没变，不重复翻译
            # 近似去重: OCR 噪声会让每帧文本有 1~2 个字符差异，差异小于
            # 5% 视为同一内容，避免无限重译
            if st["key"] and difflib.SequenceMatcher(
                    None, key, st["key"]).ratio() > 0.95:
                return
            st["key"] = key
            st["text"] = text
            if st["pending"]:
                return  # 该区域已有翻译排队中，等结果即可
            # 立即反馈: 先显示原文 + "翻译中"，避免长时间无响应像死机
            show_original = self.cfg.get("show_original", True)
            if rid == self._active_region or len(regions) == 1:
                self.subtitle.show_translating(
                    text, show_original=show_original)
            st["gen"] += 1
            st["pending"] = True
            self._trans_queue.put((rid, text, dict(self.cfg), st["gen"]))
        except Exception as e:
            log.exception("处理识别结果出错: %s", e)

    def _dispatch_loop(self):
        """翻译调度线程: 串行消费队列，多区域同时变化也只会一个翻译在飞。"""
        while True:
            item = self._trans_queue.get()
            if item is None:
                return
            rid, text, cfg, gen = item
            t0 = time.time()
            try:
                # 取状态快照（加锁: 与 GUI 线程 _reset_region_states/_tick
                # 的状态表重绑定互斥）。gen 不匹配说明状态表已被重置
                # （暂停/切换区域等），这条 in-flight 请求直接作废，
                # 不再把旧结果写进新状态表。
                with self._state_lock:
                    states = self._region_state
                    st = states[rid] if rid < len(states) else None
                    if st is not None and st["gen"] != gen:
                        st = None
                if st is None:
                    log.info("丢弃过期翻译请求: 区域%d gen=%d", rid, gen)
                    continue
                mode = cfg.get("engine_mode", "offline_first")
                # 1. 查持久缓存（mock 模式不查）
                translated = None
                ck = None
                if mode != "mock":
                    ck = trans_cache.cache_key(
                        text, cfg.get("source_lang", "auto"),
                        cfg.get("target_lang", "zh"),
                        engine=mode)
                    translated = trans_cache.get(ck)
                    if translated is not None:
                        log.info("缓存命中 (区域%d): %d 字", rid, len(text))
                # 2. 未命中 -> 真正翻译
                if translated is None:
                    prefer_offline = mode in ("offline_first", "offline_only")
                    use_mock = mode == "mock"
                    if mode == "online_only":
                        prefer_offline = False
                        if not (cfg.get("baidu_app_id") and cfg.get("baidu_secret")):
                            use_mock = True
                    translated = translate_with_fallback(
                        text,
                        appid=cfg.get("baidu_app_id", ""),
                        secret=cfg.get("baidu_secret", ""),
                        from_lang=cfg.get("source_lang", "auto"),
                        to_lang=cfg.get("target_lang", "zh"),
                        use_mock=use_mock,
                        prefer_offline=prefer_offline,
                    ) or ""
                    # 缓存成功结果（mock 兜底结果以 ⚠ 结尾，不入缓存）
                    if ck and translated and not translated.rstrip().endswith("⚠"):
                        trans_cache.put(ck, translated)
                # 3. 记录结果（加锁写回，且再次校验 gen:
                # 翻译期间状态表可能已被重置，结果作废不让它显示）
                with self._state_lock:
                    cur = self._region_state
                    cur_st = cur[rid] if rid < len(cur) else None
                    if cur_st is None or cur_st["gen"] != gen:
                        log.info("翻译结果作废: 区域%d gen=%d (状态已重置)",
                                 rid, gen)
                        continue
                    cur_st["shown_gen"] = max(cur_st.get("shown_gen", 0), gen)
                    cur_st["orig"] = text
                    cur_st["trans"] = translated
                log.info("翻译完成: 区域%d 原文 %d 字 -> 译文 %d 字 (gen=%d, 耗时 %.1f 秒)",
                         rid, len(text), len(translated or ""), gen,
                         time.time() - t0)
                # 4. 跨线程投递: 信号自动排队到 GUI 线程执行
                self.sig_show_text.emit(
                    rid, text, translated, cfg.get("show_original", True))
            except Exception as e:
                log.exception("翻译调度出错 (区域%d): %s", rid, e)
            finally:
                # 清 pending: 仅当状态表仍是本请求这一代（gen 相同）才清，
                # 避免误伤重置后新状态表或新一代请求的 pending 标记
                with self._state_lock:
                    cur = self._region_state
                    if rid < len(cur) and cur[rid]["gen"] == gen:
                        cur[rid]["pending"] = False

    def _show_text_slot(self, rid: int, original: str, translated: str,
                        show_original: bool):
        """GUI 线程内显示译文（由 sig_show_text 信号驱动）。"""
        log.info("显示译文: 区域%d 原文 %d 字 -> 译文 %d 字",
                 rid, len(original), len(translated or ""))
        try:
            # 最新结果优先显示（多区域时自动切到有新内容的区域）
            if len(self._region_state) > 1:
                self._active_region = rid
                self._sync_chips()
            self.subtitle.show_text(original, translated, show_original=show_original)
        except Exception:
            log.exception("show_text 渲染失败")

    # ===== 托盘交互 =====
    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.toggle_running()


# 日志清理策略: 单文件超过 LOG_MAX_MB 轮转(保留一份 .old)，超过
# LOG_KEEP_DAYS 天的日志文件启动时清除。
LOG_MAX_MB = 5
LOG_KEEP_DAYS = 30

# _setup_file_logging 解析出的日志目录（供反馈诊断包等使用；
# 不要用 logger.handlers 反查——FileHandler 挂在 root 上，具名 logger 查不到）
_LOG_DIR: Optional[Path] = None


def _resolve_log_dir() -> Path:
    """日志目录: 项目根/logs（exe 在 dist/屏幕翻译 下时）。

    1. exe 位于 <项目根>/dist/屏幕翻译/ 且上两级能找到 README.md
       -> 用 <项目根>/logs （用户要求日志集中在大文件夹里）
    2. 否则（绿色单文件夹拷贝到别处）用 exe 同目录的 logs/
    3. 都没有写权限再退回 %LOCALAPPDATA%\\ScreenTranslator\\logs
    """
    exe_dir = Path(sys.executable).parent
    candidates = []
    proj = exe_dir.parent.parent
    if (proj / "README.md").exists():
        candidates.append(proj / "logs")
    candidates.append(exe_dir / "logs")
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA",
                                   Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / ".local" / "share"
    candidates.append(base / "ScreenTranslator" / "logs")
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return d
        except Exception:
            continue
    return candidates[-1]  # 全失败时返回最后的候选，由调用方兜底


def _migrate_old_logs(new_dir: Path):
    """把 LOCALAPPDATA 里的旧日志搬一次家（新位置还没有 app.log 时）。"""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA",
                                   Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / ".local" / "share"
    old_dir = base / "ScreenTranslator" / "logs"
    if old_dir == new_dir or not old_dir.exists():
        return
    if (new_dir / "app.log").exists():
        return
    try:
        for name in ("app.log", "stderr.log"):
            src = old_dir / name
            if src.exists():
                src.replace(new_dir / name)
        log.info("旧日志已从 %s 迁移到 %s", old_dir, new_dir)
    except Exception:
        pass


def _cleanup_logs(d: Path):
    """定量: 超过 LOG_MAX_MB 的日志轮转为 .old（顶掉上一份 .old）。
    定期: 删除超过 LOG_KEEP_DAYS 天的日志文件。"""
    try:
        import time as _time
        now = _time.time()
        for f in d.glob("*.log*"):
            try:
                if now - f.stat().st_mtime > LOG_KEEP_DAYS * 86400:
                    f.unlink()
                    continue
            except Exception:
                pass
        for name in ("app.log", "stderr.log"):
            f = d / name
            try:
                if f.exists() and f.stat().st_size > LOG_MAX_MB * 1024 * 1024:
                    old = d / (f.stem + ".old.log")
                    if old.exists():
                        old.unlink()
                    f.replace(old)
            except Exception:
                pass
    except Exception:
        pass


def _setup_file_logging():
    """配置日志: 文件必写，控制台视环境而定。

    位置: <项目根>/logs/app.log（exe 在 dist/屏幕翻译 下时），
    详见 _resolve_log_dir。每次启动做大小轮转 + 过期清理。
    """
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    try:
        d = _resolve_log_dir()
        _migrate_old_logs(d)
        _cleanup_logs(d)
        fh = logging.FileHandler(d / "app.log", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
        global _LOG_DIR
        _LOG_DIR = d
        # stderr 落盘: exe 无控制台，Qt/PyQt 的警告（如跨线程定时器）
        # 只打到 stderr，没有这一步就完全不可见
        err_f = open(d / "stderr.log", "a", encoding="utf-8", buffering=1)
        sys.stderr = err_f
        sys.stdout = err_f
        log.info("===== 程序启动，日志文件: %s =====", d / "app.log")
    except Exception:
        pass  # 日志失败不影响主功能
    # 控制台 handler: 只在 stderr 可用时挂（开发环境 python main.py）
    try:
        if sys.stderr is not None and hasattr(sys.stderr, "write"):
            ch = logging.StreamHandler(sys.stderr)
            ch.setFormatter(fmt)
            root.addHandler(ch)
    except Exception:
        pass


def main():
    _setup_file_logging()
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, "屏幕翻译", "当前系统不支持托盘图标，无法运行。")
        return 1

    translator = ScreenTranslatorApp()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
