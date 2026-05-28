# Security Policy

## Supported Versions

This repository currently supports the latest `main` branch only.

## Reporting a Vulnerability

Do not open a public issue for:

- bot tokens
- Feishu/Lark app secrets
- chat ids tied to private accounts
- holdings, account balances, or trading logs
- SQLite databases or generated runtime artifacts
- unsafe logging of credentials

Report privately to the repository maintainer. If no private channel is listed on the repository, open a minimal public issue that says a private security report is needed, without including the sensitive details.

## Secret Handling

The project expects secrets to live in local ignored files such as `.env`.

Before publishing or pushing:

```bash
git status --short
git grep -n -I -E 'TG_BOT_TOKEN=[0-9]{6,}:[A-Za-z0-9_-]{20,}|FEISHU_APP_SECRET=[A-Za-z0-9_-]{20,}|Authorization:[[:space:]]+Bearer[[:space:]]+[A-Za-z0-9._-]{20,}' -- . ':!uv.lock' ':!README.md' ':!SECURITY.md'
```

Rotate any token that has appeared in logs, terminal output, screenshots, chat messages, or committed history.

## Runtime Data

The following paths must remain private:

- `.env`
- `data/`
- `logs/`
- `holdings.yaml`
- `risk_config.yaml`
- `risk_state.yaml`

These may contain personal trading data, chat identifiers, or account-specific runtime state.
