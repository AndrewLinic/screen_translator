"""配置管理 - 集中读取/保存用户配置"""
import json
import os
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".screen_translator"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    # 翻译引擎模式: offline_first / online_first / offline_only / online_only / mock
    "engine_mode": "offline_first",
    # 百度翻译开放平台 https://api.fanyi.baidu.com/manage/developer
    "baidu_app_id": "",
    "baidu_secret": "",
    # 翻译方向: auto=自动检测源语言
    "source_lang": "auto",
    "target_lang": "zh",
    # OCR 语言（Windows OCR 代码）
    "ocr_lang": "zh-Hans-CN",
    # OCR 引擎: rapidocr / windows / auto
    #   rapidocr = RapidOCR(PP-OCRv6, 高精度, 单次约 0.5~0.9 秒, 走子进程)
    #   windows  = Windows.Media.Ocr(极快, 约 0.05~0.2 秒, 误读略多)
    #   auto     = 两个都跑并择优(最稳, 代价是每次两次识别)
    "ocr_engine": "rapidocr",
    # 截屏区域 (x, y, w, h)，全屏则为 None
    "region": None,
    # 刷新间隔 (毫秒)
    "interval_ms": 1000,
    # 多帧投票帧数: 同一画面连续识别几帧后按词取多数（压制动态背景下的
    # OCR 浮动误识）。1 = 关闭（只识别一帧）。第 1 帧照常立刻出结果，
    # 所以调大只影响 CPU，不会增加"看到第一条译文"的等待。
    "vote_frames": 3,
    # 字幕字号
    "font_size": 22,
    # 原文字号（工具栏"显示"弹窗里可独立调节）
    "font_size_original": 18,
    # 是否显示原文
    "show_original": True,
    # 离线资源路径（默认项目自带 assets/）
    "assets_dir": "",
    # 启动时自动开始识别
    "auto_start": True,
    # 首次启动提示（Argos 未就绪时弹的安装引导框）已确认过，
    # True=用户已看过（不论选 Yes/No/关窗口），后续不再弹
    "first_run_hinted": False,
    # ===== 显示偏好（字幕工具栏 Aa 显示 弹窗） =====
    # 显示模式: block=原文/译文分块, interleave=双语逐行对照
    "display_mode": "block",
    # 字幕主题: dark / light
    "theme": "dark",
    # 字幕窗口不透明度 0.3~1.0
    "window_opacity": 0.92,
    # 整段朗读语速 -5(慢)~5(快)
    "tts_rate": 0,
    # 鼠标点击穿透（窗口只显示不挡操作，托盘/热键 Ctrl+Alt+T 切换）
    "click_through": False,
    # ===== 多区域轮询 =====
    # 多个识别区域 [(x,y,w,h), ...]，非空时启用多区域模式（最多 3 个）
    "regions": None,
    # 字幕窗口位置与大小 [x, y, w, h]
    "subtitle_geometry": None,
}


def data_dir() -> Path:
    """数据目录（翻译缓存等），优先项目内，符合"文件集中管理"约定。

    1. 源码运行: 项目根/data（cwd 下有 main.py 或 README.md）
    2. exe 在 <项目根>/dist/屏幕翻译: 用 <项目根>/data
    3. 绿色单文件夹: exe 同目录/data
    4. 兜底: ~/.screen_translator/data
    """
    candidates = []
    cwd = Path.cwd()
    if (cwd / "main.py").exists() or (cwd / "README.md").exists():
        candidates.append(cwd / "data")
    exe_dir = Path(sys.executable).parent
    proj = exe_dir.parent.parent
    if (proj / "README.md").exists():
        candidates.append(proj / "data")
    candidates.append(exe_dir / "data")
    candidates.append(CONFIG_DIR / "data")
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return d
        except Exception:
            continue
    d = CONFIG_DIR / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_config() -> dict:
    """读取配置，无文件则用默认值"""
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG.copy())
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # 缺失键补齐
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception:
        return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
