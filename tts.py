"""离线 TTS - Windows SAPI (win32com.client)
- 英文用 Microsoft Zira / Hazel (Windows 自带)
- 中文用 Microsoft Huihui / Xiaoxiao
- 简单启发：根据文本所含字符自动选语音
- 完全离线，零下载
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

log = logging.getLogger(__name__)

# 候选 lazy 加载（win32com 在 pyinstaller 里要单独 hook；用 ImportError 容错）
_spVoice = None
_voices = None
_ready = False
_err: Optional[str] = None
_lock = threading.Lock()
_supported = True


def _is_cjk(text: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in text)


def _ensure_init() -> bool:
    global _spVoice, _voices, _ready, _err, _supported
    if _ready:
        return True
    if not _supported:
        return False
    with _lock:
        if _ready:
            return True
        try:
            import win32com.client  # noqa: F401
            _spVoice = win32com.client.Dispatch("SAPI.SpVoice")
            _voices = _spVoice.GetVoices()
            _ready = True
            log.info("TTS 已初始化，共 %d 个语音", _voices.Count)
            return True
        except ImportError as e:
            _err = f"未安装 pywin32: {e}"
            _supported = False
            log.warning("TTS 不可用: %s", _err)
            return False
        except Exception as e:
            _err = str(e)
            log.warning("TTS 初始化失败: %s", e)
            return False


def _pick_voice(cjk: bool):
    """根据语种挑一个合适的语音，找不到则用默认。"""
    if _voices is None:
        return None
    if _voices.Count == 0:
        return None
    # 关键字优先级
    if cjk:
        for kw in ("Microsoft Xiaoxiao", "Microsoft Huihui",
                   "Microsoft Yaoyao", "Microsoft Kangkang",
                   "Chinese", "中文"):
            for i in range(_voices.Count):
                v = _voices.Item(i)
                name = v.GetDescription() or v.GetAttribute("Name") or ""
                if kw.lower() in name.lower():
                    return v
    else:
        for kw in ("Microsoft Zira", "Microsoft Hazel",
                   "Microsoft David", "Mark", "English",
                   "Aria", "Jenny"):
            for i in range(_voices.Count):
                v = _voices.Item(i)
                name = v.GetDescription() or v.GetAttribute("Name") or ""
                if kw.lower() in name.lower():
                    return v
    return _voices.Item(0)  # fallback


def speak(text: str, rate: Optional[int] = None, volume: int = 100) -> bool:
    """朗读文本。rate=-2..2 表示慢~快；不指定则用默认。

    返回 True 表示成功发出声音，False 表示 TTS 不可用。
    """
    if not text or not text.strip():
        return False
    if not _ensure_init():
        return False
    try:
        cjk = _is_cjk(text)
        voice = _pick_voice(cjk)
        if voice is not None:
            _spVoice.Voice = voice
        if rate is not None:
            _spVoice.Rate = max(-10, min(10, int(rate) * 2))  # rate 缩放 [-10,10]
        _spVoice.Volume = max(0, min(100, int(volume)))
        # Speak 异步, 不阻塞 UI
        _spVoice.Speak(text, 1)  # 1 = SVSFlagsAsync
        return True
    except Exception as e:
        log.warning("朗读失败: %s", e)
        return False


def stop():
    """停止当前朗读。"""
    if not _ensure_init():
        return
    try:
        _spVoice.Speak("", 2)  # 2 = SVSFPurgeBeforeSpeak
    except Exception:
        pass


def available() -> bool:
    """检查 TTS 是否可用。"""
    return _ensure_init()


def error_message() -> Optional[str]:
    return _err


def list_voices() -> list[dict]:
    """列出所有可用语音（调试用）。"""
    if not _ensure_init():
        return []
    out = []
    for i in range(_voices.Count):
        v = _voices.Item(i)
        try:
            out.append({
                "name": v.GetAttribute("Name") or "",
                "lang": v.GetAttribute("Language") or "",
                "desc": v.GetDescription() or "",
            })
        except Exception:
            pass
    return out
