"""
SQLite 连接工厂。统一开 WAL 配套 PRAGMA，避免 daemon 与 skill 并发写时撞锁。

journal_mode=WAL 是持久化 PRAGMA（写在 DB 文件头，init_db.sql 设过一次永久生效）；
synchronous 和 busy_timeout 是按连接独立的，必须每次 connect 都设——这就是本模块存在的原因。

用法：
    from db import connect
    with connect(DB) as conn:
        conn.execute(...)
"""

from __future__ import annotations
import sqlite3
from pathlib import Path


def connect(path: str | Path, *, timeout_ms: int = 5000) -> sqlite3.Connection:
    """打开 daily.db，自动设 synchronous=NORMAL + busy_timeout。

    timeout_ms：写冲突时等多久才报 'database is locked'，默认 5 秒。
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute(f"PRAGMA busy_timeout = {timeout_ms}")
    return conn
