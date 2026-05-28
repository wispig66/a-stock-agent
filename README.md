# A Stock Agent

A local-first A-share research assistant built around Codex skills, market data fetchers, SQLite, and IM notifications. It generates pre-market plans, intraday discipline reminders, post-market reviews, weekly reviews, anomaly alerts, and on-demand stock/sector analysis through Telegram and Feishu.

> This project is for research, journaling, and notification workflows only. It does not place orders, does not provide investment advice, and should not be used as an automated trading system.

## Features

- **A-share workflow**: pre-market plan, intraday checkpoints, post-market review, weekly review, and anomaly monitoring.
- **On-demand analysis**: send a stock code/name or `/ask <question>` from Telegram or Feishu.
- **Unified IM gateway**: Telegram long polling and Feishu WebSocket run under one gateway process.
- **Notification fanout**: scheduled jobs can push to Telegram and Feishu with `CHANNELS_NOTIFY=telegram,feishu`.
- **Feishu rendering**: scheduled Feishu notifications are sent as interactive markdown cards.
- **Auditable state**: SQLite tables store inbound commands, outbound notifications, decisions, and runtime events.
- **Local runtime**: designed to run on a personal Mac with `uv`, Codex, SQLite, and launchd.

## What This Is Not

- It is not a brokerage integration.
- It does not execute trades.
- It is not a guaranteed signal engine.
- It is not a hosted SaaS service.
- It is not a replacement for independent risk control.

## Architecture

```text
Codex skills
  stock-premarket    -> pre-market decision tickets
  stock-intraday     -> scheduled intraday cards
  stock-postmarket   -> post-market review
  stock-weekly       -> weekly review
  stock-anomaly      -> anomaly digests
  stock-query/ask    -> on-demand analysis

Runtime services
  scripts/channel_listener.py  -> Telegram + Feishu inbound gateway
  watch_loop.py                -> intraday threshold alerts
  anomaly_loop.py              -> market anomaly alerts
  theme_emergence_loop.py      -> theme emergence alerts

Storage
  data/daily.db                -> SQLite runtime database
  data/fact_pack/*.md          -> generated market context
  holdings.yaml                -> local user holdings, ignored by git
```

## Requirements

- macOS is the primary supported runtime.
- Python `>=3.11,<3.13`
- [`uv`](https://docs.astral.sh/uv/)
- Codex CLI/Desktop environment for skill execution
- Telegram bot token and chat id for Telegram integration
- Feishu/Lark app credentials for Feishu integration
- Network access to market data sources such as AkShare, Eastmoney, Tonghuashun, CLS, and Telegram/Feishu APIs

## Quick Start

```bash
git clone <your-fork-url>
cd a-stock-agent

uv sync --group dev
cp .env.example .env
sqlite3 data/daily.db < stock_codex/schema/init_db.sql
uv run python -m stock_codex.tools.refresh_calendar
uv run --no-sync pytest -q
```

Edit `.env` before running services. At minimum, configure Telegram:

```dotenv
TG_BOT_TOKEN=
TG_CHAT_ID=
CHANNEL_DEFAULT=telegram
CHANNELS_ENABLED=telegram
CHANNELS_NOTIFY=telegram
```

Then start the unified IM gateway:

```bash
bash scripts/start_tg_listener.sh
```

The script name is kept for backward compatibility; it now starts `scripts/channel_listener.py`.

## Feishu Setup

Feishu uses the official `lark-oapi` WebSocket client. No public webhook endpoint is required.

Run the helper:

```bash
uv run python scripts/configure_feishu.py
```

The minimum Feishu configuration is:

```dotenv
CHANNELS_ENABLED=telegram,feishu
CHANNELS_NOTIFY=telegram,feishu
FEISHU_ENABLED=1
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_HOME_CHANNEL=
FEISHU_ALLOWED_CHAT_IDS=
FEISHU_CONNECTION_MODE=websocket
FEISHU_REQUIRE_MENTION=true
```

Recommended Feishu console settings:

- Enable message receive event: `im.message.receive_v1`
- Enable bot custom menu event if needed: `application.bot.menu_v6`
- Enable WebSocket/long connection mode
- Add the bot to the target chat

Recommended bot menu keys:

| Menu | `event_key` | Behavior |
|------|-------------|----------|
| Help | `help` | Sends the help text |
| Stock analysis | `query` | Explains how to send a code/name |
| Ask | `ask` | Explains `/ask` and `/ask+` |

## Common Commands

```bash
# Run all tests
uv run --no-sync pytest -q

# Initialize or migrate SQLite schema
sqlite3 data/daily.db < stock_codex/schema/init_db.sql
uv run --no-sync python scripts/migrate_channels.py

# Start/stop unified IM gateway
bash scripts/start_tg_listener.sh
bash scripts/stop_tg_listener.sh

# Configure Telegram command menu
uv run --no-sync python scripts/set_tg_commands.py

# Runtime diagnostics
bash scripts/doctor_codex_runtime.sh
```

## 调度

This repository is built for a local machine that stays awake during market hours. 调度分两层：`Codex automations` 负责短时 LLM jobs，`launchd 运行长时 daemon` 负责长驻监听和轮询。

| 类型 | 任务 | 入口 |
|------|------|------|
| Codex automations | L1 pre-market | `stock-premarket` |
| Codex automations | L2 intraday checkpoints | `stock-intraday-*` |
| Codex automations | L4 post-market | `stock-postmarket` |
| Codex automations | weekly review | `stock-weekly-review` |
| launchd 运行长时 daemon | watch loop | `com.user.stockwatchloop` |
| launchd 运行长时 daemon | anomaly loop | `com.user.stockanomalyloop` |
| launchd 运行长时 daemon | theme loop | `com.user.stockthemeloop` |
| launchd 运行长时 daemon | optional IM gateway | `com.user.stocktglistener` |

Install local automations and launchd services:

```bash
bash scripts/sync_codex_skills.sh
bash scripts/install_codex_automations.sh
bash scripts/install_runtime_services.sh
```

The Telegram/IM gateway launchd template is disabled by default because macOS TCC can block background processes when the repository is under protected folders such as `~/Desktop`. Prefer keeping the repository under `~/code` or use the interactive start script.

## Configuration Files

Tracked templates:

- `.env.example`
- `holdings.example.yaml`
- `risk_config.example.yaml`
- `risk_state.example.yaml`

Ignored local files:

- `.env`
- `data/`
- `logs/`
- `holdings.yaml`
- `risk_config.yaml`
- `risk_state.yaml`

Do not commit real bot tokens, Feishu secrets, holdings, account balances, SQLite databases, logs, or generated fact packs.

## Data Sources

| Source | Use |
|--------|-----|
| AkShare | A-share market data, limit-up/down pools, calendars |
| Tonghuashun | concept and reason tags |
| Eastmoney | rankings and market data fallbacks |
| mootdx | Tongdaxin quote/K-line access |
| CLS | news context |

Some sources may be rate-limited, blocked by proxy rules, or change response schemas without notice. The code favors graceful degradation where possible.

## Project Layout

```text
.agents/skills/          Codex skill definitions and skill-local scripts
stock_codex/             Installable Python package
  apps/                  Runtime entrypoints
  channels/              Telegram/Feishu gateway adapters
  domain/                Calendar, holdings, risk, decisions
  infra/                 DB, logging, notification helpers
  market/                Data packs, intent, sector/event logic
  schema/                SQLite schema
scripts/                 Setup, migration, diagnostics, wrappers
bin/                     launchd shell entrypoints
launchd/                 launchd templates
tests/                   pytest suite
docs/                    Operational notes and schemas
```

## Testing

```bash
uv run --no-sync pytest -q
```

Network-facing tests are intentionally limited. Most tests use mocks or local SQLite databases.

## Open Source Readiness

Before publishing a fork or public repository:

1. Confirm `git status --short` is clean.
2. Run the tests.
3. Verify `.env`, `data/`, `logs/`, `holdings.yaml`, and risk files are not tracked.
4. Search for secrets:

```bash
git grep -n -I -E 'TG_BOT_TOKEN=[0-9]{6,}:[A-Za-z0-9_-]{20,}|FEISHU_APP_SECRET=[A-Za-z0-9_-]{20,}|Authorization:[[:space:]]+Bearer[[:space:]]+[A-Za-z0-9._-]{20,}' -- . ':!uv.lock' ':!README.md' ':!SECURITY.md'
```

5. Rotate any token that has ever appeared in logs or chat transcripts.

## Contributing

Contributions are welcome, but this codebase is optimized for a local research workflow rather than a generic SaaS product. See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## Security

Please do not file public issues containing tokens, chat ids, holdings, account balances, or logs. See [SECURITY.md](SECURITY.md).

## Acknowledgements

- [AKShare](https://github.com/akfamily/akshare)
- [mootdx](https://github.com/mootdx/mootdx)
- [simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data) for data endpoint references under Apache 2.0

## License

MIT. See [LICENSE](LICENSE).

## Disclaimer

This project is provided for educational and research purposes only. It does not provide financial, investment, legal, tax, or trading advice. You are solely responsible for every trading decision and for complying with applicable laws, broker rules, platform terms, and data-source terms.
