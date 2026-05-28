"""Create channels/gateway tables in an existing daily.db.

The DDL is idempotent and intentionally mirrors stock_codex/schema/init_db.sql.
"""
from __future__ import annotations

from stock_codex.infra.db import connect
from stock_codex.paths import DB_FILE, PROJECT_ROOT


def main() -> None:
    schema = (PROJECT_ROOT / "stock_codex" / "schema" / "init_db.sql").read_text(encoding="utf-8")
    with connect(DB_FILE) as conn:
        conn.executescript(schema)
        conn.commit()
    print(f"✓ channels gateway tables are ready: {DB_FILE}")


if __name__ == "__main__":
    main()
