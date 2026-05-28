# A Stock Agent

面向 A 股短线研究的本地智能助理。它把盘前计划、盘中纪律提醒、盘后复盘、周复盘、异动监控、个股/题材问答接到 Telegram 和飞书里，适合个人在本机长期运行。

本项目不自动下单，不连接券商，不承诺收益。它的定位是「研究 + 记录 + 推送」工具，而不是交易机器人。

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green" alt="License: MIT"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.11%20%7C%203.12-blue" alt="Python 3.11/3.12"></a>
  <a href="tests"><img src="https://img.shields.io/badge/tests-pytest-blue" alt="pytest"></a>
  <img src="https://img.shields.io/badge/runtime-macOS-lightgrey" alt="macOS runtime">
</p>

## 适合谁

- 想用 AI 辅助整理 A 股短线交易计划，但仍然自己做最终决策的人。
- 想把盘前、盘中、盘后流程固定下来，减少临盘随意操作的人。
- 想通过 Telegram 或飞书随时问「这只票怎么看」「这个题材能不能参与」的人。
- 想在本机运行，不想把持仓、token、交易记录放到第三方 SaaS 的人。

不适合：

- 想要自动买卖股票的人。
- 想要保证胜率或收益的人。
- 不愿意维护 Telegram/飞书机器人配置、本机环境和数据源可用性的人。

## 能做什么

| 能力 | 说明 |
|------|------|
| 盘前计划 | 生成今日是否出手、主攻/潜伏/备选/禁买、仓位和触发条件。 |
| 盘中提醒 | 在 09:30、09:45、11:30、14:30 给纪律提醒或叙事复盘。 |
| 异动监控 | 盘中监控观察池、持仓、涨停/炸板/新高等异动。 |
| 盘后复盘 | 总结当日题材、情绪、消息、持仓处理和明日预案。 |
| 周复盘 | 周日晚生成本周市场叙事和下周重点方向。 |
| 随时问答 | 在 Telegram/飞书发送股票代码、股票名或 `/ask <问题>`。 |
| IM 网关 | 一个进程统一处理 Telegram long polling 和飞书 WebSocket。 |
| 多通道推送 | 定时任务可同时推送到 Telegram 和飞书。 |
| 本地留痕 | SQLite 记录命令、推送、决策票和运行状态，便于回溯。 |

## 三分钟快速开始

新手推荐直接跑快速安装脚本：

```bash
git clone https://github.com/wispig66/a-stock-agent.git
cd a-stock-agent
bash scripts/quickstart.sh
```

脚本会做这些事：

1. 检查并安装 `uv`。
2. 安装 Python 依赖。
3. 初始化 `data/daily.db`。
4. 创建 `.env`。
5. 让你输入 Telegram Bot Token 和 Chat ID。
6. 设置 Telegram 菜单。
7. 启动统一 IM gateway。

运行成功后，在 Telegram 给机器人发送：

```text
/help
600519
/ask 光伏今天能不能看
```

常用安装变体：

```bash
# 只安装和初始化，不启动 IM gateway
bash scripts/quickstart.sh --no-start

# 安装后顺便配置飞书
bash scripts/quickstart.sh --with-feishu

# 安装 Codex 定时任务和 launchd 长时服务
bash scripts/quickstart.sh --install-schedule

# 非交互安装，适合服务器或脚本化部署
TG_BOT_TOKEN=xxx TG_CHAT_ID=yyy bash scripts/quickstart.sh
```

## 手动安装

如果你想一步步控制安装过程：

```bash
git clone https://github.com/wispig66/a-stock-agent.git
cd a-stock-agent

uv sync --group dev
cp .env.example .env
sqlite3 data/daily.db < stock_codex/schema/init_db.sql
uv run --no-sync python scripts/migrate_channels.py
uv run --no-sync python -m stock_codex.tools.refresh_calendar
```

编辑 `.env`，至少填入：

```dotenv
TG_BOT_TOKEN=
TG_CHAT_ID=
CHANNEL_DEFAULT=telegram
CHANNELS_ENABLED=telegram
CHANNELS_NOTIFY=telegram
```

启动：

```bash
bash scripts/start_tg_listener.sh
```

停止：

```bash
bash scripts/stop_tg_listener.sh
```

`start_tg_listener.sh` 这个名字为了兼容旧用法保留；实际启动的是统一 IM gateway：`scripts/channel_listener.py`。

## Telegram 配置

1. 在 Telegram 找到 `@BotFather`。
2. 创建 bot，拿到 `TG_BOT_TOKEN`。
3. 给 bot 发一条消息，或把 bot 拉进群。
4. 获取你的 `TG_CHAT_ID`。
5. 写入 `.env`。
6. 运行：

```bash
uv run --no-sync python scripts/set_tg_commands.py
bash scripts/start_tg_listener.sh
```

如果出现 `409 Conflict`，通常说明同一个 bot token 有两个进程在同时 long polling。先停掉旧进程：

```bash
bash scripts/stop_tg_listener.sh
```

再重新启动。

## 飞书配置

飞书固定使用官方 `lark-oapi` WebSocket SDK，不需要公网 webhook 地址。推荐通过向导配置：

```bash
uv run --no-sync python scripts/configure_feishu.py
```

最小配置：

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

飞书开放平台建议开启：

| 项 | 建议 |
|----|------|
| 连接方式 | 长连接 / WebSocket |
| 消息事件 | `im.message.receive_v1` |
| 菜单事件 | `application.bot.menu_v6` |
| 群聊策略 | 群聊默认需要 @bot，私聊直接响应 |

推荐菜单：

| 菜单 | `event_key` | 作用 |
|------|-------------|------|
| 帮助 | `help` | 发送帮助说明 |
| 单股分析 | `query` | 提示用户发送代码或股票名 |
| 随时分析 | `ask` | 提示用户使用 `/ask` |

## 常用命令

```bash
# 快速安装/启动
bash scripts/quickstart.sh

# 诊断本机运行环境
bash scripts/doctor_codex_runtime.sh

# 运行测试
uv run --no-sync pytest -q

# 刷新交易日历
uv run --no-sync python -m stock_codex.tools.refresh_calendar

# 配置飞书
uv run --no-sync python scripts/configure_feishu.py

# 启动/停止 IM gateway
bash scripts/start_tg_listener.sh
bash scripts/stop_tg_listener.sh
```

## 调度

本项目按「本机运行」设计：电脑保持在线，Codex automations 负责短时 LLM 任务，launchd 运行长时 daemon 负责盘中常驻监听和轮询。

| 类型 | 任务 | 入口 |
|------|------|------|
| Codex automations | 盘前计划 | `stock-premarket` |
| Codex automations | 盘中四个时点 | `stock-intraday-*` |
| Codex automations | 盘后复盘 | `stock-postmarket` |
| Codex automations | 周复盘 | `stock-weekly-review` |
| launchd 运行长时 daemon | 观察池/持仓轮询 | `com.user.stockwatchloop` |
| launchd 运行长时 daemon | 全市场异动监控 | `com.user.stockanomalyloop` |
| launchd 运行长时 daemon | 题材冒头监控 | `com.user.stockthemeloop` |
| launchd 运行长时 daemon | 可选 IM gateway 常驻 | `com.user.stocktglistener` |

安装调度：

```bash
bash scripts/sync_codex_skills.sh
bash scripts/install_codex_automations.sh
bash scripts/install_runtime_services.sh
```

也可以在快速安装时一次完成：

```bash
bash scripts/quickstart.sh --install-schedule
```

注意：如果仓库放在 `~/Desktop` 等受 macOS TCC 保护的目录，launchd 后台进程可能无法正常读取当前目录。更推荐放在 `~/code/a-stock-agent`，或者用 `bash scripts/start_tg_listener.sh` 在交互式终端里启动 IM gateway。

## 项目结构

```text
.agents/skills/          Codex skill：盘前、盘中、盘后、周复盘、异动、问答
stock_codex/             Python 包
  apps/                  runtime 入口
  channels/              Telegram / 飞书 gateway adapter
  domain/                交易日历、持仓、风控、决策票
  infra/                 SQLite、日志、通知
  market/                行情、题材、事件、卡片数据
  schema/                SQLite schema
scripts/                 安装、迁移、诊断、配置脚本
bin/                     launchd/manual runtime 入口
launchd/                 launchd 模板
tests/                   pytest 测试
docs/                    运行手册和设计文档
```

## 配置和隐私

这些文件是模板，可以提交：

- `.env.example`
- `holdings.example.yaml`
- `risk_config.example.yaml`
- `risk_state.example.yaml`

这些文件是本地私有数据，不要提交：

- `.env`
- `data/`
- `logs/`
- `holdings.yaml`
- `risk_config.yaml`
- `risk_state.yaml`

公开仓库或推送前建议检查：

```bash
git status --short
git grep -n -I -E 'TG_BOT_TOKEN=[0-9]{6,}:[A-Za-z0-9_-]{20,}|FEISHU_APP_SECRET=[A-Za-z0-9_-]{20,}|Authorization:[[:space:]]+Bearer[[:space:]]+[A-Za-z0-9._-]{20,}' -- . ':!uv.lock' ':!README.md' ':!SECURITY.md'
```

如果 token 曾经出现在日志、截图、聊天记录或提交历史里，应该直接轮换。

## 数据源

| 数据源 | 用途 |
|--------|------|
| AKShare | A 股行情、涨跌停、交易日历等基础数据 |
| 同花顺 | 热点题材、概念归因 |
| 东方财富 | 榜单、行情和部分 fallback 数据 |
| mootdx | 通达信行情/K 线备用通道 |
| 财联社 | 消息面上下文 |

数据源可能限流、变更字段、受代理规则影响。项目会尽量降级处理，但不能保证任何第三方数据源永远可用。

## 开发者

开发环境：

```bash
uv sync --group dev
uv run --no-sync pytest -q
```

提交前建议：

```bash
git diff --check
uv run --no-sync pytest -q
```

贡献方向：

- 新数据源适配和字段变更修复。
- Telegram/飞书 gateway 稳定性改进。
- 更多 IM adapter。
- 卡片质量、风险提示、回测记录和决策复盘。
- 文档和新手安装体验。

提交 PR 前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题请看 [SECURITY.md](SECURITY.md)，不要在公开 issue 里贴 token、chat id、持仓或日志。

## 致谢

- [AKShare](https://github.com/akfamily/akshare)
- [mootdx](https://github.com/mootdx/mootdx)
- [simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data)
- [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) 的开源项目首页结构给了本文档一些参考。

## 许可证

MIT，见 [LICENSE](LICENSE)。

## 免责声明

本项目仅用于学习、研究、记录和提醒，不构成投资建议、交易建议、法律建议或税务建议。所有交易由使用者自行判断并手工执行，任何收益或损失由使用者自行承担。
