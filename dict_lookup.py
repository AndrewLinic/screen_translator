"""ECDICT 离线英汉词典查询

数据来源: https://github.com/skywind3000/ECDICT
文件: assets/ecdict.csv (65MB, 340万词条, 77万独立词)

字段: word, phonetic, definition, translation, pos,
      collins, oxford, tag, bnc, frq, exchange, detail, audio
"""
from __future__ import annotations

import csv
import logging
import re
import sys
import threading
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)


def _resolve_assets_dir() -> Path:
    """优先: PyInstaller _MEIPASS/assets  →  exe 同目录/assets  →  工程目录/assets"""
    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "assets")
        candidates.append(Path(sys.executable).parent / "assets")
    candidates.append(Path(__file__).parent / "assets")
    for c in candidates:
        if c.exists() and (c / "ecdict.csv").exists():
            return c
    return candidates[-1]  # fallback


_DEFAULT_ASSETS = _resolve_assets_dir()
DEFAULT_CSV = _DEFAULT_ASSETS / "ecdict.csv"

# 词形还原简单规则（够应付常见情况）
_LEMMAS = {
    # -ing / -ed / -s
    "running": "run", "running.": "run",
    "stopped": "stop", "stopping": "stop",
    "better": "good", "worse": "bad",
    "best": "good", "worst": "bad",
    "children": "child", "men": "man", "women": "woman",
    "feet": "foot", "teeth": "tooth", "mice": "mouse",
}


def _lemma(word: str) -> str:
    """极简词形还原：去掉 -s/-ed/-ing/-ly 等后缀找原形。"""
    w = word.lower()
    if w in _LEMMAS:
        return _LEMMAS[w]
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("ied"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("ing"):
        base = w[:-3]
        if len(base) > 2 and base[-1] == base[-2]:
            base = base[:-1]  # running -> run
        return base
    if len(w) > 3 and w.endswith("ed"):
        return w[:-2]
    if len(w) > 3 and w.endswith("es"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    if len(w) > 4 and w.endswith("ly"):
        return w[:-2]
    return w


class Dict:
    """线程安全的 ECDICT 查询器。"""

    _instance: Optional["Dict"] = None
    _lock = threading.Lock()

    def __init__(self, csv_path: Optional[Path] = None):
        self.csv_path = Path(csv_path) if csv_path else DEFAULT_CSV
        self._idx: dict[str, List[dict]] = {}
        self._loaded = False
        self._load_lock = threading.Lock()

    @classmethod
    def get(cls) -> "Dict":
        """单例模式。"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def is_available(self) -> bool:
        return self.csv_path.exists()

    def ensure_loaded(self):
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            self._load()

    def _load(self):
        if not self.csv_path.exists():
            log.warning("词典文件不存在: %s", self.csv_path)
            return
        log.info("正在加载词典 %s ...", self.csv_path)
        t0 = time_now()
        n = 0
        with open(self.csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                w = (row.get("word") or "").strip().lower()
                if not w:
                    continue
                if w not in self._idx:
                    self._idx[w] = []
                self._idx[w].append(row)
                n += 1
        self._loaded = True
        log.info("词典加载完成: %d 行, %d 唯一词, 耗时 %.1fs",
                 n, len(self._idx), time_now() - t0)

    def lookup(self, word: str) -> Optional[dict]:
        """查一个词。返回结构化的查词结果字典，未找到则 None。

        返回 dict:
            word        原始输入
            base_word   还原后的原型（用于回退）
            phonetic    音标
            translation 中文翻译（按行）
            pos         词性列表
            collins     Collins 词频（0-5，0=未收录）
            tag         考试标签（CET4/GRE/TOEFL 等）
            exchange    词形变化 JSON 字符串
            found       bool - 是否在主表中找到（否则是词形还原命中）
        """
        if not self.is_available():
            return None
        self.ensure_loaded()
        if not self._loaded:
            return None

        w = word.strip().lower()
        if not w:
            return None

        rows = self._idx.get(w)
        base = _lemma(w) if not rows else None
        if not rows and base != w:
            rows = self._idx.get(base)

        if not rows:
            return None

        r = rows[0]
        # 拼一个简单的合并翻译
        translation = (r.get("translation") or "").strip()
        # ecdict 多义项分隔符是字面 \n（反斜杠+n 两字符），先归一为真换行
        # 否则 split_translations 拆不开，词性标注会错（见 mere 这类）
        translation_norm = translation.replace("\\n", "\n")
        phonetic = (r.get("phonetic") or "").strip()
        definition = (r.get("definition") or "").strip()
        pos = (r.get("pos") or "").strip()
        collins = 0
        try:
            collins = int(r.get("collins") or 0)
        except Exception:
            pass
        tag = (r.get("tag") or "").strip()
        exchange = (r.get("exchange") or "").strip()
        audio = (r.get("audio") or "").strip()

        # 词性补全: ecdict 的 pos 字段经常缺失（mere 的 pos 是空字符串），
        # 但 translation 里有 n./a./v. 等词性标签。从归一化后的 translation
        # 拆解时按 split_translations 提取；保留去重顺序（首次出现优先）。
        # 这样弹窗的"词性:"行能正确显示，meta_label 不再空白。
        pos_list = [p.strip() for p in pos.split("/") if p.strip()] if pos else []
        if not pos_list:
            try:
                seen = set()
                for item in split_translations(translation_norm):
                    p = item.get("pos", "")
                    if p and p not in seen:
                        seen.add(p)
                        pos_list.append(p)
            except Exception:
                pass

        return {
            "word": word,
            "base_word": base or w,
            "phonetic": phonetic,
            "translation": translation,
            "definition": definition,
            "pos": order_pos_list(w, pos_list),
            "collins": collins,
            "tag": [t.strip() for t in tag.split("/") if t.strip()] if tag else [],
            "exchange": exchange,
            "audio": audio,
            "found": w in self._idx,
            "all_translations": [r2.get("translation", "") for r2 in rows],
        }


def time_now() -> float:
    import time
    return time.time()


def split_translations(translation: str) -> List[dict]:
    """把翻译字符串拆成 [{pos, mean}, ...] 形式。

    输入示例:
        'n. 世界, 地球, 宇宙\\n[法] 世界, 地球, 世人'
        'vi. 奸狡地行动'
        'interj. 喂, 嘿'
    词典多义项分隔符:
        ecdict.csv 实际存储的是字面"反斜杠+n"两字符 ('\\\\n')，不是真换行
        符 ('\\n', chr 10)。Python str.splitlines / split('\\n') 都不会
        拆这种字面字符。函数先把字面 '\\\\n' 归一为真换行符，再按行处理，
        否则 'n. 小湖, 池塘\\na. 仅仅的, 只不过的' 这种会整段合并、词性错乱。
    """
    if not translation:
        return []
    # 归一化: 词典里的多义项分隔是字面反斜杠+n (两字符)，先转成真换行
    norm = translation.replace("\r", "").replace("\\n", "\n")
    out: List[dict] = []
    for line in norm.split("\n"):
        line = line.strip()
        if not line:
            continue
        # 提取词性
        m = re.match(r"^(\w+\.)\s*(.*)$", line)
        if m:
            pos = m.group(1)
            means = [x.strip() for x in re.split(r"[;,，；、]", m.group(2)) if x.strip()]
            out.append({"pos": pos, "means": means, "raw": line})
        else:
            means = [x.strip() for x in re.split(r"[;,，；、]", line) if x.strip()]
            out.append({"pos": "", "means": means, "raw": line})
    return out


# ===== 词性显示顺序：常用义项优先 =====
# ecdict 的 pos / translation 里词性顺序几乎总是"名词在前"，与该词最常用的
# 义项不符：run（主要是动词）、get（动词）、mere（形容词）都被排成 n. 在前，
# 看着"不对"。离线词典没有"逐词性词频"，故这里按语言常识维护一张常见多义词
# 的"主词性顺序"表；表里没有的词保持词典原序（不动）。
# 组代码: n 名词 / v 动词(vt./vi./v./aux.) / a 形容词 / adv 副词 /
#         prep 介词 / conj 连词 / pron 代词 / num 数词 / int 感叹 /
#         art 冠词 / other 其他（含 [计]/[医] 等标注行）
_POS_GROUP = {
    "n": "n",
    "vt": "v", "vi": "v", "v": "v", "aux": "v",
    "a": "a", "adj": "a",
    "ad": "adv", "adv": "adv",
    "prep": "prep", "conj": "conj", "pron": "pron",
    "num": "num", "int": "int", "interj": "int", "art": "art",
    "abbr": "other",
}


def _pos_group(tag: str) -> str:
    """把 ecdict 词性标签归一为组代码（'vt.'->'v', 'a.'->'a', ...）。"""
    return _POS_GROUP.get((tag or "").strip().lower().rstrip("."), "other")


# 动词义项常用（动词应排在名词前）的多义词
_VERB_FIRST_WORDS = """
get take make go come see know think want need use find give tell call try ask
say look hear feel keep let put help pay meet lose hold bring leave begin
become seem happen run sit stand turn start move show
include continue change lead understand follow stop create speak read allow add
spend grow walk win offer remember consider appear buy wait serve send expect
build stay fall cut reach kill remain accept raise sell require report decide
pull return explain hope develop carry break receive agree support hit produce
eat cover catch draw choose listen realize drive push fly join sing wear beat
burn teach throw ride drink fight attack defend cry laugh study climb dig feed
borrow lend hide jump kick kiss knock lift lock mix pack paint plant print
protect repair repeat ring roll save search shout smell smile smoke spell swim
switch taste touch train travel treat trust wash wonder worry
""".split()

# 形容词义项常用（形容词应排在名词前）的多义词
_ADJ_FIRST_WORDS = """
mere free right present fair firm clear dead blind calm cool dry empty fit flat
grave idle lean loose mild native noble pale plain ready rough sharp slight
smooth sober sole spare steady stiff straight tame tender thin tough vain vast
warm weak wet wild tired pretty poor odd keen lame grand
""".split()

_POS_ORDER_OVERRIDES = {}
for _w in _VERB_FIRST_WORDS:
    _POS_ORDER_OVERRIDES[_w] = ["v", "n", "a", "adv"]
for _w in _ADJ_FIRST_WORDS:
    _POS_ORDER_OVERRIDES[_w] = ["a", "n", "v", "adv"]
# 个别词顺序特殊处理（覆盖上面两条通用规则）
_POS_ORDER_OVERRIDES.update({
    "mean": ["v", "a", "n"],
    "light": ["n", "a", "v", "adv"],     # light 以名词"光"为主
    "open": ["v", "a", "n"],
    "close": ["v", "a", "n", "adv"],
    "settle": ["v", "n"],
    "charge": ["v", "n"],                # charge 动词/名词皆常见，动词为主
})


def pos_order_index(word: str, tag: str) -> int:
    """词性在【该词期望顺序】里的位次；词不在覆盖表里统一返回 0。

    返回 0 时配合稳定排序 => 保持词典原序（即不改变原本没问题的词）。
    """
    order = _POS_ORDER_OVERRIDES.get((word or "").strip().lower())
    if not order:
        return 0
    g = _pos_group(tag)
    try:
        return order.index(g)
    except ValueError:
        return len(order)


def order_pos_list(word: str, pos_list: list) -> list:
    """按"常用义项优先"重排词性列表（稳定排序；未列出的组落到最后）。"""
    if not pos_list:
        return pos_list
    order = _POS_ORDER_OVERRIDES.get((word or "").strip().lower())
    if not order:
        return pos_list
    return sorted(pos_list, key=lambda p: pos_order_index(word, p))
