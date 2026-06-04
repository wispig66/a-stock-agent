from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from stock_codex.infra import push_wrapper


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

    assert push.validator_mode("stock-premarket") == "enforce"
    assert push.validator_mode("stock-intraday") == "enforce"
    assert push.validator_mode("stock-postmarket") == "enforce"
    assert push.validator_mode("stock-weekly") == "enforce"
    assert push.validator_mode("stock-market-dynamic") == "enforce"
    assert push.validator_mode("manual") == "warn"


def test_card_validator_mode_env_overrides_default(monkeypatch):
    push = load_push_module()
    monkeypatch.setenv("CARD_VALIDATOR_MODE", "warn")

    assert push.validator_mode("stock-intraday") == "warn"


def test_enforce_validator_exception_fails_closed(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "allowed_latest_stock-market-dynamic.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr(push_wrapper, "ROOT", tmp_path)

    with pytest.raises(RuntimeError, match="validator failed"):
        push_wrapper._validate("card", "stock-market-dynamic", "enforce")


def test_enforce_missing_allowed_file_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(push_wrapper, "ROOT", tmp_path)

    with pytest.raises(RuntimeError, match="missing ALLOWED"):
        push_wrapper._validate("card", "stock-market-dynamic", "enforce")


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

    monkeypatch.setattr(push_wrapper, "_validate", lambda text, source, mode: (False, [FakeViolation()]))
    monkeypatch.setattr(push_wrapper, "push", lambda *args, **kwargs: pytest.fail("IM push should be blocked"))
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


def test_push_cli_prints_feishu_raw_message_id_without_retry_error(monkeypatch, capsys):
    push = load_push_module()
    monkeypatch.setattr(
        push,
        "push_text",
        lambda text, source, notify_blocked=False: [
            {"code": 0, "data": {"message_id": "om_test"}, "msg": "success"}
        ],
    )

    push.main(["--text", "ok", "--source", "stock-intraday"])

    assert "chunk 1/1 msg_id=om_test" in capsys.readouterr().out


def test_push_text_chunks_through_gateway(monkeypatch):
    monkeypatch.setenv("CARD_VALIDATOR_MODE", "off")
    sent = []

    def fake_push(text, source):
        sent.append((source, text))
        return {"result": {"message_id": len(sent)}}

    monkeypatch.setattr(push_wrapper, "push", fake_push)
    text = ("a" * 2000) + "\n\n" + ("b" * 2000)

    results = push_wrapper.push_text(text, source="stock-anomaly")

    assert [r["result"]["message_id"] for r in results] == [1, 2]
    assert sent[0][0] == "stock-anomaly"
    assert sent[0][1].startswith("(1/2)\n")


def test_daemon_pushes_use_unified_wrapper():
    for path in [
        ROOT / ".agents/skills/stock-intraday/scripts/watch_loop.py",
        ROOT / ".agents/skills/stock-anomaly/scripts/anomaly_loop.py",
        ROOT / "stock_codex/apps/theme_emergence_loop.py",
    ]:
        text = path.read_text(encoding="utf-8")
        assert "stock_codex.infra.push_wrapper import push_one" in text
        assert "stock_codex.infra.notify import push" not in text
