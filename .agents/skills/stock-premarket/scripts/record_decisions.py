#!/usr/bin/env python3
"""Persist decision_tickets embedded in a premarket card."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]

from stock_codex.domain.decision import parse_decision_block, replace_tickets  # noqa: E402

DB = ROOT / "data" / "daily.db"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="Markdown card containing a decision_tickets block")
    args = parser.parse_args()

    text = Path(args.file).read_text(encoding="utf-8")
    trade_date, tickets = parse_decision_block(text)
    written = replace_tickets(DB, trade_date, tickets)
    print(f"[decision] wrote {written} tickets for {trade_date}")


if __name__ == "__main__":
    main()
