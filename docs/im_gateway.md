# IM gateway 运行手册

本文记录当前 IM 接入方式。默认只跑飞书和个人微信 iLink，旧版单通道 listener 已从当前 runtime 移除。

## 运行模型

IM gateway 入口是 `stock_codex.apps.channel_listener`，由 `scripts/start_gateway.sh` 启动。它负责两件事：

- 监听飞书 WebSocket 和微信 iLink 长轮询，把用户消息交给 `stock_codex.apps.command_router`。
- 处理出站推送。飞书直接发送，微信先写入 `channel_outbox`，再由常驻 listener 通过 iLink `sendmessage` 发出。

涉及的本地状态：

| 路径或表 | 用途 |
|---|---|
| `.env` | IM 凭证和默认通道配置。 |
| `data/channel_gateway_state.json` | gateway 当前状态，包含启用通道、adapter 状态、pid 和最近错误。 |
| `data/weixin_context_tokens.json` | 微信 peer 到 context token 的映射，收到微信私聊后更新。 |
| `channel_outbox` | 微信这类连接绑定通道的待发送队列。 |
| `channel_outbound_log` | 出站审计日志，飞书和微信都会写。 |
| `channel_inbound_log` | 入站审计日志。 |

## 环境变量

最小通道配置：

```dotenv
CHANNEL_DEFAULT=feishu
CHANNELS_ENABLED=feishu,weixin
CHANNELS_NOTIFY=feishu,weixin
```

`CHANNELS_ENABLED` 控制 gateway 启动哪些 listener，`CHANNELS_NOTIFY` 控制定时任务和 `notify.push` fan-out 到哪些通道。当前推荐保持两者一致。如果微信还没扫码，可以临时把两者都设为 `feishu`。

飞书配置：

```dotenv
FEISHU_ENABLED=1
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_HOME_CHANNEL=
FEISHU_ALLOWED_CHAT_IDS=
FEISHU_CONNECTION_MODE=websocket
FEISHU_REQUIRE_MENTION=true
FEISHU_CARD=true
```

个人微信 iLink 配置：

```dotenv
WEIXIN_ACCOUNT_ID=ilink-bot
WEIXIN_TOKEN=
WEIXIN_BASE_URL=https://ilinkai.weixin.qq.com
WEIXIN_HOME_CHANNEL=
WEIXIN_GROUP_POLICY=disabled
```

`WEIXIN_TOKEN` 和 `WEIXIN_BASE_URL` 由扫码脚本写入。`WEIXIN_HOME_CHANNEL` 是默认推送目标，需要先给微信机器人私聊一条消息，拿到 peer id 后再写入。

## 首次配置

安装依赖和数据库：

```bash
uv sync --group dev
mkdir -p data
sqlite3 data/daily.db < stock_codex/schema/init_db.sql
uv run --no-sync python scripts/migrate_channels.py
```

配置飞书：

```bash
uv run --no-sync python scripts/configure_feishu.py
```

配置个人微信：

```bash
uv run --no-sync python scripts/configure_weixin.py
```

扫码成功后，先在微信里给机器人私聊一条消息，比如 `ping`。gateway 收到后会写 `data/weixin_context_tokens.json`。把里面的 peer id 写入 `.env`：

```dotenv
WEIXIN_HOME_CHANNEL=o...@im.wechat
```

## 启动和重启

```bash
bash scripts/start_gateway.sh
RESTART_GATEWAY=1 bash scripts/start_gateway.sh
```

macOS 下脚本会用 `launchctl submit` 启动 `com.user.stockchannelgateway`。检查运行状态：

```bash
launchctl list | rg 'stockchannelgateway' || true
cat data/channel_gateway_state.json
```

正常状态类似：

```json
{
  "adapters": {
    "feishu": "running",
    "weixin": "running"
  },
  "channels": [
    "feishu",
    "weixin"
  ],
  "last_error": null
}
```

如果微信还没配置 token，状态会显示 `weixin: config_missing`。这不是飞书故障，飞书可以继续运行。

## 推送验证

```bash
uv run --no-sync python -m stock_codex.infra.notify test

sqlite3 data/daily.db "SELECT id, channel, status, attempts, COALESCE(last_error, '') FROM channel_outbox ORDER BY id DESC LIMIT 5;"
sqlite3 data/daily.db "SELECT id, channel, success, source, COALESCE(error, '') FROM channel_outbound_log ORDER BY id DESC LIMIT 8;"
```

预期结果：

- 飞书收到 `✅ 推送通道已连通`。
- 微信收到同一条测试消息。
- `channel_outbox` 里微信最近一条为 `sent`。
- `channel_outbound_log` 里同一次 `manual-test` 同时有 `feishu success=1` 和 `weixin success=1`。

## 常见问题

### 微信显示 config_missing

`WEIXIN_TOKEN` 为空或 `.env` 没被加载。重新扫码并重启 gateway：

```bash
uv run --no-sync python scripts/configure_weixin.py
RESTART_GATEWAY=1 bash scripts/start_gateway.sh
```

### 微信主动推送没有到

先看数据库，不要只看聊天窗口：

```bash
sqlite3 data/daily.db "SELECT id, channel, target, status, attempts, COALESCE(last_error, '') FROM channel_outbox ORDER BY id DESC LIMIT 10;"
sqlite3 data/daily.db "SELECT id, channel, success, source, COALESCE(error, '') FROM channel_outbound_log ORDER BY id DESC LIMIT 10;"
tail -n 120 logs/channel_listener.log
```

常见原因：

- `channel_outbox` 表不存在，运行 `uv run --no-sync python scripts/migrate_channels.py`。
- `WEIXIN_HOME_CHANNEL` 为空，先给机器人私聊一条消息，再写入 `.env`。
- context token 过期，重新给机器人私聊一条消息刷新 `data/weixin_context_tokens.json`。
- gateway 没有运行，重启 `scripts/start_gateway.sh`。

### 飞书和微信都收到两条

这通常不是 IM fan-out 问题，而是上游任务重复执行或 `push.py` 在推送成功后又报错，触发自动化重试。排查：

```bash
sqlite3 -header -column data/daily.db "SELECT id,timestamp,source,success,error,length(text) FROM push_log ORDER BY id DESC LIMIT 20;"
sqlite3 -header -column data/daily.db "SELECT id,timestamp,channel,source,success,length(text) FROM channel_outbound_log ORDER BY id DESC LIMIT 40;"
```

如果同一 source 在很短时间内有两条正文长度和 hash 相同的记录，先查自动化运行日志和 `~/.codex/automations/<job>/memory.md`。当前 `push.py` 已兼容新版 IM 返回结构，打印 `msg_id` 失败不会再导致已发送消息被重试。

### 日志里还有旧通道名称

历史 memory、旧 changelog 或旧审计表名可能保留旧通道名称。当前 runtime 以 `CHANNELS_ENABLED=feishu,weixin`、`com.user.stockchannelgateway` 和 `stock_codex.apps.command_router` 为准。确认没有旧 listener：

```bash
launchctl list | rg 'stockchannelgateway' || true
```

输出里应出现 `com.user.stockchannelgateway`。
