# Contributing

Thanks for considering a contribution. This project is a local-first research and notification workflow for A-share market analysis. Changes should keep that scope clear: no brokerage execution, no automated trading, and no hidden network side effects.

## Development Setup

```bash
uv sync --group dev
cp .env.example .env
sqlite3 data/daily.db < stock_codex/schema/init_db.sql
uv run --no-sync pytest -q
```

Do not commit `.env`, `data/`, `logs/`, `holdings.yaml`, `risk_config.yaml`, or `risk_state.yaml`.

## Pull Request Guidelines

- Keep changes scoped. Split unrelated fixes into separate commits.
- Prefer existing project patterns over new abstractions.
- Add tests for behavior changes, especially gateway, notification, risk, and data parser logic.
- Do not commit generated runtime data, private holdings, chat ids, bot tokens, or logs.
- Keep financial language factual and non-promissory. Avoid wording that implies guaranteed returns.

## Code Style

- Python code targets Python 3.11-3.12.
- Use `uv run --no-sync pytest -q` before submitting.
- Keep comments concise and only where they clarify non-obvious behavior.

## Data Source Changes

Market data providers often change schemas without notice. When changing fetchers:

- Add fixtures or tests for the observed schema.
- Preserve graceful degradation where possible.
- Do not hard-code private cookies or account-specific headers.

## Security

If you find a token leak, credential handling issue, or unsafe logging path, follow `SECURITY.md` instead of opening a public issue.
