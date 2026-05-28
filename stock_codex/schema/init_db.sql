-- daily.db schema for stock 短线 Codex 辅助系统
-- 用法：sqlite3 data/daily.db < stock_codex/schema/init_db.sql

-- 启用 WAL（持久化 PRAGMA，写进 DB 文件头一次永久生效）
-- 写不阻塞读、锁持续短，避免 daemon 与 skill 并发写互相打架
PRAGMA journal_mode = WAL;

-- 注意：synchronous 和 busy_timeout 是按连接独立的，**不会**持久化到 DB 文件。
-- Python 端通过 stock_codex/infra/db.py 的 connect() 工厂每次连接都设，不要依赖这里。

CREATE TABLE IF NOT EXISTS daily_kline (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    vol REAL,
    amount REAL,
    pct_chg REAL,
    PRIMARY KEY (code, date)
);

CREATE INDEX IF NOT EXISTS idx_kline_date ON daily_kline(date);

CREATE TABLE IF NOT EXISTS limit_up (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    limit_up_num INTEGER,
    seal_amount REAL,
    turnover_rate REAL,
    first_seal_time TEXT,
    open_count INTEGER,
    concept TEXT,
    PRIMARY KEY (date, code)
);

CREATE TABLE IF NOT EXISTS lhb (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    seat_name TEXT,
    buy_amount REAL,
    sell_amount REAL,
    net_amount REAL,
    rank INTEGER
);

CREATE INDEX IF NOT EXISTS idx_lhb_date_code ON lhb(date, code);

CREATE TABLE IF NOT EXISTS sentiment_daily (
    date TEXT PRIMARY KEY,
    limit_up_count INTEGER,
    limit_down_count INTEGER,
    max_consec INTEGER,
    promotion_rate REAL,
    second_promotion_rate REAL,
    blast_rate REAL,
    money_effect REAL,
    loss_effect INTEGER,
    phase TEXT
);

-- 推送日志：每次 Telegram 推送都入库，用于后续分析与持续改进
CREATE TABLE IF NOT EXISTS push_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,        -- ISO 8601
    source TEXT,                    -- 'stock-premarket' / 'stock-postmarket' / 'manual' 等
    chat_id TEXT,
    msg_id INTEGER,                 -- Telegram message_id（多段时取第一段）
    text TEXT NOT NULL,             -- 原始消息内容（完整）
    chunks INTEGER DEFAULT 1,       -- 分段数
    success INTEGER DEFAULT 1,      -- 1=成功 0=失败
    error TEXT                      -- 失败时的错误信息
);

CREATE INDEX IF NOT EXISTS idx_push_log_ts ON push_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_push_log_source ON push_log(source);

-- 跨 IM 出站日志：新 channels/gateway 主表。push_log 继续兼容写入，供旧复盘逻辑读取。
CREATE TABLE IF NOT EXISTS channel_outbound_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    channel TEXT NOT NULL,
    account_id TEXT,
    conversation_id TEXT NOT NULL,
    thread_id TEXT,
    provider_msg_id TEXT,
    source TEXT,
    text TEXT NOT NULL,
    format TEXT DEFAULT 'plain',
    chunks INTEGER DEFAULT 1,
    success INTEGER DEFAULT 1,
    error TEXT,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_channel_outbound_ts ON channel_outbound_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_channel_outbound_channel ON channel_outbound_log(channel, conversation_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_channel_outbound_source ON channel_outbound_log(source);

-- 跨 IM 入站日志：后续替代 tg_inbound。Telegram v1 会 dual-write 兼容 tg_inbound。
CREATE TABLE IF NOT EXISTS channel_inbound_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    channel TEXT NOT NULL,
    account_id TEXT,
    conversation_id TEXT NOT NULL,
    thread_id TEXT,
    sender_id TEXT,
    provider_msg_id TEXT NOT NULL,
    provider_event_id TEXT,
    dedupe_key TEXT UNIQUE,
    raw_text TEXT NOT NULL,
    parsed_command TEXT,
    parsed_intent TEXT,
    parsed_payload TEXT,
    response_channel TEXT,
    response_msg_id TEXT,
    handler_status TEXT,
    handler_error TEXT,
    duration_ms INTEGER,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_channel_inbound_ts ON channel_inbound_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_channel_inbound_channel ON channel_inbound_log(channel, conversation_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_channel_inbound_command ON channel_inbound_log(parsed_command);

-- 用户实盘交易流水：TG /buy /sell 命令落库，用于复盘进出场决策
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                -- ISO 8601 成交时间（允许 @HH:MM 指定当日分钟）
    code TEXT NOT NULL,              -- 6 位股票代码
    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
    price REAL NOT NULL,             -- 成交价
    qty INTEGER NOT NULL,            -- 股数（已 = 手数 × 100）
    reason TEXT,                     -- 8 个枚举之一
    source_msg_id INTEGER,           -- TG reply 关联的 push_log.msg_id
    note TEXT,                       -- 自由文本（可选）
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trades_code_ts ON trades(code, ts);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);

-- TG 入向消息全量审计：每条用户命令落一条，handler 完成后回填状态
CREATE TABLE IF NOT EXISTS tg_inbound (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,             -- ISO 8601 收到时间
    update_id INTEGER UNIQUE,            -- TG update_id 去重
    chat_id TEXT NOT NULL,
    user_msg_id INTEGER NOT NULL,        -- TG message_id（用户那条）
    raw_text TEXT NOT NULL,              -- 原始命令文本
    parsed_command TEXT,                 -- '/ask' '/ask+' '/buy' 'free_text' 等
    parsed_intent TEXT,                  -- /ask 类：sector/stock/event/ambiguous
    parsed_payload TEXT,                 -- JSON: 提取的 sector/code/event_text
    response_msg_id INTEGER,             -- 关联 push_log.msg_id
    handler_status TEXT,                 -- ok/rejected/timeout/error
    handler_error TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tg_inbound_ts ON tg_inbound(timestamp);
CREATE INDEX IF NOT EXISTS idx_tg_inbound_chat ON tg_inbound(chat_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_tg_inbound_command ON tg_inbound(parsed_command);

-- 同花顺热点强势股 + 题材归因（盘后 15:30+ 才有当日数据；盘前跑用 D-1）
CREATE TABLE IF NOT EXISTS ths_hot_reason (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    close REAL,
    change_pct REAL,
    turnover_pct REAL,
    amount REAL,
    big_net REAL,
    reason TEXT,
    PRIMARY KEY (date, code)
);

CREATE INDEX IF NOT EXISTS idx_ths_hot_date ON ths_hot_reason(date);

-- 个股日级资金流（百度股市通）
CREATE TABLE IF NOT EXISTS fund_flow_daily (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    close REAL,
    change_pct REAL,
    super_net REAL,
    large_net REAL,
    medium_net REAL,
    small_net REAL,
    main_in REAL,
    PRIMARY KEY (date, code)
);

CREATE INDEX IF NOT EXISTS idx_fund_flow_date ON fund_flow_daily(date);

-- 限售解禁日历缓存
CREATE TABLE IF NOT EXISTS unlock_calendar (
    code TEXT NOT NULL,
    unlock_date TEXT NOT NULL,
    type TEXT,
    shares REAL,
    float_ratio REAL,
    fetched_at TEXT,
    PRIMARY KEY (code, unlock_date)
);

CREATE INDEX IF NOT EXISTS idx_unlock_date ON unlock_calendar(unlock_date);

-- 股票基础信息（代码→名称/板块/上市日/ST 标志），refresh_stock_basic.py 每日刷新
CREATE TABLE IF NOT EXISTS stock_basic (
    code TEXT PRIMARY KEY,
    name TEXT,
    board TEXT,           -- main / chinext / star / bse
    list_date TEXT,
    is_st INTEGER DEFAULT 0,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_stock_basic_board ON stock_basic(board);


-- ──────────────────────────────────────────────────────────────
-- theme_emergence_loop（Layer 1：盘中新主线浮现识别）
-- ──────────────────────────────────────────────────────────────

-- 主线浮现审计日志（每次 T1/T2 触发都写一条）
CREATE TABLE IF NOT EXISTS theme_emergence_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at     TEXT NOT NULL,         -- ISO 8601 触发时间
    trade_date      TEXT NOT NULL,         -- YYYY-MM-DD
    concept_tag     TEXT NOT NULL,         -- 一级题材名
    signal_level    TEXT NOT NULL,         -- T1 / T2
    signals_hit     TEXT NOT NULL,         -- JSON {"PH":true,"cluster3":true,...}
    cluster_count   INTEGER,               -- 30min 滑窗内同题材涨停家数
    first_leader    TEXT,                  -- 首封龙头 code
    first_seal_time TEXT,                  -- HH:MM:SS
    ph_value        REAL,                  -- 触发时 PH 数值
    notes           TEXT,
    push_msg_id     INTEGER                -- 关联 push_log.id
);
CREATE INDEX IF NOT EXISTS idx_tel_date_tag ON theme_emergence_log(trade_date, concept_tag);

-- 动态观察池（Layer 2 intraday/postmarket 读这张）
CREATE TABLE IF NOT EXISTS watchlist_dynamic (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    concept_tag     TEXT NOT NULL,
    code            TEXT NOT NULL,
    name            TEXT NOT NULL,
    role            TEXT NOT NULL,         -- leader / follower
    entry_price     REAL,
    stop_price      REAL,
    target_pct      REAL,
    discipline_type TEXT NOT NULL,         -- A / B / D
    action_window   TEXT NOT NULL,         -- before_1030 / 1030_1400 / after_1400
    status          TEXT DEFAULT 'pending',-- pending/triggered/expired/skipped
    source_emergence_id INTEGER,
    UNIQUE(trade_date, code, concept_tag)
);
CREATE INDEX IF NOT EXISTS idx_wld_date ON watchlist_dynamic(trade_date);
CREATE INDEX IF NOT EXISTS idx_wld_concept ON watchlist_dynamic(trade_date, concept_tag);

-- ──────────────────────────────────────────────────────────────
-- decision_tickets（交易决策漏斗：主攻 / 潜伏 / 备选 / 禁买）
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS decision_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    concept TEXT,
    lane TEXT NOT NULL CHECK(lane IN ('main','ambush','backup','ban')),
    faction TEXT CHECK(faction IN ('A','B','C','D','E')),
    action TEXT NOT NULL DEFAULT 'wait' CHECK(action IN ('buy_if','wait','avoid','sell','empty')),
    entry_low REAL,
    entry_high REAL,
    max_chase_price REAL,
    stop_price REAL,
    invalid_price REAL,
    deadline_time TEXT,
    size_pct INTEGER,
    thesis TEXT,
    evidence_json TEXT,
    invalid_conditions_json TEXT,
    upgrade_conditions_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','triggered','bought','expired','invalid','reviewed')),
    source_msg_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(trade_date, code, lane)
);
CREATE INDEX IF NOT EXISTS idx_decision_tickets_date ON decision_tickets(trade_date);
CREATE INDEX IF NOT EXISTS idx_decision_tickets_lane ON decision_tickets(trade_date, lane);

-- 实时涨停池快照（簇集计数 + 首板时间数据源）
CREATE TABLE IF NOT EXISTS intraday_limit_up_snapshot (
    snapshot_ts     TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    code            TEXT NOT NULL,
    name            TEXT,
    limit_up_count  INTEGER,
    first_seal_time TEXT,
    open_count      INTEGER,
    seal_amount     REAL,
    concept_top1    TEXT,
    PRIMARY KEY (snapshot_ts, code)
);
CREATE INDEX IF NOT EXISTS idx_ilu_date_concept ON intraday_limit_up_snapshot(trade_date, concept_top1);

-- PH detector 当日状态快照（故障重启恢复用）
-- x_mean 必须持久化：update() 增量公式 (x - x_mean)/n 重启后用错均值会假触发
CREATE TABLE IF NOT EXISTS ph_state_snapshot (
    trade_date      TEXT NOT NULL,
    concept_tag     TEXT NOT NULL,
    last_update     TEXT NOT NULL,
    m_t             REAL,
    min_m_t         REAL,
    n_samples       INTEGER,
    x_mean          REAL DEFAULT 0,
    PRIMARY KEY (trade_date, concept_tag)
);
