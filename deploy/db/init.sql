-- deploy/db/init.sql — Core OLTP schema (REDESIGN_PROMPT.md §7)
-- PostgreSQL tables for orders, trades, audit, strategies, risk, backtests.
-- TimescaleDB hypertables are in init-timescale.sql.

-- ──────────────────────────────────────────────
-- Schema: trading (created if not exists)
-- ──────────────────────────────────────────────

CREATE SCHEMA IF NOT EXISTS trading;
SET search_path = trading, public;

-- ──────────────────────────────────────────────
-- Instruments
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS instruments (
    instrument_id   TEXT PRIMARY KEY,
    symbol          TEXT NOT NULL,
    segment         TEXT NOT NULL,       -- NIFTY, BANKNIFTY, COMMODITY, etc.
    exchange        TEXT NOT NULL DEFAULT 'NSE',
    currency        TEXT NOT NULL DEFAULT 'INR',
    instrument_type TEXT NOT NULL,       -- FUT, OPT, STOCK, COMMODITY
    expiry          TIMESTAMP,
    strike          DOUBLE PRECISION,
    tick_size       DOUBLE PRECISION NOT NULL,
    lot_size        INTEGER NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_instruments_segment ON instruments(segment);
CREATE INDEX IF NOT EXISTS idx_instruments_active ON instruments(is_active) WHERE is_active;

-- ──────────────────────────────────────────────
-- Strategies
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS strategies (
    strategy_id     TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    version         TEXT NOT NULL,
    type            TEXT NOT NULL,       -- SHORT_VOL, SWING, INTRADAY, etc.
    config          JSONB NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'draft',  -- draft | paper | live | retired
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ──────────────────────────────────────────────
-- Signals
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS signals (
    signal_id       TEXT PRIMARY KEY,
    strategy_id     TEXT NOT NULL REFERENCES strategies(strategy_id),
    instrument_id   TEXT NOT NULL,
    direction       TEXT NOT NULL,       -- LONG, SHORT, FLAT
    structure       TEXT,                -- IRON_CONDOR, PUT_SPREAD, etc.
    conviction      DOUBLE PRECISION,
    features        JSONB NOT NULL DEFAULT '{}',
    ttl_seconds     INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy_id);
CREATE INDEX IF NOT EXISTS idx_signals_instrument ON signals(instrument_id);

-- ──────────────────────────────────────────────
-- Orders (OMS)
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT 'single',
    strategy_id     TEXT NOT NULL REFERENCES strategies(strategy_id),
    signal_id       TEXT REFERENCES signals(signal_id),
    instrument_id   TEXT NOT NULL,
    direction       TEXT NOT NULL,
    structure       TEXT,
    quantity        INTEGER NOT NULL,
    order_type      TEXT NOT NULL,       -- MARKET, LIMIT, STOP, STOP_LIMIT
    price           DOUBLE PRECISION,
    status          TEXT NOT NULL,       -- PENDING, SUBMITTED, PARTIAL, FILLED, REJECTED, CANCELLED
    algo_id         TEXT,               -- exchange-issued algo ID (SEBI compliance)
    broker_order_id TEXT,
    submitted_at    TIMESTAMPTZ,
    filled_at       TIMESTAMPTZ,
    rejected_at     TIMESTAMPTZ,
    last_update     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_orders_tenant ON orders(tenant_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_broker ON orders(broker_order_id);

-- ──────────────────────────────────────────────
-- Order events (immutable audit trail)
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS order_events (
    event_id        BIGSERIAL,
    order_id        TEXT NOT NULL REFERENCES orders(order_id),
    tenant_id       TEXT NOT NULL DEFAULT 'single',
    strategy_id     TEXT,
    signal_hash     TEXT,
    algo_id         TEXT,
    event_type      TEXT NOT NULL,     -- CREATE, UPDATE, FILL, REJECT, CANCEL, PARTIAL
    event_data      JSONB NOT NULL DEFAULT '{}',
    risk_checks     JSONB DEFAULT '[]',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(order_id);
CREATE INDEX IF NOT EXISTS idx_order_events_tenant ON order_events(tenant_id);

-- ──────────────────────────────────────────────
-- Trades (attribution)
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS trades (
    trade_id        TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT 'single',
    strategy_id     TEXT NOT NULL REFERENCES strategies(strategy_id),
    signal_id       TEXT,
    instrument_id   TEXT NOT NULL,
    direction       TEXT NOT NULL,
    structure       TEXT,
    entry_price     DOUBLE PRECISION NOT NULL,
    exit_price      DOUBLE PRECISION,
    quantity        INTEGER NOT NULL,
    entry_time      TIMESTAMPTZ NOT NULL,
    exit_time       TIMESTAMPTZ,
    pnl             DOUBLE PRECISION,
    gross_pnl       DOUBLE PRECISION,
    brokerage       DOUBLE PRECISION NOT NULL DEFAULT 0,
    stt             DOUBLE PRECISION NOT NULL DEFAULT 0,
    exchange_txn    DOUBLE PRECISION NOT NULL DEFAULT 0,
    gst             DOUBLE PRECISION NOT NULL DEFAULT 0,
    stamp           DOUBLE PRECISION NOT NULL DEFAULT 0,
    sebi            DOUBLE PRECISION NOT NULL DEFAULT 0,
    slippage_bps    DOUBLE PRECISION NOT NULL DEFAULT 0,
    regime          TEXT,
    conviction      DOUBLE PRECISION,
    agent_votes     JSONB DEFAULT '[]',
    slippage_est    DOUBLE PRECISION NOT NULL DEFAULT 0,
    cost_total      DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trades_tenant ON trades(tenant_id);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy_id);
CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time);

-- ──────────────────────────────────────────────
-- Positions snapshot (periodic)
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS positions_snapshot (
    snapshot_id     BIGSERIAL,
    tenant_id       TEXT NOT NULL DEFAULT 'single',
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT now(),
    data            JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_positions_snapshot_tenant ON positions_snapshot(tenant_id);
CREATE INDEX IF NOT EXISTS idx_positions_snapshot_ts ON positions_snapshot(tenant_id, timestamp);

-- ──────────────────────────────────────────────
-- Risk limits & events
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS risk_limits (
    limit_id        TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT 'single',
    name            TEXT NOT NULL,
    value           DOUBLE PRECISION NOT NULL,
    unit            TEXT,              -- PERCENT, ABSOLUTE, COUNT
    strategy_id     TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS risk_events (
    event_id        BIGSERIAL,
    tenant_id       TEXT NOT NULL DEFAULT 'single',
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT now(),
    limit_id        TEXT,
    event_type      TEXT NOT NULL,     -- DRAWDOWN, DAILY_LOSS, MARGIN, KILL_SWITCH, OPTION_BAN
    severity        TEXT NOT NULL,     -- WARN, ALERT, CRITICAL
    message         TEXT NOT NULL,
    data            JSONB DEFAULT '{}',
    action_taken    TEXT
);

CREATE INDEX IF NOT EXISTS idx_risk_events_tenant ON risk_events(tenant_id);
CREATE INDEX IF NOT EXISTS idx_risk_events_severity ON risk_events(severity);

-- ──────────────────────────────────────────────
-- Backtests
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS backtests (
    backtest_id     TEXT PRIMARY KEY,
    strategy_id     TEXT NOT NULL REFERENCES strategies(strategy_id),
    config          JSONB NOT NULL DEFAULT '{}',
    data_snapshot   JSONB DEFAULT '{}',
    git_sha         TEXT,
    start_date      DATE,
    end_date        DATE,
    gross_pnl       DOUBLE PRECISION,
    net_pnl         DOUBLE PRECISION,
    gross_sharpe    DOUBLE PRECISION,
    net_sharpe      DOUBLE PRECISION,
    max_drawdown    DOUBLE PRECISION,
    win_rate        DOUBLE PRECISION,
    total_trades    INTEGER,
    gate_results    JSONB DEFAULT '{}',  -- CPCV, DSR, PBO, MC results
    status          TEXT NOT NULL DEFAULT 'running',  -- running | passed | failed | promoted
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_backtests_strategy ON backtests(strategy_id);
CREATE INDEX IF NOT EXISTS idx_backtests_status ON backtests(status);

-- ──────────────────────────────────────────────
-- Promotions
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS promotions (
    promotion_id    TEXT PRIMARY KEY,
    strategy_id     TEXT NOT NULL REFERENCES strategies(strategy_id),
    from_stage      TEXT NOT NULL,
    to_stage        TEXT NOT NULL,
    gate_results    JSONB NOT NULL DEFAULT '{}',
    approved_by     TEXT NOT NULL DEFAULT 'system',
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ──────────────────────────────────────────────
-- Event calendar
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS event_calendar (
    event_id        TEXT PRIMARY KEY,
    underlying      TEXT NOT NULL,
    event_type      TEXT NOT NULL,     -- RBI, BUDGET, ELECTION, EXPIRY, DIVIDEND
    start_time      TIMESTAMPTZ NOT NULL,
    end_time        TIMESTAMPTZ NOT NULL,
    impact          TEXT,              -- HIGH, MEDIUM, LOW
    description     TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ──────────────────────────────────────────────
-- Agent decisions
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS agent_decisions (
    decision_id     TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT 'single',
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_name      TEXT NOT NULL,
    decision_type   TEXT NOT NULL,     -- VETO, APPROVE, DOWNSIZE, ALERT
    target          TEXT,              -- signal_id, order_id, etc.
    input_context   JSONB DEFAULT '{}',
    output          JSONB NOT NULL DEFAULT '{}',
    prompt_hash     TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    latency_ms      DOUBLE PRECISION,
    source_chunks   JSONB DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_agent_decisions_tenant ON agent_decisions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_agent_decisions_agent ON agent_decisions(agent_name);

-- ──────────────────────────────────────────────
-- Daily P&L
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS daily_pnl (
    id              BIGSERIAL,
    tenant_id       TEXT NOT NULL DEFAULT 'single',
    date            DATE NOT NULL,
    strategy_id     TEXT,
    realized_pnl    DOUBLE PRECISION NOT NULL DEFAULT 0,
    unrealized_pnl  DOUBLE PRECISION NOT NULL DEFAULT 0,
    gross_pnl       DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_costs     DOUBLE PRECISION NOT NULL DEFAULT 0,
    net_pnl         DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_trades    INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, date, strategy_id)
);

-- ──────────────────────────────────────────────
-- Tenant (multi-user)
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    email           TEXT,
    broker          TEXT,               -- angel_one, dhan, upstox, zerodha
    status          TEXT NOT NULL DEFAULT 'active',  -- active | suspended | paper_only
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ──────────────────────────────────────────────
-- Row-Level Security (for multi-tenant)
-- ──────────────────────────────────────────────

-- Enable RLS on tenant-scoped tables
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE order_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE trades ENABLE ROW LEVEL SECURITY;
ALTER TABLE positions_snapshot ENABLE ROW LEVEL SECURITY;
ALTER TABLE risk_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE daily_pnl ENABLE ROW LEVEL SECURITY;

-- RLS policies
CREATE POLICY tenant_isolation_orders ON orders
    USING (tenant_id = current_setting('app.current_tenant', true));
CREATE POLICY tenant_isolation_order_events ON order_events
    USING (tenant_id = current_setting('app.current_tenant', true));
CREATE POLICY tenant_isolation_trades ON trades
    USING (tenant_id = current_setting('app.current_tenant', true));
CREATE POLICY tenant_isolation_positions ON positions_snapshot
    USING (tenant_id = current_setting('app.current_tenant', true));
CREATE POLICY tenant_isolation_risk ON risk_events
    USING (tenant_id = current_setting('app.current_tenant', true));
CREATE POLICY tenant_isolation_agents ON agent_decisions
    USING (tenant_id = current_setting('app.current_tenant', true));
CREATE POLICY tenant_isolation_pnl ON daily_pnl
    USING (tenant_id = current_setting('app.current_tenant', true));