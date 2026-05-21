#!/usr/bin/env python
"""一次性 migration：给已有 data/daily.db 加 tg_inbound 表 + 3 索引。
幂等：CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS。
"""
from __future__ import annotations

from stock_codex.infra.db import connect  # noqa: E402
from stock_codex.paths import DB_FILE

DDL = """
CREATE TABLE IF NOT EXISTS tg_inbound (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    update_id INTEGER UNIQUE,
    chat_id TEXT NOT NULL,
    user_msg_id INTEGER NOT NULL,
    raw_text TEXT NOT NULL,
    parsed_command TEXT,
    parsed_intent TEXT,
    parsed_payload TEXT,
    response_msg_id INTEGER,
    handler_status TEXT,
    handler_error TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tg_inbound_ts ON tg_inbound(timestamp);
CREATE INDEX IF NOT EXISTS idx_tg_inbound_chat ON tg_inbound(chat_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_tg_inbound_command ON tg_inbound(parsed_command);
"""


def main() -> None:
    db_path = DB_FILE
    if not db_path.exists():
        print(f"⚠️  {db_path} 不存在，跳过（首次 setup 会通过 init_db.sql 建表）")
        return
    with connect(db_path) as conn:
        conn.executescript(DDL)
    print(f"✓ tg_inbound 表 + 3 索引已就绪：{db_path}")


if __name__ == "__main__":
    main()
