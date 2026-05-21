from __future__ import annotations

import sys
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

import notify  # noqa: E402


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def json(self) -> dict:
        return self.payload


def test_safe_error_text_redacts_telegram_token(monkeypatch):
    monkeypatch.setattr(notify, "TOKEN", "secret-token")

    text = notify._safe_error_text(
        "https://api.telegram.org/botsecret-token/sendMessage failed"
    )

    assert "secret-token" not in text
    assert "<redacted-token>" in text


def test_send_retries_transient_request_errors(monkeypatch):
    calls = {"count": 0}
    monkeypatch.setattr(notify, "TOKEN", "secret-token")
    monkeypatch.setattr(notify, "CHAT_ID", "123")
    monkeypatch.setattr(notify, "API", "https://api.telegram.org/botsecret-token")
    monkeypatch.setattr(notify.time, "sleep", lambda _seconds: None)

    def fake_post(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] < 3:
            raise requests.ConnectionError("temporary dns failure")
        return FakeResponse({"ok": True, "result": {"message_id": 42}})

    monkeypatch.setattr(notify.requests, "post", fake_post)

    result = notify._send("hello")

    assert result["result"]["message_id"] == 42
    assert calls["count"] == 3


def test_send_failure_message_redacts_token_after_retries(monkeypatch):
    monkeypatch.setattr(notify, "TOKEN", "secret-token")
    monkeypatch.setattr(notify, "CHAT_ID", "123")
    monkeypatch.setattr(notify, "API", "https://api.telegram.org/botsecret-token")
    monkeypatch.setattr(notify.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        notify.requests,
        "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            requests.ConnectionError(
                "https://api.telegram.org/botsecret-token/sendMessage"
            )
        ),
    )

    try:
        notify._send("hello")
    except notify.NotifyError as exc:
        error = str(exc)
    else:
        raise AssertionError("expected NotifyError")

    assert "secret-token" not in error
    assert "<redacted-token>" in error
