"""全局热键（仅 Windows）。

- 切换点击穿透: 首选 Ctrl+Alt+T，被其他软件占用时依次回退
  Ctrl+Alt+G / Ctrl+Alt+P / Ctrl+Alt+Y（结果通过 clickthrough_name 查询，
  main 负责告知用户实际生效的组合键）
- Ctrl+Alt+R: 重新识别当前内容（被占用时回退 Ctrl+E）

实现: 专用工作线程内 RegisterHotKey(None) + GetMessageW 阻塞循环，
收到 WM_HOTKEY 后跨线程 emit Qt 信号（PyQt 自动排队到 GUI 线程）。
为什么这么绕:
1. QAbstractNativeEventFilter + installNativeEventFilter 在 PyInstaller
   冻结环境下有 sip "cannot be converted to QObject" 崩溃（实测）
2. RegisterHotKey(None) 的消息投递到线程队列，会被 Qt 自己的消息泵
   消费掉，QTimer+PeekMessage 轮询收不到（实测）
工作线程方案全部走 Win32 原生 API，与 Qt 事件循环零耦合。
任何一步失败都静默降级（只打日志），主功能不受影响。
"""
from __future__ import annotations

import logging
import os
import threading

from PyQt5.QtCore import QObject, pyqtSignal

log = logging.getLogger(__name__)

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002

ID_TOGGLE_CLICKTHROUGH = 1
ID_RESCAN = 2

# 候选组合键: (显示名, 修饰键, 虚拟键码)
_CT_CANDIDATES = [
    ("Ctrl+Alt+T", MOD_CONTROL | MOD_ALT, 0x54),
    ("Ctrl+Alt+G", MOD_CONTROL | MOD_ALT, 0x47),
    ("Ctrl+Alt+P", MOD_CONTROL | MOD_ALT, 0x50),
    ("Ctrl+Alt+Y", MOD_CONTROL | MOD_ALT, 0x59),
]
_RS_CANDIDATES = [
    ("Ctrl+Alt+R", MOD_CONTROL | MOD_ALT, 0x52),
    ("Ctrl+Alt+E", MOD_CONTROL | MOD_ALT, 0x45),
]


class HotkeyFilter(QObject):
    hotkey_toggle_clickthrough = pyqtSignal()
    hotkey_rescan = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._registered: list[tuple[int, str]] = []
        self._ready = threading.Event()
        self._plan: list[tuple] = []
        # 实际生效的组合键名（注册后可读，未注册为 ""）
        self.clickthrough_name = ""
        self.rescan_name = ""

    def register_all(self, preferred_ct: str = "", preferred_rs: str = "") -> bool:
        if os.name != "nt":
            return False
        self.unregister_all()
        self._plan = [
            (ID_TOGGLE_CLICKTHROUGH, _CT_CANDIDATES, preferred_ct),
            (ID_RESCAN, _RS_CANDIDATES, preferred_rs),
        ]
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=3):   # 等工作线程完成注册
            log.warning("热键注册线程未就绪，放弃热键")
            return False
        if self.clickthrough_name or self.rescan_name:
            log.info("全局热键注册: 穿透=%s 重扫=%s",
                     self.clickthrough_name or "失败", self.rescan_name or "失败")
            return True
        log.info("全局热键全部注册失败（可能被占用），仅托盘菜单可用")
        return False

    def _run(self):
        """工作线程: 注册 -> 消息循环 -> 退出时反注册（须同线程）。"""
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        names = {}
        # 本地持有已注册列表: 主线程 unregister_all 可能清空共享列表，
        # 退出清理必须用本地副本，否则反注册被跳过 -> 热键泄漏（P2 #13）
        local_registered = []
        for hid, candidates, preferred in self._plan:
            ordered = sorted(candidates,
                             key=lambda c: (c[0] != preferred,))
            for name, mod, vk in ordered:
                if user32.RegisterHotKey(None, hid, mod, vk):
                    local_registered.append((hid, name))
                    self._registered.append((hid, name))
                    names[hid] = name
                    break
        self.clickthrough_name = names.get(ID_TOGGLE_CLICKTHROUGH, "")
        self.rescan_name = names.get(ID_RESCAN, "")
        self._ready.set()
        if not local_registered:
            return

        msg = wintypes.MSG()
        # 注: GetMessageW 收到 WM_QUIT 时直接返回 0（不作为消息投递），
        # 循环退出即代表收到了退出指令
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                # 跨线程 emit: PyQt 自动以队列连接投递到 GUI 线程
                if msg.wParam == ID_TOGGLE_CLICKTHROUGH:
                    self.hotkey_toggle_clickthrough.emit()
                elif msg.wParam == ID_RESCAN:
                    self.hotkey_rescan.emit()
        # 反注册必须在注册线程内做
        for hid, _ in local_registered:
            user32.UnregisterHotKey(None, hid)
        self._registered.clear()

    def unregister_all(self):
        if self._thread is not None and self._thread.is_alive():
            if self._thread_id:
                try:
                    import ctypes
                    ctypes.windll.user32.PostThreadMessageW(
                        self._thread_id, WM_QUIT, 0, 0)
                except Exception:
                    pass
            # 线程退出时自己反注册（必须在注册线程内做）；给足时间，
            # 之前 1s 超时后清空状态、线程拿不到列表会泄漏热键
            self._thread.join(timeout=3)
        self._thread = None
        self._thread_id = 0
        self._registered = []
        self.clickthrough_name = ""
        self.rescan_name = ""
