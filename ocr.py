"""Windows OCR 封装 - 基于 winocr (Windows.Media.Ocr)"""
from __future__ import annotations

import itertools
import logging
import re
from typing import List, Tuple

import winocr
from PIL import Image

log = logging.getLogger(__name__)

# 常用 OCR 语言代码 (BCP-47)
LANG_MAP = {
    "zh-Hans-CN": "简体中文",
    "zh-Hant-TW": "繁体中文",
    "en-US": "English",
    "ja-JP": "日本語",
    "ko-KR": "한국어",
    "fr-FR": "Français",
    "de-DE": "Deutsch",
    "ru-RU": "Русский",
}


def _upscale(img: Image.Image, scale: int) -> Image.Image:
    """放大图片。小字号文字直接喂给 OCR 识别率很低，放大后明显提升。"""
    if scale <= 1:
        return img
    w, h = img.size
    return img.resize((w * scale, h * scale), Image.LANCZOS)


def _pick_scale(img: Image.Image) -> int:
    """按图片尺寸选择放大倍数。"""
    w, h = img.size
    if h < 40 or w < 200:
        return 3
    if h < 100 or w < 500:
        return 2
    return 1


# 符号识别说明（实测结论）:
# Windows OCR 的中文引擎会把箭头(→↑)误读成形近字("一"/"T"/"乛")，
# 纯符号行则中英引擎都返回空。英文引擎兜底合并经实测无收益
# （行内箭头它照样丢、纯符号行它也认不出），反而会注入乱码，已移除。
# 有效手段是像素级形状修正: 见 _fix_symbol_misreads。
# 单独出现的纯符号(如独立一行"→")属于系统引擎能力边界，无法识别。

# Windows OCR 对箭头类符号没有专门的字符类，常见误读成形近字/字母。
# 修正策略（像素级验证，避免误伤正常文本）:
#   乛 -> →   (笔画字符，正常内容不会单独出现，直接修正)
#   "一"(细长横条形) -> 像素分析: 墨量明显偏向一端 => 箭头 →/←
#   "T"/"t"(窄长条形) -> 像素分析: 顶部/底部有展开的箭头翼 => ↑/↓
# 纯像素验证保证: 真的"一"字(对称横条)、真的 T(T恤) 不会被改。
_ARROW_MISREAD_MAP = {"乛": "→"}


def _has_cjk(s: str) -> bool:
    return any(0x2E80 <= ord(c) <= 0x9FFF or 0xAC00 <= ord(c) <= 0xD7AF
               for c in s)


def _ink_mask(img, rect):
    """取词矩形内的二值墨迹掩码。

    - 自适应阈值: 细线条抗锯齿后呈灰色，固定阈值会漏掉
    - 极性自适应: 深色背景(浅色文字)与浅色背景都能处理
    返回 numpy 数组（True=墨迹）或 None。
    """
    try:
        import numpy as np
    except ImportError:
        return None
    try:
        x, y, w, h = [int(v) for v in rect]
        # Windows OCR 的 bounding_rect 整体偏上几像素，细横条字形
        # （如被误读成"一"的箭头）常落在矩形外 —— 垂直方向多留余量，
        # 水平方向保持窄边距以免吃进相邻文字。
        pad_x, pad_y = 3, 10
        x0, y0 = max(0, x - pad_x), max(0, y - pad_y)
        x1 = min(img.width, x + w + pad_x)
        y1 = min(img.height, y + h + pad_y)
        if x1 - x0 < 5 or y1 - y0 < 5:
            return None
        b = np.asarray(img.crop((x0, y0, x1, y1)).convert("L"),
                       dtype=np.int32)
        lo, hi = int(b.min()), int(b.max())
        if hi - lo < 40:
            return None                      # 前景背景对比不足
        border = np.concatenate([b[0], b[-1], b[:, 0], b[:, -1]])
        bg = int(border.mean())
        span = hi - lo
        if bg > (lo + hi) // 2:              # 浅色背景，墨是暗的
            ink = b < lo + span * 0.45
        else:                                # 深色背景，墨是亮的
            ink = b > hi - span * 0.45
        if ink.sum() < 4:
            return None
        return ink
    except Exception:
        return None


def _h_arrow_dir(img, rect):
    """细长横条: 判断是否水平箭头（→ / ←）。

    特征: 箭头的翼=连续多列墨量明显高于主干，且翼集中在字形一端；
    真"一"字主干均匀（顿笔只造成单列增粗且无渐变）。
    返回 '→' / '←' / None。
    """
    ink = _ink_mask(img, rect)
    if ink is None:
        return None
    try:
        import numpy as np
        rows = ink.sum(axis=1)
        nzr = [i for i, v in enumerate(rows) if v > 0]
        if len(nzr) < 3:
            return None
        band = ink[nzr[0]:nzr[-1] + 1]
        col = band.sum(axis=0)
        nz = [(i, int(v)) for i, v in enumerate(col) if v > 0]
        if len(nz) < 6:
            return None
        i0, i1 = nz[0][0], nz[-1][0]
        shaft = int(np.median([v for _, v in nz]))
        if shaft < 1:
            return None
        # 箭头翼: 墨量 >= 主干+2 的连续列段
        head = [i for i, v in nz if v >= shaft + 2]
        if len(head) < 3:
            return None
        # 要求连续（翼是一整段）
        if max(head) - min(head) + 1 != len(head):
            return None
        rel = ((head[0] + head[-1]) / 2 - i0) / max(1, i1 - i0)
        if rel > 0.6:
            return "→"
        if rel < 0.4:
            return "←"
        return None
    except Exception:
        return None


def _v_arrow_dir(img, rect):
    """窄长竖条: 判断是否垂直箭头（↑ / ↓）。

    特征: 箭头尖端处墨宽远小于翼展，且翼集中在字形上端(↑)或下端(↓)；
    真"T"字顶部横杠就是最大宽度。
    返回 '↑' / '↓' / None。
    """
    ink = _ink_mask(img, rect)
    if ink is None:
        return None
    try:
        row = ink.sum(axis=1)
        nz = [i for i, v in enumerate(row) if v > 0]
        if len(nz) < 6:
            return None
        i0, i1 = nz[0], nz[-1]
        span = i1 - i0
        max_w = int(row.max())
        if max_w < 3:
            return None
        top2 = int(max(row[i0:i0 + 2]))
        bottom2 = int(max(row[i1 - 1:i1 + 1])) if i1 > 0 else 0
        argmax = int(row.argmax())
        rel = (argmax - i0) / max(1, span)
        # 翼(最大宽)在竖条中点以上 + 尖端很窄 -> ↑
        if rel < 0.4 and top2 <= max(2, max_w * 0.7):
            return "↑"
        # 翼在中点以下 + 底端很窄 -> ↓
        if rel > 0.6 and bottom2 <= max(2, max_w * 0.7):
            return "↓"
        return None
    except Exception:
        return None


def _fix_symbol_misreads(words: list, img=None) -> list:
    """修正词级符号误读（先文字规则粗筛，再像素形状验证）。"""
    out = []
    for i, (t, r) in enumerate(words):
        if t in _ARROW_MISREAD_MAP:
            t = _ARROW_MISREAD_MAP[t]
        elif t == "一" and img is not None and r and r[2] >= 2.5 * max(1, r[3]):
            # 细长横条形的孤立"一": 像素验证是否箭头
            d = _h_arrow_dir(img, r)
            if d:
                t = d
        elif t in ("T", "t") and img is not None and r and r[3] >= 1.8 * max(1, r[2]):
            # 窄长竖条形的 T/t: 像素验证是否箭头
            d = _v_arrow_dir(img, r)
            if d:
                t = d
        out.append((t, r))
    return out


def _extract_lines(result: dict) -> list:
    """从 OCR 结果提取结构化行: [{text, rect, words}, ...]"""
    lines = []
    for line in result.get("lines", []) or []:
        text = (line.get("text") or "").strip()
        words = []
        for w in line.get("words", []) or []:
            t = (w.get("text") or "").strip()
            r = w.get("bounding_rect") or {}
            if not t:
                continue
            words.append((t, (int(r.get("x", 0)), int(r.get("y", 0)),
                              int(r.get("width", 0)), int(r.get("height", 0)))))
        if not words:
            if text:
                lines.append({"text": text, "rect": None, "words": []})
            continue
        xs = [r[0] for _, r in words]
        ys = [r[1] for _, r in words]
        xe = [r[0] + r[2] for _, r in words]
        ye = [r[1] + r[3] for _, r in words]
        rect = (min(xs), min(ys), max(xe) - min(xs), max(ye) - min(ys))
        lines.append({"text": text, "rect": rect, "words": words})
    return lines


def _structure_from_lines(lines: list, img=None) -> str:
    """把结构化行重建成保留【行 / 段落 / 空格】的文本。

    - 行内: 词间水平空隙大 -> 补空格（还原缩进、多列、词距）
    - 行间: 同一视觉行(y 重叠)合并；垂直空隙明显大于正常行距 -> 空行分段
    img: 原识别图，用于箭头等符号的像素级误读修正（rect 与之同坐标系）
    """
    if not lines:
        return ""

    # 参考行高（中位数，抗异常值）
    heights = sorted(l["rect"][3] for l in lines if l.get("rect"))
    h = heights[len(heights) // 2] if heights else 20
    if h <= 0:
        h = 20

    # 1) 行内空格还原：按词间水平空隙重建行文本（先修正符号误读）
    for row in lines:
        ws = row.get("words")
        if not ws:
            continue
        ws = _fix_symbol_misreads(ws, img)
        row["words"] = ws
        parts = [ws[0][0]]
        for (_, r1), (t2, r2) in zip(ws, ws[1:]):
            gap = r2[0] - (r1[0] + r1[2])
            if gap > 2.0 * h:
                parts.append("    ")   # 大空隙（多列 / 深缩进）
            elif gap > 0.8 * h:
                parts.append("  ")     # 中等空隙（缩进 / 对齐空格）
            elif gap > 0.15 * h:
                parts.append(" ")      # 正常词间空格
            parts.append(t2)
        row["text"] = "".join(parts)

    # 2) 排序 + 同一视觉行合并（OCR 可能把一行拆成多个 line）
    rows = sorted(lines, key=lambda r: ((r["rect"][1] if r.get("rect") else 0),
                                        (r["rect"][0] if r.get("rect") else 0)))
    merged = []
    for row in rows:
        if (merged and row.get("rect") and merged[-1].get("rect")
                and abs(row["rect"][1] - merged[-1]["rect"][1]) < 0.5 * h):
            prev = merged[-1]
            gap_x = row["rect"][0] - (prev["rect"][0] + prev["rect"][2])
            sep = "    " if gap_x > 1.5 * h else " "
            prev["text"] += sep + row["text"]
            x0 = min(prev["rect"][0], row["rect"][0])
            y0 = min(prev["rect"][1], row["rect"][1])
            x1 = max(prev["rect"][0] + prev["rect"][2],
                     row["rect"][0] + row["rect"][2])
            y1 = max(prev["rect"][1] + prev["rect"][3],
                     row["rect"][1] + row["rect"][3])
            prev["rect"] = (x0, y0, x1 - x0, y1 - y0)
        else:
            merged.append({"text": row["text"], "rect": row.get("rect")})

    # 3) 段落还原：行间垂直空隙明显大于正常行距 -> 插入空行
    out = []
    prev = None
    for row in merged:
        if prev is not None and prev.get("rect") and row.get("rect"):
            gap_y = row["rect"][1] - (prev["rect"][1] + prev["rect"][3])
            if gap_y > 0.55 * h:
                out.append("")     # 段落之间的空行
        out.append(row["text"])
        prev = row
    # 兜底：拼接后残余的误读字符
    return "\n".join(out).replace("乛", "→")


def _recognize_text(im: Image.Image, lang: str) -> str:
    """单次完整识别，返回保留结构、修正符号误读的文本。"""
    result = winocr.recognize_pil_sync(im, lang=lang)
    lines = _extract_lines(result)
    text = _structure_from_lines(lines, im)
    if not text:
        text = (result.get("text") or "").strip()
    return text


_WORD_SET = None


def _load_wordset() -> set:
    """英文词表（ecdict 词典的词头），用于识别质量评分与词级修复。

    懒加载：首次调用耗时约 2~3 秒（77 万词），之后走缓存。
    """
    global _WORD_SET
    if _WORD_SET is not None:
        return _WORD_SET
    try:
        from dict_lookup import DEFAULT_CSV
        ws = set()
        with open(DEFAULT_CSV, encoding="utf-8", errors="ignore") as f:
            next(f)  # 表头
            for line in f:
                w = line.split(",", 1)[0].strip().lower()
                if w and w.isascii():
                    ws.add(w)
        _WORD_SET = ws
    except Exception:
        _WORD_SET = set()
    return _WORD_SET


_LEXICON = None


def _load_lexicon():
    """加载 OCR 词库（词频表 + 专名表），首次约几十毫秒。

    数据来自 lexicon_data.py（由 build_lexicon.py 从 ecdict.csv 生成，
    zlib+base64 常量）。两张表:
      - freq:  {word: rank} 榜单排名，越小越常用；缺键 = 无排名
      - names: {word} 专名（人名/地名/月份星期/品牌），用于保护句中人名
    加载失败时返回空表，各调用点会自然退化成"无仲裁/无保护"的旧行为。
    """
    global _LEXICON
    if _LEXICON is not None:
        return _LEXICON
    freq, names = {}, frozenset()
    try:
        import base64
        import zlib
        import lexicon_data
        raw = zlib.decompress(base64.b64decode(lexicon_data.FREQ_B64))
        for line in raw.decode("utf-8").split("\n"):
            if not line:
                continue
            w, _, r = line.partition("\t")
            try:
                freq[w] = int(r)
            except ValueError:
                pass
        raw = zlib.decompress(base64.b64decode(lexicon_data.NAMES_B64))
        names = frozenset(x for x in raw.decode("utf-8").split("\n") if x)
    except Exception as e:
        log.debug("OCR 词库加载失败（退化为无仲裁）: %s", e)
    _LEXICON = (freq, names)
    return _LEXICON


def _load_wordfreq() -> dict:
    """词频表 {word: rank}（榜单排名，越小越常用）。"""
    return _load_lexicon()[0]


def _load_proper_names() -> frozenset:
    """专名表（人名/地名/月份星期/品牌）。"""
    return _load_lexicon()[1]


def _text_quality(text: str) -> int:
    """文本质量评分: 用于多个识别结果之间择优。

    - CJK/常用符号加分、字母数字小加分、乱码符号扣分
    - 拉丁文为主的文本里出现全角标点（．，：）或怪符号（℃）是
      中文引擎误读英文的典型特征，要重罚
    """
    q = 0
    n_latin = 0
    n_cjk = 0
    n_fullwidth = 0
    for c in text:
        o = ord(c)
        if (0x2E80 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF
                or 0xAC00 <= o <= 0xD7AF):
            q += 3
            n_cjk += 1
        elif c.isascii() and c.isalpha():
            q += 1
            n_latin += 1
        elif c.isdigit():
            q += 1
        elif c in "，。！？、：；（）【】《》“”‘’…—→←↑↓✓✗·％":
            q += 2 if n_cjk >= n_latin else -2
            if o > 0x2000:   # 全角标点
                n_fullwidth += 1
        elif c in "℃℉￥￡￠§№☆★○●◎◇◆□■":
            q -= 3
            n_fullwidth += 1
        elif not c.isspace():
            q -= 1
    # 拉丁文为主却混入大量全角标点 => 误读文本，整体降权
    if n_latin > n_cjk and n_fullwidth:
        q -= 4 * n_fullwidth
    # 词典命中率评分: 拉丁文为主时，不是英文单词的词（如 trip 误读成
    # 的 tnp、断裂的 Exped）按个扣分——真词（trip/then/bring）之间的
    # 候选由此拉开差距，专治小字号的形近字误读。
    if n_latin > n_cjk:
        ws = _load_wordset()
        if ws:
            for tok in re.findall(r"[A-Za-z]{3,}", text):
                if tok.lower() not in ws:
                    q -= 3
    return q


# 拉丁文为主的文本里，全角标点/怪符号一律还原成半角
# （中文引擎读英文的典型误读: "1 ，500" / "next ．" / "go℃"）
_FULL2HALF = str.maketrans(
    "．，：；！？（）【】《》“”‘’、\u3000ＡＢＣ",
    ".,:;!?()[]<>\"\"'', ABC",
)
# OCR 把括号 ' 判丢时会留下 "don t" / "I m" 这类被错误分开的词对。
# 安全合并的两种授权: a) 前缀是常见缩写主词（直接接受）；b) 拼成的
# "a'b" 整体作为合法英文缩写表中的一项（防 "go t" -> "go't" 之类效率低的误改）。
_APOS_PREFIXES = frozenset({
    # be 动词变位 + 否定主词
    "i", "you", "he", "she", "we", "they", "it",
    "don", "won", "can", "didn", "doesn", "hasn", "haven", "hadn",
    "isn", "aren", "wasn", "weren", "couldn", "shouldn", "wouldn",
    "mightn", "mustn", "needn", "shall",
    # 缩写主词
    "let", "who", "what", "where", "when", "how", "why",
    "here", "there",
    # 名物主 / 缩写尾巴
    "o", "ma", "ol",
    # OCR 误识: "I've" 首字母 I 在小字下常被读成 l / 1
    # (Lusterfield is all l've ever known)。单字母 i/l/1 在英文里
    # 几乎不会单独出现在实词后跟一个短词的位置，所以加进前缀集合是安全的
    "l", "1",
})
# 合法英文缩写全集（拼成 a'b 时整体查这个表即可）。覆盖 be 动词、
# 否定缩写、杂项（ma'am/o'clock）。足够覆盖 99% 场景。
_APOS_FORMS = frozenset({
    "i'm", "i've", "i'll", "i'd",
    "you're", "you've", "you'll", "you'd",
    "he's", "he'd", "he'll",
    "she's", "she'd", "she'll",
    "we're", "we've", "we'll", "we'd",
    "they're", "they've", "they'll", "they'd",
    "it's", "it'd", "it'll",
    "that's", "that'd", "that'll",
    "there's", "there'd", "there'll", "there've",
    "here's", "here'd", "here'll",
    "what's", "what'd", "what'll", "what've",
    "where's", "where'd", "where'll", "where've",
    "when's", "who's", "who'd", "who'll", "who've",
    "how's", "how'd", "how'll",
    "let's",
    "don't", "doesn't", "didn't",
    "won't", "wouldn't",
    "can't", "couldn't",
    "isn't", "aren't", "wasn't", "weren't",
    "hasn't", "haven't", "hadn't",
    "shouldn't", "mightn't", "mustn't", "needn't", "shan't",
    "ma'am", "o'clock", "o'er", "ne'er", "ol'",
    "'tis", "'twas",
})
# 能接 "'t" 的缩写主词（不含撇号的形态）。用于把 "donT"/"canT"/"wonT"
# 这类"尾部字母被 OCR 抬成大写"的缩写还原成 "don't"/"can't"/"won't"
# —— 这些词在词典里只有带撇号形态，查不到 "dont"/"cant"，所以需要单独的表。
_APOS_T_STEMS = frozenset({
    "don", "won", "can", "isn", "aren", "wasn", "weren", "didn", "doesn",
    "hasn", "haven", "hadn", "couldn", "shouldn", "wouldn", "mightn",
    "mustn", "needn", "shan",
})



def _normalize_latin(text: str) -> str:
    """拉丁文为主的文本: 全角标点转半角、数字间的全角逗号还原。

    OCR 偶尔会输出 "goatS,that" 这种 "标点贴词" 的串（标点前后无空格，
    影响下一阶段按空格切词）。在归一化阶段直接补空格，保持文本后续
    加工的可预期性。"a.5" / "2,500" / 句末标点这种不会被破坏。
    """
    n_latin = sum(1 for c in text if c.isascii() and c.isalpha())
    n_cjk = sum(1 for c in text if 0x2E80 <= ord(c) <= 0x9FFF)
    if n_latin <= n_cjk:
        return text
    t = text.translate(_FULL2HALF)
    # "1 ，500" / "1 ， 500" -> "1,500"
    t = re.sub(r"(?<=\d)\s*[，,]\s*(?=\d{3}\b)", ",", t)
    # 标点后补空格（仅 OCR 吞掉的情况）
    # - 不补: 句末 / 小数 / 千分位 / 闭合括号 → 空白这类
    # - 补: ,;:?! 后紧跟字母/开括号/方括号/引号
    t = re.sub(r"(?<=[,.;:?!])(?=[A-Za-z(\\[\"'])", " ", t)
    return t


def _repair_latin_words(text: str) -> str:
    """拉丁文为主的词级修复（在多候选择优之后做最终清理）。

    - 词尾误读大写 O -> o（"tO Sebas" -> "to Sebas"；真词尾大写 O
      基本不存在，命中条件要求词内其余字母全是小写，不会伤及专名）
    - 词内断裂合并（"Exped ition" -> "Expedition"）: 前半不是英文词、
      拼起来是 => 合并（用 ecdict 词表验证，纯猜测不合并）
    - 标点前的空格（"2 . Sebas" -> "2. Sebas"）
    - 词内错位大写（"BasiCS" -> "Basics"）: Windows OCR 小字常把部分
      字母误判成大写；小写形式是词典词时按词典规范还原

    注意: 下面所有步骤都按"空格"分词，换行符会粘在行首词的第一个字符上
    （"\ngoatS" 会被当成非字母开头而整词跳过）。所以先按行拆开，每行独立
    修复再拼回 —— 否则每行的第一个词永远修不了（实测 "the\ngoatS" 的
    goatS、"desperately\nt1Ying" 的 t1Ying 都因此漏修）。
    """
    if "\n" in text:
        return "\n".join(_repair_latin_words(p) for p in text.split("\n"))
    n_latin = sum(1 for c in text if c.isascii() and c.isalpha())
    n_cjk = sum(1 for c in text if 0x2E80 <= ord(c) <= 0x9FFF)
    if n_latin <= n_cjk:
        return text
    # 0) 提前加载词典：后面的 "r/l/i 误识为 1" / 词内断裂合并 / 吞空格还原
    #    都用同一份 ecdict，避免重复 IO（启动期一次性加载约 30ms）
    ws = _load_wordset()
    # 闭集功能词（短小、最高频，OCR 吞空格后剩多个连续闭集词很常见）。
    # 2.5) 数字误识还原 与 3.7) 吞空格还原 共用这一份。
    _SWALLOWED_FRONT = frozenset({
        "of", "to", "in", "on", "at", "by", "is", "it", "an", "as",
        "or", "do", "if", "be", "no", "so", "we", "he", "my",
        "but", "had", "has", "his", "for", "the", "not", "out",
        "off", "her", "him", "who", "how", "did", "was", "all",
        "one", "any", "up", "down",
        # 高频闭集介词/连词（OCR 吞空格后能可靠组成多词组合）
        "with", "upon", "from", "until", "while", "since",
        "than", "that", "into", "also",
    })
    _SWALLOWED_BACK = _SWALLOWED_FRONT | {
        "their", "more", "some", "each", "them", "this",
        "that", "these", "those",
        # 人称代词（OCR 吞空格后剩 ofus/onme/intomy 很常见）
        "me", "us", "you", "him", "her", "its", "she",
        # 缩写后缀（OCR 把 "we've" 读成 "we ve" 时 ve 是合法词）
        "ve", "ll", "re",
    }
    # 1) 词尾大写 O（前文全小写）-> o
    text = re.sub(r"\b([a-z][a-z]*)O\b", r"\1o", text)
    # 2) 标点前多余空格（>=2 才修，正常单空格不动；"_normalize_latin"
    #    已补过 OCR 吞掉的"标点后空格"，这里负责"标点前多余空格"）
    text = re.sub(r" {2,}([,.:;!?])", r"\1", text)
    # 2.5) 字母误识为数字的还原。Windows OCR 小字常把字母读成数字，实测:
    #      "trying" -> "t1Ying"、"also" -> "a1so"、"cock" -> "c0ck"、
    #      "point" -> "P01nt"（同时误识两个字母）。
    #      词典里"字母 + 数字 + 字母"的纯字母词几乎不存在，所以这种形态
    #      基本都是误识。规则:
    #      - 作用域: token 首尾都是字母、长度 >= 4、数字个数 1~3
    #        （首尾字母护栏已排除纯数字 "160"、"B2B"、"A1"、
    #         "H2SO4"/"sha256"/"R2D2"/"C3PO" 这类型号编号——它们的
    #         末尾一位就是数字；"C3PO" 那类即使进了也会因 3 无候选表而
    #         原样退回）
    #      - 按 _DIGIT_FIX 表把每个数字换成候选字母，**组合枚举**
    #        （"P01nt" 需要 0->o 且 1->i 同时成立，逐位单独替换凑不出
    #         point）
    #      - 任一组合替换后是合法英文词 -> 采用（第一优先级）
    #      - 否则替换后是"吞空格形态"（"Ofy0ur" -> "Ofyour" -> of+your）
    #        也采用，交给 3.7) 继续拆
    #      - 都不命中 -> 不动（保守，不误伤真实型号/编号）
    _DIGIT_FIX = {
        "1": ("r", "l", "i"),   # r/l/i 最易被读成 1
        "0": ("o",),            # o 被读成 0
        "5": ("s",),
        "2": ("z",),
        "6": ("g", "b"),
        "8": ("b",),
        "9": ("g", "q"),
        "4": ("a",),
        "7": ("t",),
    }

    def _looks_swallowed(w: str) -> bool:
        """w 是否像"吞了空格"的形态（闭集前缀 + 词典/闭集词）。"""
        for j2 in range(2, min(5, len(w))):
            if w[:j2] in _SWALLOWED_FRONT and (
                    w[j2:] in ws or w[j2:] in _SWALLOWED_FRONT
                    or w[j2:] in _SWALLOWED_BACK):
                return True
        return False

    if ws:
        toks = text.split(" ")
        out15 = []
        for tok in toks:
            core = tok.strip(".,:;!?()[]\"'…")
            if (not core or len(core) < 4
                    or not core[0].isalpha() or not core[-1].isalpha()):
                out15.append(tok)
                continue
            # 数字个数 1~3（"P01nt" 是 2 位：0->o、1->i 需同时成立）
            digs = [i for i, c in enumerate(core) if c.isdigit()]
            if not 1 <= len(digs) <= 3:
                out15.append(tok)
                continue
            cands_list = [_DIGIT_FIX.get(core[i]) for i in digs]
            if any(cl is None for cl in cands_list):
                out15.append(tok)
                continue
            total = 1
            for cl in cands_list:
                total *= len(cl)
            if total > 64:          # 组合爆炸保护（正常最多 3x1x... 很小）
                out15.append(tok)
                continue
            combos = []
            for combo in itertools.product(*cands_list):
                cand = core
                for i_d, ch in zip(digs, combo):
                    cand = cand[:i_d] + ch + cand[i_d + 1:]
                combos.append(cand)
            # 第一优先级: 直接命中词典 —— 若多个组合都是合法词，用**词频仲裁**
            # 择优（"有排名者优先、排名小者优先"）。这一步治的正是候选顺序
            # 带来的系统性歧义: "th1s" 的候选 thrs/thls/this 都是词典词，
            # 按 _DIGIT_FIX 顺序会先命中 thrs；而 this(rank 20) 有排名、
            # thrs 无排名，仲裁后稳定选中 this。同理 wait(400) 胜过
            # wart(15251)。"w0rld" 的 wld/tlme 这类无排名垃圾候选也一并出局。
            freq = _load_wordfreq()
            new_core = None
            best_rank = None
            for cand in combos:
                low_c = cand.lower()
                if low_c not in ws:
                    continue
                r = freq.get(low_c)
                if new_core is None or (r is not None
                                        and (best_rank is None or r < best_rank)):
                    new_core = cand
                    best_rank = r
            if new_core is None:           # 第二优先级: 吞空格形态
                for cand in combos:
                    if _looks_swallowed(cand.lower()):
                        new_core = cand
                        break
            if new_core is None:
                out15.append(tok)
                continue
            # 把 core 段替换回 tok（保留前后标点）
            lead = tok[:len(tok) - len(tok.lstrip(".,:;!?()[]\"'…"))]
            trail = tok[len(tok.rstrip(".,:;!?()[]\"'…")):]
            out15.append(lead + new_core + trail)
        text = " ".join(out15)
    # 3) 词内断裂合并（词典验证）
    _PUNCT = ".,:;!?()[]\"'…"
    if ws:
        toks = text.split(" ")
        out = []
        for tok in toks:
            if out:
                prev = out[-1]
                a = prev.strip(_PUNCT)
                b = tok.strip(_PUNCT)
                joined = a + b
                if (a and b and b[:1].islower() and len(joined) >= 4
                        and joined.lower() in ws
                        and (a.lower() not in ws or b.lower() not in ws)):
                    # 用去标点后的 a+b 拼接（"Exped. ition" -> "Expedition"，
                    # 之前 prev+tok 会得到 "Exped.ition"），保留 prev 的
                    # 前导标点（如引号/括号）和 tok 的尾随标点（如逗号）
                    lead = prev[:len(prev) - len(prev.lstrip(_PUNCT))]
                    trail = tok[len(tok.rstrip(_PUNCT)):]
                    out[-1] = lead + joined + trail
                    continue
            out.append(tok)
        text = " ".join(out)
        # 3.5) 撇号合并: OCR 把撇号 ' 当成词间空隙丢了，于是 "don't"
        #      被切成 "don t"、"I'm" -> "I m"、"won't" -> "won t"。
        #      合并条件: 两侧都是纯字母短词、合并后形如 "词+'t/m/s/d/re/ll/ve"
        #      这种合法缩写；为安全起见只在前缀是已知缩写主词或合并结果在
        #      词典里命中时执行。
        # 英文缩写合法后缀（OCR 把这些单字母词当独立词常出现）
        _APOS_SUFFIXES = ("t", "m", "s", "d", "re", "ll", "ve")
        toks = text.split(" ")
        ap_out = []
        for tok in toks:
            if ap_out:
                prev = ap_out[-1]
                a = prev.strip(_PUNCT)
                b = tok.strip(_PUNCT)
                if (a.isalpha() and b.isalpha() and len(b) <= 3
                        and len(a) >= 1
                        and b.lower() in _APOS_SUFFIXES
                        # 前缀是常见缩写主词（don/won/can/did/had/have/is/
                        # it/I/let/who/you/she/he/we/they 等）直接接受。
                        # 否则要求 "a'b" 整体作为词在词典里（防止随机合并
                        # 像 "go t" -> "go't" 这种无效形态）。
                        and (a.lower() in _APOS_PREFIXES
                             or (a + "'" + b).lower() in ws
                             or (a + "'" + b).lower() in _APOS_FORMS)):
                    lead = prev[:len(prev) - len(prev.lstrip(_PUNCT))]
                    trail = tok[len(tok.rstrip(_PUNCT)):]
                    ap_out[-1] = lead + a + "'" + b + trail
                    continue
            ap_out.append(tok)
        text = " ".join(ap_out)
        # 3.7) 吞空格还原（"ofthe" / "intothe" / "tothe" / "onhis" /
        #      "oneofthe" 这类 OCR 把相邻词之间的空格给吃了的形态）:
        #      闭集功能词前缀的贪心拆分算法。安全护栏:
        #      - token 长度 [5, 16]
        #      - 不在词典里（词典里有就保留）
        #      - Mac/Mc 专名（macOS/McDonald 等）不切
        #      - 末段也必须在词典里 / 是闭集词
        # （闭集词表 _SWALLOWED_FRONT / _SWALLOWED_BACK 已提到函数开头，
        #   与 2.5) 数字误识还原 共用同一份）
        # 词典前缀取最长候选（"into"/"with" 等也是合法词），
        # 闭集前缀只能在词典前缀 < 闭集前缀长度时才补位。

        def _can_split_completely(s: str) -> bool:
            """剩余段 s 能否被前缀（词典/闭集之一）贪心拆完到合法末段。
            限制递归以避免指数退化（实际深度被 min(8, ...) 限到 < 16）。
            注意这只是 ok 标记，不修改 splits；调用方据此决定是否切。
            """
            if not s:
                return True
            if len(s) > 16:
                return False
            for j2 in range(min(8, len(s)), 1, -1):
                if s[:j2] in ws:
                    if j2 == len(s) or _can_split_completely(s[j2:]):
                        return True
            for j2 in range(min(4, len(s) - 1), 1, -1):
                if s[:j2] in _SWALLOWED_FRONT:
                    if j2 == len(s) or _can_split_completely(s[j2:]):
                        return True
            return False
        toks = text.split(" ")
        gap_out = []
        for tok in toks:
            core = tok.strip(_PUNCT)
            low = core.lower()
            # 首字母大写过去一律当专名跳过（Lusterfield / Mystertown）。
            # 但 OCR 吞空格时会把 "of your" 读成 "Ofyour"（顺带把首字母
            # 抬成大写，见截图 "size Ofyour Cock"），所以改为允许大写词进入
            # 拆分，但**只走闭集前缀路径**（见下方 restrict_dict / is_upper），
            # 绝不走"词典最长前缀"。这样 "Ofyour" -> "of your"，
            # 而 Lusterfield/Moonlight 这类专名切不出来，仍保持原样。
            # 长度护栏: 4 字符形态进入拆分时，要求"双闭集"形态才切
            # （防止 over / only / hand / long 这类 4 字符合法词被误拆）。
            if (not core.isalpha() or len(core) < 4 or len(core) > 16
                    or low in ws
                    or low[:3] == "mac" or low[:2] == "mc"):
                gap_out.append(tok)
                continue
            # 词中带大写 = 大小写误识信号，不是吞空格。吃空格留下的产物
            # 是纯小写（顶多首字母被屏幕的大写规则抬起来，"Ofyour"），
            # 词中间冒大写只可能是 OCR 判错大小写（"donT"/"WhO"），
            # 交给第 4 步还原；在这里切会切出垃圾（"donT" -> "do nt"）。
            if any(c.isupper() for c in core[1:]):
                gap_out.append(tok)
                continue
            is_upper = core[0].isupper()
            # 闭集优先的多段贪心拆分。
            # 关键约束: 闭集前缀的候选剩余段必须能继续切分到尾部
            # （末段仍是合法词），否则回退到词典最长前缀。这样能避免
            # "ofthe" 被错误切成 "oft he"（要求"of + the"），同时让
            # "into the" 走"into(词典) + the(闭集)"而非"in to the"。
            #
            # 4 字符形态（如 ofmy / tomy / onit）只能走双闭集路径：
            # 闭集前缀(2~4) + 整段在闭集/词典。否则不切（防 over/hand
            # 这种 4 字符合法词被误拆）。
            is_short = len(core) == 4
            splits = []
            pos = 0
            ok = True
            while True:
                remaining = low[pos:]
                if not remaining:
                    break
                # 末段已合法（remaining 整体在词典/闭集），直接收尾
                if (remaining in ws or remaining in _SWALLOWED_FRONT
                        or remaining in _SWALLOWED_BACK):
                    if pos > 0:
                        splits.append(pos)
                    break
                # 三级优先级: 1) 短左闭集 + 右侧为闭集/词典词/可继续切; 2) 词典最长前缀; 3) 单闭集
                cut, cut_pos = 0, -1
                # 1) 短左闭集（j2 从 2 起）+ 右侧合法
                best_pair = 0
                best_pair_pos = -1
                for j2 in range(2, min(5, len(remaining))):
                    cand = remaining[:j2]
                    rest = remaining[j2:]
                    # 双闭集: 闭集前 + 整段闭集/词典词 (不递归)
                    # 这一层要"短闭集 + 实体词"才赢 - 防止 in+to the 这种多段误选
                    if cand in _SWALLOWED_FRONT and (
                            rest in _SWALLOWED_FRONT
                            or rest in _SWALLOWED_BACK
                            or rest in ws):
                        best_pair = j2
                        best_pair_pos = pos + j2
                        break
                # 2) 词典最长前缀（剩余段可继续切）
                best_dict = 0
                best_dict_pos = -1
                if not is_short and not is_upper:
                    # 4 字符 / 首字母大写形态只走闭集前缀，防 over→o ver、
                    # 也防专名被"词典前缀"切成两段（Moonlight→moon light）

                    for j2 in range(min(8, len(remaining)), 1, -1):
                        cand = remaining[:j2]
                        if cand in ws and j2 > best_dict:
                            rest = remaining[j2:]
                            if j2 == len(remaining) or _can_split_completely(rest):
                                best_dict = j2
                                best_dict_pos = pos + j2
                # 3) 单闭集前缀
                best_sw = 0
                best_sw_pos = -1
                for j2 in range(min(4, len(remaining) - 1), 1, -1):
                    cand = remaining[:j2]
                    if cand in _SWALLOWED_FRONT:
                        rest = remaining[j2:]
                        if j2 == len(remaining) or _can_split_completely(rest):
                            best_sw = j2
                            best_sw_pos = pos + j2
                            break
                if best_pair > 0:
                    cut, cut_pos = best_pair, best_pair_pos
                elif best_dict > 0:
                    cut, cut_pos = best_dict, best_dict_pos
                elif best_sw > 0:
                    cut, cut_pos = best_sw, best_sw_pos
                else:
                    ok = False
                    break
                # 安全: 防止死循环（pos 必须推进）
                if cut_pos <= pos:
                    ok = False
                    break
                if cut_pos == len(low):
                    if pos > 0:
                        splits.append(pos)
                    break
                if pos > 0:
                    splits.append(pos)
                pos = cut_pos
            if not ok or not splits:
                gap_out.append(tok)
                continue
            # 末段验证
            last = low[splits[-1]:] if splits else low
            if (last not in ws and last not in _SWALLOWED_FRONT
                    and last not in _SWALLOWED_BACK):
                # 末段不合法, 撤销最后一次切分
                if len(splits) >= 2:
                    splits = splits[:-1]
                    last = low[splits[-1]:]
                    if (last not in ws and last not in _SWALLOWED_FRONT
                            and last not in _SWALLOWED_BACK):
                        gap_out.append(tok)
                        continue
                else:
                    gap_out.append(tok)
                    continue
            # 还原大小写: 吞空格是 OCR 错误，拆开的词一律小写（"Ofyour"
            # -> "of your"）。只有当 token 处于**句首**（文本第一个词，或
            # 前一个已输出 token 以句末标点结尾）时才保留首字母大写。
            parts = []
            prev_i = 0
            for sp in splits:
                parts.append(core[prev_i:sp])
                prev_i = sp
            parts.append(core[prev_i:])
            keep_cap = False
            if core[0].isupper():
                prev_tok = gap_out[-1] if gap_out else ""
                pv = prev_tok.rstrip()
                keep_cap = (not pv) or pv[-1] in ".!?…:"
            rebuilt = []
            for i_p, p in enumerate(parts):
                if i_p == 0 and keep_cap:
                    rebuilt.append(p[:1].upper() + p[1:].lower())
                else:
                    rebuilt.append(p.lower())
            new_core = " ".join(rebuilt)
            if tok and tok[-1] in _PUNCT and not new_core.endswith(tok[-1]):
                trail = tok[-1]
            else:
                trail = ""
            gap_out.append(new_core + trail)
        text = " ".join(gap_out)

        # 4) 词内错位大写（"BasiCS" -> "Basics"、"goatS" -> "goats"）。
        #    命中条件拆两条:
        #    a) 位置>=1 的字母里混有 >=2 个大写（覆盖 BasiCS 类多字母误判，
        #       不会伤 McDonald/McCree/GoatS——它们都只有1个内部大写）
        #    b) 仅尾字母大写（其它字母都小写）：英文里尾字母大写几乎都是
        #       OCR 误识（小字字体尾巴被抬高一截就判成大写），按词典还原。
        #    Mac/Mc 前缀专名先豁免；全大写缩略语不动。
        def _fix_case(core: str) -> str:
            if core.isupper():          # 全大写（缩略语/标题）不动
                return core
            if core[:3].lower() == "mac" or core[:2].lower() == "mc":
                return core             # Mac/Mc 前缀专名（macOS/McDonald）不动
            inner_upper = sum(1 for c in core[1:] if c.isupper())
            low = core.lower()
            # 条件 a: 多个内部大写（BasiCS），按词典规范还原
            if inner_upper >= 2 and low in ws:
                return core[0] + low[1:] if core[0].isupper() else low
            # 条件 d 必须排在条件 b 前面: "canT"/"wonT"/"isnT" 的小写形式
            # "cant"/"wont"/"isnt" 里前两个本身是词典里的真词（cant=伪善之
            # 言、wont=习惯），条件 b 会抢先返回 "cant"，把缩写判没了。
            # 条件 d: 缩写尾部误大写（donT -> don't / canT -> can't /
            #    wonT -> won't）。词典里没有 "dont"/"cant" 这类无撇号形态
            #    （只有 "don't"），条件 b 的"小写形式在词典里"过不了，
            #    只能靠"能接 't 的缩写主词表"来判。首字母大小写原样保留
            #    （句首 "DonT" -> "Don't"）。
            if (inner_upper == 1 and core[-1].isupper()
                    and core[1:-1].islower() and core[:-1] in _APOS_T_STEMS):
                return core[:-1] + "'t"
            # 条件 b: 词尾单大写（goatS/basicS/WhO/ThE），按词典还原。
            #    首字母的大小写**原样保留**——OCR 既可能把句中词首抬成大写
            #    （"patrons WhO"），也可能本来就在句首（"WhO are you"），
            #    保留首字母大小写两种情况都不会错。
            if (inner_upper == 1 and core[-1].isupper() and low in ws
                    and core[1:-1].islower()):
                return core[0] + low[1:]
            # 条件 c: 词中间夹一个孤立大写（"trying" 被读成 "trYing"）。
            # 真英文词里"两侧都是小写字母的孤立大写"几乎不存在（驼峰词
            # 如 iPhone 不受影响——照词典小写化无害）；侧写护栏:
            # 全词仅 1 个大写、至少 3 个小写（排除 USA/USt 这类缩写）、
            # 长度 >= 4、小写形式在词典里。
            if (inner_upper == 1 and core[0].islower() and core[-1].islower()
                    and len(core) >= 4
                    and sum(1 for c in core if c.islower()) >= 3
                    and low in ws):
                i_up = next(i for i, c in enumerate(core) if c.isupper())
                if 0 < i_up < len(core) - 1 and core[i_up - 1].islower() \
                        and core[i_up + 1].islower():
                    return low
            return core
        # 5) 句中"首字母误大写"还原（"your Cock" -> "your cock"）。
        #    Windows OCR 小字偶尔把句中实词首字母判成大写（截图实测
        #    "size Ofyour Cock"）。句中实词首字母大写本来只该出现在专名，
        #    所以护栏必须严、宁缺毋滥（不碰专名）:
        #      - 形如 "Xxxx"（仅首字母大写、其余全小写、长度>=3）
        #      - 小写形式在 ecdict 词典里（排除 Lusterfield/Sebas 这类
        #        词典查不到的专名）
        #      - 不在句首（前面有词，且不以句末标点结尾）
        #      - 前面（可隔最多 2 个"纯小写词典词"）出现限定词/物主代词/
        #        介词（your/the/of/with...）: 这个位置的名词几乎不可能
        #        是专名（专名多为裸词位置）
        _DET_CTX = frozenset({
            "the", "a", "an", "this", "that", "these", "those",
            "my", "your", "his", "her", "its", "our", "their",
            "some", "any", "no", "every", "each", "both", "all",
            "such", "another", "other", "many", "much", "few", "several",
            "of", "in", "on", "at", "to", "from", "with", "by", "for",
            "into", "onto", "over", "under", "above", "below", "near",
            "behind", "beside", "between", "among", "through", "against",
            "about", "around", "along", "across", "before", "after",
            "during", "without", "within", "upon", "toward", "towards",
        })
        _DET_HARD = frozenset({      # 限定词/物主代词（允许隔词回看）
            "the", "a", "an", "this", "that", "these", "those",
            "my", "your", "his", "her", "its", "our", "their",
            "some", "any", "no", "every", "each", "both", "all",
            "such", "another", "other", "many", "much", "few", "several",
        })
        toks5 = text.split(" ")

        # 封闭类功能词（限定词/代词/介词/连词/wh-词）。这些词在任何语言
        # 位置都不可能是专名，所以只要不在句首，句中写成大写就是 OCR 误识，
        # 可以直接还原成小写——不需要上下文护栏。
        # 实测场景: "with patrons WhO" 里的 WhO 经条件 b 还原成 "Who" 后，
        # 因为中间隔了 patrons（回看层只认 _DET_HARD 限定词，不认 with 这类
        # 介词）而判不出句中大写，停在 "patrons Who"。补这条规则才对。
        # 注意不含 will/may/rose/mark 这类"兼作人名的词"。
        _FUNC_WORDS = frozenset({
            "who", "whom", "whose", "what", "when", "where", "why", "how",
            "which", "that", "this", "these", "those", "the", "and", "but",
            "or", "if", "then", "than", "so", "not", "there", "here",
            "with", "from", "into", "onto", "upon", "about", "after",
            "because", "while", "until", "since", "though", "over", "under",
            "between", "through", "against", "without", "within", "during",
            "before", "towards", "toward", "above", "below", "around",
        })

        def _at_sentence_start(idx: int) -> bool:
            """idx 处是否处于句子/分句开头（句首大写是正常的，不能小写化）。"""
            if idx <= 0:
                return True
            return toks5[idx - 1][-1] in ".!?…:"

        def _mid_sentence_cap_ok(idx: int) -> bool:
            """idx 处的实词是否"句中首字母误大写"（可还原）。

            前一个词是限定词/物主代词/介词即命中；否则再向前回看最多 2
            个"纯小写词典词"，中间遇到限定词/物主代词也算命中（覆盖
            "the sheer Size" / "the giant Cock" 这类形容词隔开的情况）。
            遇到大写词 / 词典外词 / 句末标点即放弃——所以专名
            （Mark/Rose/Sebas）不会被误改。回看里不认介词，避免
            "of Mark" 这种"介词 + 专名"被误判。
            """
            j = idx - 1
            back = 0
            while j >= 0:
                raw = toks5[j]
                core_p = raw.strip(_PUNCT)
                if not core_p or raw[-1] in ".!?…:":
                    return False
                lowp = core_p.lower()
                if lowp in (_DET_CTX if back == 0 else _DET_HARD):
                    return True
                if not core_p.islower() or lowp not in ws or back >= 2:
                    return False
                back += 1
                j -= 1
            return False

        # 专名表护栏: 命中专名表的大写词（Mark/Rose/Paris/June...）即使处于
        # "限定词/介词 + 大写实词"的位置也不还原。回看层把介词也算作触发
        # 条件（换来 at Night -> at night、of Course -> of course 的收益），
        # 代价是 "with Mark" 这类"介词 + 人名"会被误改小写；靠这张表兜住。
        # 表里刻意排除了 will/may 等情态助动词（见 build_lexicon.py）。
        _PROPER = _load_proper_names()
        fixed_toks = []
        for i_t, tok in enumerate(toks5):
            core = tok.strip(_PUNCT)
            if core.isalpha() and len(core) >= 3 and core != core.lower():
                fixed = _fix_case(core)
                if fixed != core:
                    tok = tok.replace(core, fixed)
                    core = fixed
                if (core[0].isupper() and core[1:].islower()
                        and core.lower() in ws
                        and core.lower() not in _PROPER
                        and (_mid_sentence_cap_ok(i_t)
                             or (core.lower() in _FUNC_WORDS
                                 and not _at_sentence_start(i_t)))):
                    tok = tok.replace(core, core.lower())
            fixed_toks.append(tok)
        text = " ".join(fixed_toks)
    return text


# ===== 引擎路由 =====
# ocr_engine 取值:
#   rapidocr —— RapidOCR / PP-OCRv6（高精度，识别更准，单次约 0.5~0.9 秒）
#   windows  —— Windows.Media.Ocr（极快，约 0.05~0.2 秒，误读略多）
#   auto     —— 两个都跑，按评分择优（最稳，代价是两次识别）
# 说明: rapidocr 跑在独立子进程里（见 ocr_rapid.py / ocr_worker.py），
#      不可用时自动回退 Windows 引擎，识别链路不会因为第三方引擎中断。
OCR_ENGINES = ("rapidocr", "windows", "auto")


def _score_text(text: str) -> int:
    """候选文本评分（auto 模式择优用）。

    = 基础质量分（CJK/字母/乱码权重）
      + 英文词命中 ECDICT 的加分、词表外词扣分
      - "字母夹数字"（t1Ying / c0ck）这类强误识特征的扣分
    只在拉丁文为主时启用词表打分，中文文本不受影响。
    """
    if not text:
        return -1 << 30
    score = _text_quality(text)
    n_lat = sum(1 for c in text if c.isascii() and c.isalpha())
    n_cjk = sum(1 for c in text
                if 0x2E80 <= ord(c) <= 0x9FFF or 0xAC00 <= ord(c) <= 0xD7AF)
    if n_lat >= 4 and n_lat > n_cjk:
        toks = re.findall(r"[A-Za-z][A-Za-z']*", text)
        ws = _load_wordset()
        if toks and ws:
            hit = sum(1 for t in toks if t.lower() in ws)
            score += 4 * hit - 5 * (len(toks) - hit)
        score -= 6 * len(re.findall(r"[A-Za-z]\d|\d[A-Za-z]", text))
    return score


def _rapid_text(img: Image.Image, lang: str):
    """RapidOCR 识别 + 同一套拉丁文后处理。不可用/失败返回 None。"""
    try:
        from ocr_rapid import get_client
    except Exception as e:
        log.debug("RapidOCR 客户端不可用: %s", e)
        return None
    try:
        text = get_client().recognize(img, lang)
    except Exception as e:
        log.debug("RapidOCR 识别异常: %s", e)
        return None
    if text is None:
        return None
    return _normalize_latin(text)


# ===== 视觉换行合并 =====
# 行尾出现这些字符 = 真句子边界，换行要保留
_SENT_END_CHARS = ".!?…。！？；;"
# "Reward: ..." / "Objective: ..." 这类标签行；标签必须独占一行，
# 否则 translator 里的 GAME_LABELS 拆分逻辑会失效
_LABEL_LINE_RE = re.compile(r"^\s*[A-Za-z][A-Za-z ]{0,16}\s*[:：]")
# 列表项（"- xxx" / "• xxx"）也应各自成行
_BULLET_LINE_RE = re.compile(r"^\s*[-–—•*‣·]\s")
# 中日文字符/标点（判断换行处该不该补空格）
_CJK_CH = re.compile(r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
                     r"\uac00-\ud7af\uff00-\uffef]")


def _join_visual_lines(text: str) -> str:
    """把 OCR 按屏幕折行产生的"视觉换行"合并成连贯段落。

    为什么必须做：OCR 的每个换行都来自屏幕上的自动折行，不是语义分段。
    argostranslate 内部**按 "\\n" 切分逐行翻译**，所以保留换行等于强行
    把一句话切成几段分别翻译。实测同一句字幕：

        带换行："It means ... with\\npatrons who\\nhave ya ass reserved...
                 for their enjoyment."
        -> "这意味着你要把身体 使用和花一些高质量的时间与
            \\n赞助商\\n让你的屁股保留... 享受。"     ← 支离破碎

        合并成一段 -> "意思是你要把尸体用起来 花点时间和赞助人在一起
                       给大家留点时间 让他们享受"      ← 连贯

    保留换行（真分段）的两种情况：
      - 上一行以句末标点结尾（.!?…。！？；;）
      - 本行是标签行（"Reward: ..."）或列表项（"- xxx"）

    英文之间补空格；中日文之间**不补**（中文本来不用空格，
    补了会在句子里留下突兀的空隙）。
    """
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    if len(lines) <= 1:
        return lines[0] if lines else ""
    out = lines[0]
    for ln in lines[1:]:
        if (out[-1] in _SENT_END_CHARS
                or _LABEL_LINE_RE.match(ln) or _BULLET_LINE_RE.match(ln)):
            out += "\n" + ln
            continue
        sep = ""
        if not (_CJK_CH.match(out[-1]) and _CJK_CH.match(ln[0])):
            sep = " "
        out += sep + ln
    return out


def ocr_image(img: Image.Image, lang: str = "zh-Hans-CN",
              engine: str = None) -> str:
    """识别图片中的全部文字，返回一段连贯文本。

    最后一步会把屏幕上的"视觉换行"合并掉（见 _join_visual_lines）：
    OCR 的每一行来自屏幕自动折行，同一句话常被切成 2~4 行，若原样保留，
    翻译引擎会按行独立翻译，译文支离破碎。

    engine: None/rapidocr/windows/auto，见 OCR_ENGINES。
    返回空串表示未识别到内容（不是错误）。
    """
    text = _ocr_image_raw(img, lang, engine)
    if not text:
        return text
    # 顺序很重要：**先合并视觉换行、再跑拉丁文修复**。
    # 修复链里的"句中误大写还原"要靠完整句子的上下文才敢下判断
    # （"with patrons Who" 里的 Who 是看见前面的 with 才判成小写），
    # 按行修时上下文被换行切断，就只能停在 "Who"。
    return _repair_latin_words(_join_visual_lines(text))


def _ocr_image_raw(img: Image.Image, lang: str = "zh-Hans-CN",
                   engine: str = None) -> str:
    """识别图片中的全部文字，返回保留分段/空格结构的字符串（引擎路由）。

    engine: None/rapidocr/windows/auto，见 OCR_ENGINES。
    返回空串表示未识别到内容（不是错误）。

    小图先放大再识别；原尺寸识别为空时自动放大重试一次（Windows 引擎）。
    """
    engine = (engine or "rapidocr").lower()
    if engine not in OCR_ENGINES:
        engine = "rapidocr"

    if engine in ("rapidocr", "auto"):
        rapid = _rapid_text(img, lang) if img is not None else None
        if engine == "rapidocr":
            # 拿到任何非空结果就直接用；空结果/失败都回退 Windows
            if rapid and rapid.strip():
                return rapid
            return _ocr_image_windows(img, lang)
        # auto: 两引擎都跑，择优
        win = _ocr_image_windows(img, lang)
        if not rapid or not rapid.strip():
            return win
        if not win or not win.strip():
            return rapid
        if _score_text(rapid) >= _score_text(win):
            log.debug("auto: 采用 RapidOCR（%d vs %d）",
                      _score_text(rapid), _score_text(win))
            return rapid
        log.debug("auto: 采用 Windows OCR（%d vs %d）",
                  _score_text(win), _score_text(rapid))
        return win

    return _ocr_image_windows(img, lang)


def _ocr_image_windows(img: Image.Image, lang: str = "zh-Hans-CN") -> str:
    """Windows.Media.Ocr 完整流程（多尺度 + 英文引擎候选择优）。"""
    if img is None or img.size[0] == 0 or img.size[1] == 0:
        return ""
    try:
        if img.mode != "RGB":
            img = img.convert("RGB")

        text = _recognize_text(img, lang)
        if not text:
            # 空结果: 放大重试一次（小字号/低分辨率场景）
            scale = max(2, _pick_scale(img))
            text = _recognize_text(_upscale(img, scale), lang)
            if text:
                log.debug("放大 %dx 后识别成功", scale)
        else:
            # 有结果: 若图很小仍用放大版重跑，按质量分择优
            # （放大版更准，但也可能产出乱码，不能只比长度）
            scale = _pick_scale(img)
            if scale > 1:
                try:
                    better = _recognize_text(_upscale(img, scale), lang)
                    if _text_quality(better) > _text_quality(text) + 2:
                        text = better
                        log.debug("放大 %dx 后结果质量更优，已采用", scale)
                except Exception:
                    pass

        # 英文兜底择优: 中文/日文引擎读英文误读率高（．/"/go℃/Of/tnp），
        # 若结果以拉丁文为主，用英文引擎再识别一遍，按质量分择优。
        # 中文内容不受影响（英文引擎对中文图返回空或乱码）。
        # 注意: 即使图很大（整块区域）也强制跑一个 2x 放大候选 —— 游戏
        # UI 常是"大区域小字号"，按整图尺寸判断放大倍数会漏掉它
        # （实测 12px 文字 trip -> tnp 只在 2x 下才读对）。
        if lang != "en-US":
            has_latin = sum(1 for c in text if c.isascii() and c.isalpha())
            has_cjk = sum(1 for c in text if 0x2E80 <= ord(c) <= 0x9FFF)
            if has_latin >= 8 and has_latin > has_cjk:
                try:
                    alt = _recognize_text(img, "en-US")
                    for s in (2, 3):
                        cand = _recognize_text(_upscale(img, s), "en-US")
                        if _text_quality(cand) > _text_quality(alt):
                            alt = cand
                    if alt and _text_quality(alt) > _text_quality(text):
                        log.debug("英文引擎识别质量更优，已采用")
                        text = alt
                except Exception:
                    pass

        return _normalize_latin(text)
    except Exception as e:
        log.exception("OCR 调用失败: %s", e)
        return ""


def ocr_lines(img: Image.Image, lang: str = "zh-Hans-CN") -> List[Tuple[str, Tuple[int, int, int, int]]]:
    """识别图片中的每一行文字及其矩形区域 (x, y, w, h)。

    适合需要按行处理的场景（如只翻译某一行）。
    """
    if img is None or img.size[0] == 0 or img.size[1] == 0:
        return []
    try:
        if img.mode != "RGB":
            img = img.convert("RGB")
        result = winocr.recognize_pil_sync(img, lang=lang)
        lines = []
        for line in result.get("lines", []) or []:
            t = (line.get("text") or "").strip()
            if not t:
                continue
            words = line.get("words") or []
            if not words:
                lines.append((t, (0, 0, 0, 0)))
                continue
            # 合并所有 word 的 bounding_rect
            xs, ys, xe, ye = [], [], [], []
            for w in words:
                r = w.get("bounding_rect", {})
                xs.append(int(r.get("x", 0)))
                ys.append(int(r.get("y", 0)))
                xe.append(int(r.get("x", 0) + r.get("width", 0)))
                ye.append(int(r.get("y", 0) + r.get("height", 0)))
            x, y = min(xs), min(ys)
            w_, h_ = max(xe) - x, max(ye) - y
            lines.append((t, (x, y, w_, h_)))
        return lines
    except Exception as e:
        log.exception("OCR lines 调用失败: %s", e)
        return []
