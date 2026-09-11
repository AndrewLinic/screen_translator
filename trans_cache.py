"""翻译结果持久缓存（sqlite，跨会话复用）。

- 线程安全: 每次操作独立连接（翻译每秒最多几次，开销可忽略）
- 键: sha1(f"{from_lang}>{to_lang}|{text}")，同一语言对下同文本命中
- 容量: 超过 _MAX_ROWS 按最旧淘汰（写入时节流清理）
- 初始化失败时自动降级为不缓存（get 返回 None，put 静默丢弃），
  不影响翻译主链路。
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_MAX_ROWS = 5000
_PRUNE_EVERY = 50  # 每 N 次写入清理一次

_db_path: Optional[Path] = None
_lock = threading.Lock()
_put_count = 0


def init(db_path: Path) -> bool:
    """初始化缓存库。返回是否可用。"""
    global _db_path
    try:
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with _conn(p) as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS cache("
                "k TEXT PRIMARY KEY, v TEXT NOT NULL, ts REAL NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_ts ON cache(ts)")
        _db_path = p
        log.info("翻译缓存已启用: %s", p)
        return True
    except Exception as e:
        log.warning("翻译缓存初始化失败（将不缓存）: %s", e)
        _db_path = None
        return False


def _conn(path: Optional[Path] = None) -> sqlite3.Connection:
    return sqlite3.connect(str(path or _db_path), timeout=3)


def cache_key(text: str, from_lang: str, to_lang: str, engine: str = "") -> str:
    """缓存键 = 语言对 + 原文 + 引擎模式。

    engine 用于区分离线/在线结果：同一句话在 Argos 和百度下的译文
    质量不同（尤其多义词消歧），切引擎后旧缓存应失效、用新引擎重翻。
    传空字符串则退化为旧行为（仅语言对+原文）。
    """
    raw = f"{from_lang}>{to_lang}|{engine}|{text}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def get(key: str) -> Optional[str]:
    if _db_path is None:
        return None
    c = None
    try:
        c = _conn()
        with c:
            row = c.execute(
                "SELECT v FROM cache WHERE k=?", (key,)).fetchone()
            return row[0] if row else None
    except Exception:
        return None
    finally:
        if c is not None:
            c.close()


def put(key: str, value: str):
    global _put_count
    if _db_path is None or not value:
        return
    c = None
    try:
        c = _conn()
        with _lock, c:
            c.execute(
                "INSERT OR REPLACE INTO cache(k, v, ts) VALUES(?,?,?)",
                (key, value, time.time()))
            _put_count += 1
            if _put_count % _PRUNE_EVERY == 0:
                c.execute(
                    "DELETE FROM cache WHERE k IN ("
                    " SELECT k FROM cache ORDER BY ts DESC LIMIT -1"
                    " OFFSET ?)", (_MAX_ROWS,))
    except Exception as e:
        log.debug("缓存写入失败: %s", e)
    finally:
        if c is not None:
            c.close()
