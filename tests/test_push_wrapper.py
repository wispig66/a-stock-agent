from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PUSH = ROOT / ".agents" / "skills" / "stock-premarket" / "scripts" / "push.py"


def load_push_module():
    spec = importlib.util.spec_from_file_location("stock_push_wrapper", PUSH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scheduled_card_sources_default_to_enforce(monkeypatch):
    push = load_push_module()
    monkeypatch.delenv("CARD_VALIDATOR_MODE", raising=False)

    assert push.validator_mode("stock-intraday") == "enforce"
    assert push.validator_mode("stock-postmarket") == "enforce"
    assert push.validator_mode("stock-weekly") == "enforce"
    assert push.validator_mode("manual") == "warn"


def test_card_validator_mode_env_overrides_default(monkeypatch):
    push = load_push_module()
    monkeypatch.setenv("CARD_VALIDATOR_MODE", "warn")

    assert push.validator_mode("stock-intraday") == "warn"


def test_strip_machine_blocks_removes_decision_json():
    push = load_push_module()
    text = """hello

```decision_tickets
{"trade_date":"2026-05-19","tickets":[]}
```

world
"""

    assert push.strip_machine_blocks(text).strip() == "hello\n\nworld"


def test_enforce_rejects_invalid_card_without_sending(monkeypatch):
    push = load_push_module()
    monkeypatch.delenv("CARD_VALIDATOR_MODE", raising=False)

    class FakeViolation:
        kind = "unknown_code"
        target = "000000"
        expected = ""

        def to_dict(self):
            return {"kind": self.kind, "target": self.target}

    monkeypatch.setattr(push, "_validate", lambda text, source, mode: (False, [FakeViolation()]))
    monkeypatch.setattr(push, "push", lambda *args, **kwargs: pytest.fail("Telegram push should be blocked"))
    log_dir = push.ROOT / "data" / "card_violations"
    before = set(log_dir.glob("*_stock-intraday.json")) if log_dir.exists() else set()

    with pytest.raises(SystemExit) as exc:
        push.main(["--text", "bad card", "--source", "stock-intraday"])

    assert exc.value.code == 2
    after = set(log_dir.glob("*_stock-intraday.json"))
    new_logs = after - before
    assert len(new_logs) == 1
    log = new_logs.pop()
    assert '"mode": "enforce"' in log.read_text(encoding="utf-8")
    log.unlink()
