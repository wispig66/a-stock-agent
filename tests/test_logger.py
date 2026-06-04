from __future__ import annotations

from stock_codex.infra import logger


def test_redact_secrets_removes_weixin_token_from_traceback(monkeypatch):
    monkeypatch.setenv("WEIXIN_TOKEN", "wx-secret-token")

    text = (
        "requests.exceptions.HTTPError: 403 Client Error for iLink "
        "token=wx-secret-token"
    )

    redacted = logger._redact_secrets(text)

    assert "wx-secret-token" not in redacted
