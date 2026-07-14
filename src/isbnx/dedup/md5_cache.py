"""MD5 缓存模块：基于 SQLite 的 LRU 缓存，避免对未变更文件重复计算 MD5。"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

_DEFAULT_DB_PATH = Path(os.path.expanduser("~/.isbnx")) / "dedup_md5_cache.db"
_DEFAULT_MAX_ENTRIES = 100_000


class MD5Cache:
    """SQLite 文件级 MD5 缓存，线程安全。

    缓存键：(size, mtime_ns, path) → md5_hex
    - size 在前优化 B-tree 索引（去重流程按 size 分组，同批查询 size 相同）
    - mtime_ns 为内容最后修改时间（纳秒），内容不变则缓存有效
    - path 为绝对路径（含文件名和后缀），放最后做字符串比较

    淘汰策略：内存计数器跟踪条目数（写入时 +N），超过上限时
    做一次真实 COUNT 并批量淘汰到 95%，留出缓冲减少触发频率。
    """

    _EVICT_WATERMARK = 0.95  # 淘汰后保留比例

    def __init__(
        self,
        db_path: str | Path | None = None,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._db_path = str(db_path or _DEFAULT_DB_PATH)
        self._max_entries = max_entries
        self._lock = threading.Lock()

        # 统计计数器
        self.hits: int = 0
        self.misses: int = 0
        self.stores: int = 0

        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

        # 完整性检查：损坏则重建
        try:
            result = self._conn.execute("PRAGMA quick_check").fetchone()
            if result and result[0] != "ok":
                self._conn.close()
                os.remove(self._db_path)
                self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            self._conn.close()
            os.remove(self._db_path)
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")

        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS md5_cache (
                rowid      INTEGER PRIMARY KEY,
                size       INTEGER NOT NULL,
                mtime_ns   INTEGER NOT NULL,
                path       TEXT    NOT NULL,
                md5_hex    TEXT    NOT NULL,
                last_used  REAL    NOT NULL,
                UNIQUE (size, mtime_ns, path)
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_md5_cache_last_used
            ON md5_cache (last_used)
        """)
        self._conn.commit()

        # 内存计数器：启动时查一次真实值，后续写入时 +N
        count_row = self._conn.execute("SELECT COUNT(*) FROM md5_cache").fetchone()
        self._count: int = count_row[0] if count_row else 0

    def get(
        self,
        size: int,
        mtime_ns: int,
        path: str,
    ) -> str | None:
        """查询缓存，命中则更新 last_used 并返回 md5 hex，否则返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT md5_hex FROM md5_cache WHERE size=? AND mtime_ns=? AND path=?",
                (size, mtime_ns, path),
            ).fetchone()

            if row:
                self.hits += 1
                self._conn.execute(
                    "UPDATE md5_cache SET last_used=? WHERE size=? AND mtime_ns=? AND path=?",
                    (time.time(), size, mtime_ns, path),
                )
                self._conn.commit()
                return row[0]

            self.misses += 1
            return None

    def set(
        self,
        size: int,
        mtime_ns: int,
        path: str,
        md5_hex: str,
    ) -> None:
        """写入或更新缓存条目，必要时触发 LRU 淘汰。"""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO md5_cache (size, mtime_ns, path, md5_hex, last_used) VALUES (?, ?, ?, ?, ?)",
                (size, mtime_ns, path, md5_hex, now),
            )
            self._conn.commit()
            self.stores += 1
            self._count += 1
            self._evict_if_needed()

    def get_batch(
        self,
        items: list[tuple[int, int, str]],
    ) -> dict[tuple[int, int, str], str]:
        """批量查询缓存。

        Args:
            items: [(size, mtime_ns, path), ...]

        Returns:
            {键元组: md5_hex} — 仅包含命中的条目。
        """
        if not items:
            return {}

        result: dict[tuple[int, int, str], str] = {}
        now = time.time()

        with self._lock:
            for key in items:
                size, mtime_ns, path = key
                row = self._conn.execute(
                    "SELECT md5_hex FROM md5_cache WHERE size=? AND mtime_ns=? AND path=?",
                    (size, mtime_ns, path),
                ).fetchone()
                if row:
                    result[key] = row[0]
                    self.hits += 1
                    self._conn.execute(
                        "UPDATE md5_cache SET last_used=? WHERE size=? AND mtime_ns=? AND path=?",
                        (now, size, mtime_ns, path),
                    )
                else:
                    self.misses += 1
            self._conn.commit()

        return result

    def set_batch(
        self,
        items: list[tuple[int, int, str, str]],
    ) -> None:
        """批量写入缓存条目，必要时触发 LRU 淘汰。

        Args:
            items: [(size, mtime_ns, path, md5_hex), ...]
        """
        if not items:
            return

        now = time.time()
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO md5_cache (size, mtime_ns, path, md5_hex, last_used) VALUES (?, ?, ?, ?, ?)",
                [(item[0], item[1], item[2], item[3], now) for item in items],
            )
            self._conn.commit()
            n = len(items)
            self.stores += n
            self._count += n
            self._evict_if_needed()

    def stats(self) -> dict[str, int]:
        """返回缓存统计信息。"""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
        }

    def close(self) -> None:
        """关闭数据库连接。"""
        with self._lock:
            self._conn.close()

    # ── 内部方法 ──

    def _evict_if_needed(self) -> None:
        """内存计数超上限时，用真实 COUNT 纠正并批量淘汰到 95%（已在锁内调用）。"""
        if self._count <= self._max_entries:
            return

        # 查真实条目数
        real_count = self._conn.execute("SELECT COUNT(*) FROM md5_cache").fetchone()[0]
        if real_count <= self._max_entries:
            self._count = real_count
            return

        # 批量淘汰到 95%，留出缓冲
        target = int(self._max_entries * self._EVICT_WATERMARK)
        to_delete = real_count - target
        if to_delete <= 0:
            self._count = real_count
            return

        self._conn.execute(
            "DELETE FROM md5_cache WHERE rowid IN (  SELECT rowid FROM md5_cache ORDER BY last_used ASC LIMIT ?)",
            (to_delete,),
        )
        self._conn.commit()
        self._count = target
