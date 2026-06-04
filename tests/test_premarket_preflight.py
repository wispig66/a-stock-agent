from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

from stock_codex.domain import holdings


ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / ".agents" / "skills" / "stock-premarket" / "scripts" / "preflight.py"


def load_module():
    spec = importlib.util.spec_from_file_location("premarket_preflight", PREFLIGHT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preflight_outputs_holding_details(monkeypatch, capsys) -> None:
    pf = load_module()
    rec = holdings.Holding(
        code="002608",
        name="江苏国信",
        genre="A",
        cost=10.253,
        shares=2000,
        buy_date=date(2026, 6, 2),
        stop_loss=9.99,
        take_profit=10.56,
        note="电力 · 换手二板",
    )
    monkeypatch.setattr(pf, "read_holdings", lambda: [rec])
    monkeypatch.setattr(
        pf.risk,
        "load_risk_config",
        lambda: {"total_capital": 500000, "max_total_exposure_pct": 70, "max_single_position_pct": 30},
    )
    monkeypatch.setattr(pf.risk, "fetch_spot_price_fn", lambda: lambda code: 10.1)

    assert pf.main() == 0
    out = json.loads(capsys.readouterr().out)

    assert out["position_count"] == 1
    assert out["holdings"] == [{
        "code": "002608",
        "name": "江苏国信",
        "genre": "A",
        "cost": 10.253,
        "shares": 2000,
        "buy_date": "2026-06-02",
        "stop_loss": 9.99,
        "take_profit": 10.56,
        "note": "电力 · 换手二板",
    }]


def test_preflight_failure_keeps_known_holdings(monkeypatch, capsys) -> None:
    pf = load_module()
    rec = holdings.Holding(
        code="002608",
        name="江苏国信",
        genre="A",
        cost=10.253,
        shares=2000,
        buy_date=date(2026, 6, 2),
        stop_loss=9.99,
    )
    monkeypatch.setattr(pf, "read_holdings", lambda: [rec])
    monkeypatch.setattr(
        pf.risk,
        "load_risk_config",
        lambda: (_ for _ in ()).throw(RuntimeError("bad config")),
    )

    assert pf.main() == 0
    out = json.loads(capsys.readouterr().out)

    assert out["position_count"] == 1
    assert out["holdings"][0]["code"] == "002608"
