from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
ENV_FILE = PROJECT_ROOT / ".env"
HOLDINGS_FILE = PROJECT_ROOT / "holdings.yaml"
RISK_CONFIG_FILE = PROJECT_ROOT / "risk_config.yaml"
RISK_STATE_FILE = PROJECT_ROOT / "risk_state.yaml"
TRADE_CALENDAR_FILE = DATA_DIR / "trade_calendar.csv"
DB_FILE = DATA_DIR / "daily.db"
