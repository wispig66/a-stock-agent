-- daily.db schema for stock 短线 CC 辅助系统
-- 用法：sqlite3 data/daily.db < code/init_db.sql

-- 启用 WAL（持久化 PRAGMA，写进 DB 文件头一次永久生效）
-- 写不阻塞读、锁持续短，避免 daemon 与 skill 并发写互相打架
PRAGMA journal_mode = WAL;

-- 注意：synchronous 和 busy_timeout 是按连接独立的，**不会**持久化到 DB 文件。
-- Python 端通过 code/db.py 的 connect() 工厂每次连接都设，不要依赖这里。

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
