"""RapidOCR 常驻识别子进程（高精度 OCR 引擎）。

为什么是独立进程：
  1. onnxruntime 与 PyQt5 / winocr 在同一进程内会 DLL 冲突
     （ImportError: DLL 加载失败 - 动态链接库(DLL)初始化例程失败），
     除非 onnxruntime 抢在它们之前导入；放子进程彻底绕开。
  2. onnxruntime + opencv 约 200MB，打进 exe 会让绿色包体积暴涨；
     本进程由 pyenv.json 里那个共享 Python 环境启动，exe 保持精简。
  3. 模型加载要 1~2 秒，常驻进程只加载一次；每次识别 0.4~0.9 秒。

协议（stdin/stdout，按行 UTF-8 JSON）:
  请求: {"id": 1, "png": "<base64 PNG>", "lang": "zh-Hans-CN"}
  应答: {"id": 1, "ok": true, "text": "...", "lines": [[x, y, w, h, "文字"], ...],
         "ms": 480.2, "lang_type": "ch", "fallback": false}
  控制: {"cmd": "ping"} -> {"ok": true, "ready": true}
        {"cmd": "quit"} -> 退出

模型选用经验（实测，见 2026-09-10 日志）:
  - PP-OCRv6 small（内置，det 9.9MB + rec 21MB）明显优于 Windows OCR：
    真实游戏截图 CER 0.000~0.018，Windows OCR 是 0.019~0.042。
  - Det.limit_type 默认 "min" 会把 1016x105 这种窄条放大 7 倍再检测
    （单次 2.5 秒）；改成 "max" 后原地检测，0.5 秒且更准。
  - Global.use_cls=False 省掉方向分类，中文/英文横排不需要。
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import time

# 协议走原始字节流，避免 Windows 控制台代码页污染
_STDIN = sys.stdin.buffer
_STDOUT = sys.stdout.buffer
# 任何库的 print 一律赶去 stderr（客户端会把 stderr 接到日志文件）
sys.stdout = sys.stderr

os.environ.setdefault("OMP_NUM_THREADS", str(os.cpu_count() or 4))

import numpy as np                     # noqa: E402
from PIL import Image                  # noqa: E402

from rapidocr import RapidOCR          # noqa: E402  (内部先导入 onnxruntime)

# OCR 语言代码 -> RapidOCR 语言模型。
# 中文（简/繁）与英文用内置的多语言 PP-OCRv6，离线开箱可用；
# 其余语种按需加载专用模型（本地没有会联网下载，失败自动退回内置模型）。
_LANG_TYPE = {
    "zh-Hans-CN": "ch",
    "zh-Hant-TW": "chinese_cht",
    "en-US": "en",
    "ja-JP": "japan",
    "ko-KR": "korean",
    "ru-RU": "cyrillic",
}
_BUILTIN_SAFE = {"ch", "en"}   # 内置模型直接覆盖，不触发下载
_DEFAULT_LANG_TYPE = "ch"

_BASE_PARAMS = {
    # 关键：不改 limit_type 时窄条图会被放大数倍，慢且更差
    "Det.limit_type": "max",
    "Det.limit_side_len": 1280,
    "Det.box_thresh": 0.3,
    "Det.unclip_ratio": 2.0,
    # 横排文本不需要方向分类，省一次前向
    "Global.use_cls": False,
    "Global.log_level": "error",
}

_engines: dict = {}


def _build_engine(lang_type: str) -> tuple:
    """返回 (engine, lang_type, fallback)。专用模型不可用时退回内置模型。"""
    if lang_type and lang_type not in _BUILTIN_SAFE:
        params = dict(_BASE_PARAMS)
        params["Rec.lang_type"] = lang_type
        params["Det.lang_type"] = lang_type
        try:
            return RapidOCR(params=params), lang_type, False
        except Exception as e:
            print(f"[ocr_worker] {lang_type} 模型不可用({e})，退回内置模型",
                  file=sys.stderr, flush=True)
    return RapidOCR(params=dict(_BASE_PARAMS)), _DEFAULT_LANG_TYPE, (
        lang_type not in _BUILTIN_SAFE)


def get_engine(lang: str) -> tuple:
    want = _LANG_TYPE.get(lang, _DEFAULT_LANG_TYPE)
    if want in _engines:
        return _engines[want]
    eng, used, fb = _build_engine(want)
    _engines[want] = (eng, used, fb)
    return _engines[want]


def _order_lines(boxes, txts) -> list:
    """按"视觉行"排序（同一行的框先合并再按 x 排），还原自然阅读顺序。

    同框间距判定（用框多边形的实际右边缘计算真实 gap，不再用 len(text) 估算）:
      - gap < 0.15 * h_med（或重叠/负值）: 同一单词被检测模型拆成多框
        （如 "unfamiliar" -> "ufa"+"mili"+"ar"），直接拼接不加空格
      - 0.15 * h_med <= gap <= 1.5 * h_med: 正常词间距，加 1 个空格
      - gap > 1.5 * h_med: 大间距（如两列布局），加 4 个空格
    """
    if boxes is None or len(boxes) == 0:
        return []
    rows = []
    for box, text in zip(boxes, txts):
        if not text:
            continue
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        x0, x1 = min(xs), max(xs)
        y0 = min(ys)
        h = max(ys) - min(ys)
        rows.append({"x": x0, "x1": x1, "y": y0, "h": max(1.0, h), "t": text})
    if not rows:
        return []
    heights = sorted(r["h"] for r in rows)
    h_med = heights[len(heights) // 2] or 20.0
    rows.sort(key=lambda r: (r["y"], r["x"]))
    out = []
    for r in rows:
        if out and abs(r["y"] - out[-1]["y"]) < 0.6 * h_med:
            prev = out[-1]
            gap = r["x"] - prev["x1"]
            if gap < 0.15 * h_med:
                sep = ""       # 同一单词被拆成多框，直接拼接
            elif gap > 1.5 * h_med:
                sep = "    "   # 大间距（多列/缩进）
            else:
                sep = " "      # 正常词间距
            prev["t"] += sep + r["t"]
            prev["x1"] = max(prev["x1"], r["x1"])
        else:
            out.append({"y": r["y"], "x1": r["x1"], "t": r["t"],
                        "w": max(1.0, h_med), "h": r["h"]})
    return [(r["t"], (int(0), int(r["y"]), int(r["w"]), int(r["h"])))
            for r in out]


def handle(req: dict) -> dict:
    if req.get("cmd") == "ping":
        return {"ok": True, "ready": True}

    lang = req.get("lang") or "zh-Hans-CN"
    raw = base64.b64decode(req.get("png") or "")
    img = Image.open(io.BytesIO(raw))
    if img.mode != "RGB":
        img = img.convert("RGB")

    t0 = time.time()
    engine, lang_type, fallback = get_engine(lang)
    out = engine(np.asarray(img))
    txts = out.txts or ()
    if len(txts) == 0:
        text = ""
        lines = []
    else:
        ordered = _order_lines(out.boxes, txts)
        lines = [[r[0], list(r[1])] for r in ordered]
        text = "\n".join(r[0] for r in ordered)
    return {"ok": True, "text": text, "lines": lines,
            "ms": round((time.time() - t0) * 1000, 1),
            "lang_type": lang_type, "fallback": fallback}


def main() -> int:
    # 常驻前先热身一遍，把模型加载开销放到客户端启动阶段
    try:
        get_engine("zh-Hans-CN")
    except Exception as e:
        print(f"[ocr_worker] 初始化失败: {e}", file=sys.stderr, flush=True)
        return 1

    _STDOUT.write(json.dumps({"ok": True, "ready": True}).encode("utf-8") + b"\n")
    _STDOUT.flush()

    while True:
        line = _STDIN.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line.decode("utf-8"))
        except Exception as e:
            _STDOUT.write(json.dumps(
                {"ok": False, "error": f"bad request: {e}"}).encode("utf-8") + b"\n")
            _STDOUT.flush()
            continue
        if req.get("cmd") == "quit":
            return 0
        try:
            resp = handle(req)
        except Exception as e:
            import traceback
            traceback.print_exc(file=sys.stderr)
            resp = {"ok": False, "error": str(e)}
        resp["id"] = req.get("id")
        try:
            _STDOUT.write(json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n")
            _STDOUT.flush()
        except Exception:
            return 1


if __name__ == "__main__":
    sys.exit(main())
