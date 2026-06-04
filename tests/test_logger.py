from __future__ import annotations

import logging

from stock_codex.infra import logger


def test_redact_secrets_removes_weixin_token_from_traceback(monkeypatch):
    monkeypatch.setenv("WEIXIN_TOKEN", "wx-secret-token")

    text = (
        "requests.exceptions.HTTPError: 403 Client Error for iLink "
        "token=wx-secret-token"
    )

    redacted = logger._redact_secrets(text)

    assert "wx-secret-token" not in redacted


def test_error_alert_handler_is_disabled_under_pytest(monkeypatch):
    calls = []

    from stock_codex.infra import notify

    monkeypatch.setattr(notify, "push", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_logger.py::test_error_alert_handler_is_disabled_under_pytest (call)")

    record = logging.LogRecord(
        name="unit",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="boom",
        args=(),
        exc_info=None,
    )
    logger._IMErrorHandler(throttle_sec=0).emit(record)

    assert calls == []
