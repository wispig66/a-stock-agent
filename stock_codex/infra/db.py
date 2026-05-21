"""
SQLite 连接工厂。统一开 WAL 配套 PRAGMA，避免 daemon 与 skill 并发写时撞锁。

journal_mode=WAL 是持久化 PRAGMA（写在 DB 文件头，init_db.sql 设过一次永久生效）；
synchronous 和 busy_timeout 是按连接独立的，必须每次 connect 都设——这就是本模块存在的原因。

用法（兼容历史）：
    from stock_codex.infra.db import connect
    with connect(DB) as conn:           # sqlite3 内置 ctx：自动 commit/rollback（不关闭）
        conn.execute(...)
    conn.close()                         # 一次性脚本需要显式关闭

长时间运行的 daemon 应该用 connect_close()，自动关闭防 fd 累积：
    from stock_codex.infra.db import connect_close
    with connect_close(DB) as conn:
        conn.execute(...)
"""

from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def connect(path: str | Path, *, timeout_ms: int = 5000) -> sqlite3.Connection:
    """打开 daily.db，自动设 synchronous=NORMAL + busy_timeout。

    注意：返回原生 Connection，`with` 退出**不会关闭**连接（仅 commit/rollback）。
    长生命周期 daemon 用 connect_close() 替代。

    timeout_ms：写冲突时等多久才报 'database is locked'，默认 5 秒。
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute(f"PRAGMA busy_timeout = {timeout_ms}")
    return conn


@contextmanager
def connect_close(path: str | Path, *, timeout_ms: int = 5000) -> Iterator[sqlite3.Connection]:
    """同 connect()，但退出时**强制 close**（防 fd 累积）。

    daemon 用这个；一次性脚本用 connect() 即可。
    """
    conn = connect(path, timeout_ms=timeout_ms)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
