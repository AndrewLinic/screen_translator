"""生词本 - SQLite 持久化
- 查过的词右键「加入生词本」即可收藏
- 自动记录首次/最近查询时间、查询次数
- 提供列表/搜索/删除/导出 Anki CSV
- 数据位置: 用户目录 ~/.screen_translator/vocab.db
"""
from __future__ import annotations

import csv
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)

DEFAULT_DB = Path.home() / ".screen_translator" / "vocab.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS words (
    word           TEXT PRIMARY KEY,
    phonetic       TEXT,
    translation    TEXT,
    pos            TEXT,
    definition     TEXT,
    first_added    REAL NOT NULL,           -- 首次加入时间戳
    last_queried   REAL NOT NULL,           -- 最近一次查询时间
    query_count    INTEGER DEFAULT 1,       -- 查询次数
    note           TEXT DEFAULT ''          -- 用户备注
);
CREATE INDEX IF NOT EXISTS idx_last_queried ON words(last_queried DESC);
CREATE INDEX IF NOT EXISTS idx_query_count  ON words(query_count DESC);
"""


class Vocab:
    """线程安全的生词本。"""

    _instance: Optional["Vocab"] = None
    _lock = threading.Lock()

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._ensure_schema()

    @classmethod
    def get(cls) -> "Vocab":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(str(self.db_path), timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    def _ensure_schema(self):
        with self._conn() as c:
            c.executescript(_SCHEMA)
            c.commit()

    def add_or_update(
        self,
        word: str,
        phonetic: str = "",
        translation: str = "",
        pos: str = "",
        definition: str = "",
    ) -> bool:
        """加入或更新一个词。返回是否成功。"""
        w = word.strip().lower()
        if not w:
            return False
        import time
        now = time.time()
        with self._write_lock, self._conn() as c:
            row = c.execute("SELECT word, query_count FROM words WHERE word=?",
                            (w,)).fetchone()
            if row:
                c.execute(
                    """UPDATE words SET
                        phonetic=?, translation=?, pos=?, definition=?,
                        last_queried=?, query_count=query_count+1
                       WHERE word=?""",
                    (phonetic, translation, pos, definition, now, w),
                )
            else:
                c.execute(
                    """INSERT INTO words
                        (word, phonetic, translation, pos, definition,
                         first_added, last_queried, query_count)
                        VALUES (?,?,?,?,?,?,?,?)""",
                    (w, phonetic, translation, pos, definition,
                     now, now, 1),
                )
            c.commit()
        return True

    def remove(self, word: str) -> bool:
        w = word.strip().lower()
        with self._write_lock, self._conn() as c:
            cur = c.execute("DELETE FROM words WHERE word=?", (w,))
            c.commit()
            return cur.rowcount > 0

    def has(self, word: str) -> bool:
        w = word.strip().lower()
        with self._conn() as c:
            r = c.execute("SELECT 1 FROM words WHERE word=?", (w,)).fetchone()
            return r is not None

    def search(self, keyword: str = "", limit: int = 200) -> List[dict]:
        kw = (keyword or "").strip().lower()
        with self._conn() as c:
            if kw:
                rows = c.execute(
                    """SELECT * FROM words
                       WHERE word LIKE ? OR translation LIKE ?
                       ORDER BY last_queried DESC LIMIT ?""",
                    (f"%{kw}%", f"%{kw}%", limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM words ORDER BY last_queried DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        with self._conn() as c:
            r = c.execute("SELECT COUNT(*) AS n FROM words").fetchone()
            return int(r["n"]) if r else 0

    def export_anki_csv(self, out_path: Optional[Path] = None) -> Path:
        """导出为 Anki 可导入的 CSV (utf-8). 默认导出到 ~/.screen_translator/vocab_export.csv"""
        if out_path is None:
            out_path = Path.home() / ".screen_translator" / "vocab_export.csv"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = self.search(keyword="", limit=100000)
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            # Anki: 正面 / 背面 / 标签
            for r in rows:
                tag = "screen_translator"
                phonetic = r.get("phonetic") or ""
                pos = r.get("pos") or ""
                definition = r.get("definition") or ""
                translation = r.get("translation") or ""
                front = r["word"]
                # 背面带音标/词性
                back_bits = []
                if phonetic:
                    back_bits.append(f"[{phonetic}]")
                if pos:
                    back_bits.append(f"({pos})")
                back_bits.append(translation)
                if definition:
                    back_bits.append(f"\n英文释义: {definition}")
                back = " ".join(back_bits)
                w.writerow([front, back, tag])
        log.info("已导出 %d 个生词到 %s", len(rows), out_path)
        return out_path

    def open_export_folder(self):
        """在资源管理器中打开导出目录。"""
        import os
        import subprocess
        folder = self.db_path.parent
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(str(folder))  # noqa
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as e:
            log.warning("打开目录失败: %s", e)
