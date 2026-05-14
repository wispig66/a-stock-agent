# A 股 T+1 机制全项目改造 · 设计文档

- **日期**：2026-05-14
- **状态**：design / pending implementation
- **范围**：watch_loop、4 个 skill、holdings 数据模型、新增 Telegram 入站 bot
- **背景**：审计发现项目对 A 股 T+1 机制（当日买入、最早 T+1 才能卖）的考虑约 20%——架构上有 `buy_date` 字段，但所有告警/纪律/skill 文案都未把"承诺过夜"显式化，盘中止损告警对今日新仓物理上不可执行（参见开头案例：000601 10:05 触发买点、10:15 破止损"立即出"，但 T+1 下 10:15 卖不掉）。

## 1. 目标

1. **告警措辞物理可执行**：当日新仓盘中破止损/止盈，告警不再出现"立即出"，改为"明早开盘预案"。
2. **买点信号过夜质量**：每个买点信号 = 16 小时持仓承诺，前置 sanity check 屏蔽显著不该追的形态。
3. **持仓状态一等公民**：`is_locked(today)` 作为所有下游告警/skill 的统一判定来源。
4. **实时录入闭环**：买入操作通过 Telegram bot 入站，使 watch_loop 盘中即可识别今日新仓。
5. **信号质量可复盘**：所有买点告警入流水，盘后能区分"听系统的盈亏 vs 自主追高的盈亏"。

## 2. 非目标

- 不接入券商 API 自动下单。
- 不引入 SQLite 替代 holdings.yaml（保持可 vim 直接看的简单性）。
- 不重构派别 A/B/C/D 框架（只补"过夜本质"一行 + 派别 B 加严过滤）。
- 不做日内 T+0 模拟（A 股不支持）。

## 3. 架构概览

```
                            Telegram (云端)
                          ↑ 推送        ↓ 入站
                          │            │
       ┌──────────────────┴───┐  ┌─────┴──────────┐
       │ watch_loop.py        │  │ bot_inbound.py │ (新增进程)
       │ - 90s 轮询行情        │  │ - long polling │
       │ - sanity.py 前置过滤  │  │ - /buy /sell ..│
       │ - alert_router 分轨   │  │ - 白名单校验    │
       └──────────┬───────────┘  └───────┬────────┘
                  │ 读                    │ 写
                  ↓                      ↓
       ┌────────────────────────────────────────┐
       │ holdings.yaml (filelock + 原子 rename) │
       │ pending_signals.jsonl (append-only)    │
       │ trades.jsonl (成交流水)                 │
       │ data/trade_calendar.csv                │
       └────────────────────────────────────────┘
                  ↑ 读
       4 个 skill (premarket / intraday / anomaly / postmarket)
```

watch_loop 与 bot_inbound 独立进程，文件 + filelock 同步。所有 skill 仍按原触发时点跑，文案改写后从 holdings.yaml 读 `is_locked` 状态分轨。

## 4. 数据模型

### 4.1 holdings.yaml schema

```yaml
- code: "000601"
  name: "韶能股份"
  school: "B"                  # 新增：派别 A/B/C/D
  buy_date: "2026-05-14"       # 已有
  unlock_date: "2026-05-15"    # 新增：T+1 解锁日（next_trade_day(buy_date)）
  cost: 9.02                   # 加权均价
  position_pct: 30
  stop_loss: 8.90
  take_profit: 9.50
  source: "watch_loop_buy_alert" | "manual"  # 新增：买入触发源
```

- 历史持仓缺 `school`/`unlock_date` 视为 `已解锁`，走可卖仓分轨。
- 同 code 多次加仓：cost 取加权均价，unlock_date 取**最新一笔**的 next_trade_day（最保守）。

### 4.2 pending_signals.jsonl（append-only）

```json
{"ts": "2026-05-14T10:05:23", "code": "000601", "school": "B",
 "trigger_price": 9.02, "sanity_check": "pass" | "soft_block" | "hard_block",
 "block_reason": null | "炸板回撤 -7.9%" | ...,
 "user_action": null | "bought" | "ignored"}
```

bot 收到 `/buy 000601 ...` 时，在最近 30 分钟内 match 同 code 的 pending_signal，回填 `user_action: bought`。

### 4.3 trades.jsonl（成交流水）

bot 处理完 `/sell` 后 append。盘后复盘读这个文件做"系统触发 vs 自主追高"的盈亏对比。

### 4.4 data/trade_calendar.csv

单列日期，akshare `tool_trade_date_hist_sina()` 拉取，覆盖至次年底。`scripts/refresh_calendar.py` 幂等更新；watch_loop 启动时检查覆盖范围，不足则触发刷新；cron 每月 1 日双保险。

## 5. 新增模块：scripts/lib/

### 5.1 calendar.py

```python
def is_trade_day(d: date) -> bool
def next_trade_day(d: date) -> date  # unlock_date 计算入口
def trade_days_between(a: date, b: date) -> int
```

### 5.2 holdings.py

```python
class Holding:
    def is_locked(self, today: date) -> bool: return today < self.unlock_date

def read_holdings() -> list[Holding]  # filelock 共享锁
def upsert_holding(h: Holding) -> None  # 加权均价合并 + 原子 rename
def remove_holding(code: str) -> Holding  # /sell 时调用
```

### 5.3 sanity.py

5 条规则，每条返回 `pass | soft_block | hard_block` 之一。派别 B 走加严表（详见 §6.1）。

### 5.4 alert_router.py

```python
def render_alert(alert_type, code, price, holding: Holding | None, today) -> str
# 6 类 alert × 2 轨（locked / unlocked / no_holding）= 渲染矩阵
```

## 6. watch_loop.py 改造

### 6.1 买点 sanity check（sanity.py）

| 规则 | 阈值 | 通用派别 | 派别 B |
|---|---|---|---|
| 当日封板后炸板 | 现价较封板价回撤 > 5% | soft_block | hard_block |
| 当日大阴线 | 实体跌幅 > 3% 且收价 < 均价 | soft_block | hard_block |
| 当日成交量过热 | > 5 日均量 3 倍 | soft_block | soft_block |
| 当日跌停过 | 哪怕已打开 | hard_block | hard_block |
| 当日累计振幅 | > 12% | soft_block | hard_block |

行为：
- `pass` → 发买点告警 + append pending_signals（sanity_check: pass）
- `soft_block` → 不发买点，发"🟡 观察池信号弱化（原因：xxx），不建议追入" + append
- `hard_block` → 完全静默，只记日志 + append；盘后 skill 汇总"今日 N 个买点被屏蔽"

阈值首期凭直觉给定，跑两周后回看 pending_signals 调参。

### 6.2 告警分轨（alert_router.py）

每条 alert 渲染前查 holdings：

```
止损/止盈 alert × is_locked(today):
  True  → "⚠️ {code} 现价 {p} ≤ 止损 {sl}（假突破已现）
            📌 T+1 锁仓中（{unlock_date} 后可卖）
            🌅 明早开盘预案：竞价评估，破开盘价 -3% 内出，跌停一字则次日再说"
  False → "🚨 {code} 现价 {p} ≤ 止损 {sl} → 立即出"
  no_holding → 走观察池原文案
```

6 类告警（火箭/封板/炸板/买点/止损/止盈）全部走这个 router。

### 6.3 数据流

```
tick (90s)
  ↓
拉行情
  ↓
对每只 watched_code:
  ├─ holding = read_holdings().get(code)
  ├─ 价格触发判断
  ├─ 若买点信号 → sanity.check() → 决定发送 + append pending_signals
  └─ 若止损/止盈/异动 → alert_router.render(holding, today) → 发送
  ↓
push Telegram + 写 audit log (SQLite, 新增事件类型 sanity_block)
```

### 6.4 不变

- 90s 轮询节奏。
- 东财→新浪 数据源 fallback。
- SQLite audit log 结构（仅新增 sanity_block 事件类型）。

## 7. Telegram 入站 bot（新增 bot_inbound.py）

### 7.1 进程与依赖

- 库：`python-telegram-bot`（已用于推送，复用 token）。
- 模式：long polling，与 watch_loop 姊妹进程并行。
- 部署：macOS launchd 或 systemd user unit，`scripts/run_bot.sh` 启动脚本。

### 7.2 命令集

| 命令 | 用法 | 行为 |
|---|---|---|
| `/buy` | `/buy 000601 9.02 30 B [sl=8.9] [tp=9.5]` | 写 holdings.yaml；buy_date=today；unlock_date=next_trade_day(today)；同 code 加仓走加权均价；append trades.jsonl；回填最近 30 分钟同 code 的 pending_signals.user_action=bought |
| `/sell` | `/sell 000601 9.50` | 校验 `today >= unlock_date`，违规拒单："⛔ T+1 锁仓中，最早 {unlock_date} 可卖"。通过则 remove + append trades.jsonl |
| `/list` | `/list` | 当前持仓 + 每条锁仓状态 |
| `/cancel` | `/cancel 000601` | 撤回最近 5 分钟内的同 code /buy |
| `/setsl` | `/setsl 000601 8.95` | 修改止损价（盘中常用） |

### 7.3 安全

- `TELEGRAM_ALLOWED_CHAT_IDS` 环境变量白名单。非白名单消息静默丢弃 + WARN 日志。
- 命令格式错误回固定模板，不解释更多（防探测）。
- token 仍走环境变量，不入库。

### 7.4 错误处理

- code 校验：6 位数字 + 前缀映射（60→sh, 00/30→sz, 68→sh sci）。
- 重复 /buy 同 code：合并加仓 + 加权均价 + unlock_date 取最新一笔。
- Telegram 断连 > 5 分钟：watch_loop 推一条"⚠️ bot 失联"告警。

## 8. 4 个 skill 文案 T+1 化

### 8.1 stock-premarket

1. 顶部加固定提示卡：
   ```
   ⚠️ T+1 提醒：今日买入的票最早 {next_trade_day} 可卖。
     盘中买点触发 = 16 小时持仓承诺。
   ```
2. 观察池条目模板加"过夜风险"行 + "T+1 约束"行。
3. 派别说明增加"过夜本质"一行；派别 C "T+1 不强势就出"补可量化定义（次日早盘弱于大盘 1% 以上）。

### 8.2 stock-intraday

1. 4 个时点的持仓建议按 `is_locked` 分轨：
   - 09:30 / 09:45：今日新仓段落不出现。
   - 11:30：可卖仓"是否减"，今日新仓"是否加仓"。
   - 14:30：新增段落"今日新仓过夜评估"——板块情绪 + K 线形态 + 明日风险点 + 三档预案（高开 +2%+/平开/低开 -2%-）。

### 8.3 stock-anomaly

异动卡片若推荐新方向，末尾自动加："📌 接力建议：日内追入 = T+1 锁仓，建议明日盘前评估或仅尾盘 1% 仓位试探"。

### 8.4 stock-postmarket

1. 复盘卡片按 `unlock_date` 分两段：**今日新仓（T+1 锁仓，明早处理）** + **可卖仓（已解锁）**。
   - 新仓段每只票给三档明早预案 + 盯点。
   - 可卖仓段沿用现有逻辑。
2. 新增"今日买点信号质量复盘"段（读 pending_signals.jsonl）：
   - 今日告警数 × sanity 分布 × 用户实际买入数
   - 系统触发买入盈亏 vs 自主买入盈亏对比（多周累计后输出趋势）
3. 数据落库时同步刷新 trade_calendar.csv（每天 15:35，幂等）。

## 9. 文件清单（新增 / 改动）

新增：
- `scripts/lib/calendar.py`
- `scripts/lib/holdings.py`
- `scripts/lib/sanity.py`
- `scripts/lib/alert_router.py`
- `scripts/bot_inbound.py`
- `scripts/refresh_calendar.py`
- `scripts/run_bot.sh`
- `data/trade_calendar.csv`
- `pending_signals.jsonl`
- `trades.jsonl`

改动：
- `scripts/watch_loop.py`（接入 sanity + alert_router + filelock 读 holdings）
- `holdings.yaml`（schema 增字段，向后兼容）
- `.claude/skills/stock-premarket/SKILL.md`
- `.claude/skills/stock-intraday/SKILL.md`
- `.claude/skills/stock-anomaly/SKILL.md`
- `.claude/skills/stock-postmarket/SKILL.md`
- `pyproject.toml`（如需新增 filelock 依赖）
- `README.md`（说明 bot 启动）

## 10. 推出顺序

建议分三批合入，每批可独立跑：

1. **数据 + 节假日 + holdings 状态机**（calendar.py / holdings.py / schema 迁移）——无外部行为变化，奠定基础。
2. **watch_loop 改造**（sanity.py + alert_router）——告警立即可见 T+1 化效果。
3. **Telegram bot 入站 + 4 个 skill 文案改写**——闭环完成。

每批合入后跑 1-2 个交易日观察，发现问题再进下一批。

## 11. 验证清单

- [ ] 节假日表覆盖未来 90 天，长假场景（如国庆）unlock_date 算对。
- [ ] holdings.yaml 并发写不丢数据（bot 与 watch_loop 同时写测试）。
- [ ] 开头案例（000601 10:05 买点 + 10:15 破止损）现在产出双轨告警，明早预案合理。
- [ ] sanity check 5 条规则各有一个真实历史样本通过验证。
- [ ] 派别 B 加严过滤命中率合理（盘后复盘汇总能看到数字）。
- [ ] bot 白名单校验，非白 chat_id 不响应。
- [ ] /sell 违规 T+1 时被拒单。
- [ ] 加仓加权均价计算正确（手工算一例对比）。
- [ ] pending_signals 与 trades 能 join 出"系统触发 vs 自主追高"对比。

## 12. 已知限制

- 节假日表依赖 akshare 接口稳定，接口变更需修 `refresh_calendar.py`。
- 同 code 加仓 unlock_date 取最新一笔最保守但偏严（实际上老仓位部分已解锁），首期接受这个保守度；如后续要分批 unlock 再做。
- bot 单聊只支持一个用户（白名单可加，但 holdings.yaml 全局共享），多账户场景不支持。
