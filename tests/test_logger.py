from __future__ import annotations

from stock_codex.infra import logger


def test_redact_secrets_removes_telegram_token_from_traceback_url(monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "123:secret-token")

    text = (
        "requests.exceptions.HTTPError: 409 Client Error for url: "
        "https://api.telegram.org/bot123:secret-token/getUpdates"
    )

    redacted = logger._redact_secrets(text)

    assert "123:secret-token" not in redacted
    assert "/bot<redacted>/getUpdates" in redacted

