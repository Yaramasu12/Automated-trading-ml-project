-- deploy/db/init-timescale.sql — TimescaleDB hypertables (REDESIGN_PROMPT.md §7, §3.1)
-- Must run AFTER init.sql (which creates the trading schema).

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ──────────────────────────────────────────────
-- TimescaleDB Hypertables for time-series data
-- ──────────────────────────────────────────────

-- Ticks hypertable
CREATE TABLE IF NOT EXISTS ticks (
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT now(),
    instrument_id  TEXT NOT NULL,
    segment        TEXT NOT NULL,
    last_price     DOUBLE PRECISION NOT NULL,
    bid            DOUBLE PRECISION,
    ask            DOUBLE PRECISION,
    bid_qty        BIGINT,
    ask_qty        BIGINT,
    oi             BIGINT,
    depth          JSONB DEFAULT '{}',
    source         TEXT NOT NULL DEFAULT 'angel_one',
    raw_tick       JSONB DEFAULT '{}',
    PRIMARY KEY (timestamp, instrument_id)
) PARTITION BY RANGE (timestamp);

SELECT create_hypertable('ticks', 'timestamp', if_not_exists => TRUE);
CREATE INDEX ON ticks(instrument_id, timestamp DESC);
CREATE INDEX ON ticks(segment, timestamp DESC);

-- Retention policy: keep ticks 30 days
SELECT create_retention_policy('ticks',
    hypertable => 'ticks',
    retention_period => '30 days',
    retention_period_source => 'creation_time',
    if_exists => TRUE);

-- ──────────────────────────────────────────────
-- 1-minute bars (continuous aggregates)
-- ──────────────────────────────────────────────

CREATE MATERIALIZED VIEW IF NOT EXISTS bars_1m
(
    time          TIMESTAMPTZ,
    instrument_id TEXT,
    segment       TEXT,
    open          DOUBLE PRECISION,
    high          DOUBLE PRECISION,
    low           DOUBLE PRECISION,
    close         DOUBLE PRECISION,
    volume        BIGINT,
    oi            BIGINT,
    tick_count    BIGINT,
    avg_price     DOUBLE PRECISION,
    vwap          DOUBLE PRECISION
)
FROM ticks
GROUP BY time, instrument_id, segment;

SELECT create_hypertable('bars_1m', 'time', if_not_exists => TRUE);
SELECT create_continuous_aggregate('bars_1m', 'time', 'instrument_id',
    'segment',
    view_window_size => INTERVAL '7 days',
    refresh_window_start => INTERVAL '1 day',
    if_not_exists => TRUE);

-- 5m / 15m / 1h continuous aggregates
CREATE MATERIALIZED VIEW IF NOT EXISTS bars_5m
(
    time          TIMESTAMPTZ,
    instrument_id TEXT,
    segment       TEXT,
    open          DOUBLE PRECISION,
    high          DOUBLE PRECISION,
    low           DOUBLE PRECISION,
    close         DOUBLE PRECISION,
    volume        BIGINT,
    oi            BIGINT,
    tick_count    BIGINT,
    avg_price     DOUBLE PRECISION,
    vwap          DOUBLE PRECISION
)
FROM ticks
GROUP BY time, instrument_id, segment;

SELECT create_hypertable('bars_5m', 'time', if_not_exists => TRUE);
SELECT create_continuous_aggregate('bars_5m', 'time', 'instrument_id',
    'segment',
    view_window_size => INTERVAL '14 days',
    refresh_window_start => INTERVAL '2 days',
    if_not_exists => TRUE);

CREATE MATERIALIZED VIEW IF NOT EXISTS bars_15m
(
    time          TIMESTAMPTZ,
    instrument_id TEXT,
    segment       TEXT,
    open          DOUBLE PRECISION,
    high          DOUBLE PRECISION,
    low           DOUBLE PRECISION,
    close         DOUBLE PRECISION,
    volume        BIGINT,
    oi            BIGINT,
    tick_count    BIGINT,
    avg_price     DOUBLE PRECISION,
    vwap          DOUBLE PRECISION
)
FROM ticks
GROUP BY time, instrument_id, segment;

SELECT create_hypertable('bars_15m', 'time', if_not_exists => TRUE);
SELECT create_continuous_aggregate('bars_15m', 'time', 'instrument_id',
    'segment',
    view_window_size => INTERVAL '30 days',
    refresh_window_start => INTERVAL '3 days',
    if_not_exists => TRUE);

CREATE MATERIALIZED VIEW IF NOT EXISTS bars_1h
(
    time          TIMESTAMPTZ,
    instrument_id TEXT,
    segment       TEXT,
    open          DOUBLE PRECISION,
    high          DOUBLE PRECISION,
    low           DOUBLE PRECISION,
    close         DOUBLE PRECISION,
    volume        BIGINT,
    oi            BIGINT,
    tick_count    BIGINT,
    avg_price     DOUBLE PRECISION,
    vwap          DOUBLE PRECISION
)
FROM ticks
GROUP BY time, instrument_id, segment;

SELECT create_hypertable('bars_1h', 'time', if_not_exists => TRUE);
SELECT create_continuous_aggregate('bars_1h', 'time', 'instrument_id',
    'segment',
    view_window_size => INTERVAL '90 days',
    refresh_window_start => INTERVAL '7 days',
    if_not_exists => TRUE);

-- ──────────────────────────────────────────────
-- Option chain snapshots
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS option_chain_snapshots (
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT now(),
    underlying      TEXT NOT NULL,
    spot_price      DOUBLE PRECISION NOT NULL,
    atm_iv          DOUBLE PRECISION,
    iv_rank         DOUBLE PRECISION,
    iv_percentile   DOUBLE PRECISION,
    pcr             DOUBLE PRECISION,
    max_pain        DOUBLE PRECISION,
    chain_data      JSONB NOT NULL DEFAULT '[]',
    source          TEXT NOT NULL DEFAULT 'angel_one',
    PRIMARY KEY (timestamp, underlying)
);

SELECT create_hypertable('option_chain_snapshots', 'timestamp',
    if_not_exists => TRUE);
CREATE INDEX ON option_chain_snapshots(underlying, timestamp DESC);

-- ──────────────────────────────────────────────
-- Greeks time series
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS greeks_ts (
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT now(),
    instrument_id   TEXT NOT NULL,
    underlying      TEXT NOT NULL,
    net_delta       DOUBLE PRECISION,
    net_gamma       DOUBLE PRECISION,
    net_vega        DOUBLE PRECISION,
    net_theta       DOUBLE PRECISION,
    gross_gamma     DOUBLE PRECISION,
    total_oi        BIGINT,
    total_volume    BIGINT,
    data            JSONB DEFAULT '{}',
    PRIMARY KEY (timestamp, instrument_id)
);

SELECT create_hypertable('greeks_ts', 'timestamp',
    if_not_exists => TRUE);
CREATE INDEX ON greeks_ts(underlying, timestamp DESC);

-- ──────────────────────────────────────────────
-- Features hypertable (Feast online + offline)
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS features (
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT now(),
    instrument_id   TEXT NOT NULL,
    feature_view    TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    feature_vector  JSONB NOT NULL DEFAULT '{}',
    entity_keys     JSONB DEFAULT '{}',
    source          TEXT NOT NULL DEFAULT 'pipeline',
    lineage         JSONB DEFAULT '{}',
    PRIMARY KEY (timestamp, instrument_id, feature_view, feature_version)
);

SELECT create_hypertable('features', 'timestamp',
    if_not_exists => TRUE);
CREATE INDEX ON features(instrument_id, feature_view, timestamp DESC);

-- ──────────────────────────────────────────────
-- Feature retention (30 days for time-partitioned)
-- ──────────────────────────────────────────────

SELECT create_retention_policy('option_chain_snapshots',
    hypertable => 'option_chain_snapshots',
    retention_period => '90 days',
    if_exists => TRUE);

SELECT create_retention_policy('greeks_ts',
    hypertable => 'greeks_ts',
    retention_period => '180 days',
    if_exists => TRUE);

SELECT create_retention_policy('features',
    hypertable => 'features',
    retention_period => '365 days',
    if_exists => TRUE);

-- ──────────────────────────────────────────────
-- Phase 2 market-data store (ADDITIVE; see persistence.py _PG_MARKET_DDL)
--
-- A unified, DB-backed EOD store so validation/retraining read daily bars,
-- option quotes and modeled index IV from one place instead of scattered
-- CSVs and live Angel One pulls. Purely additive: nothing above (ticks,
-- option_chain_snapshots, the trading-schema tables incl. index_vol_premium)
-- is altered or dropped, and the live short-vol order path is untouched.
-- Each statement is independent (autocommit per statement convention), so an
-- IF-NOT-EXISTS re-run / a missing extension never poisons the rest. NO
-- retention policy: this is the durable historical record, not a rolling
-- tick buffer. Modeled IV is kept in its own labeled column, never conflated
-- with the raw traded close/settle.
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS instrument_master (
    instrument_id   TEXT        PRIMARY KEY,
    exchange        TEXT        NOT NULL,
    segment         TEXT        NOT NULL,
    underlying      TEXT,
    instrument_type TEXT        NOT NULL,
    tckr_symbol     TEXT        NOT NULL,
    expiry          DATE,
    strike          DOUBLE PRECISION,
    option_type     TEXT,
    lot_size        INTEGER,
    tick_size       DOUBLE PRECISION,
    first_seen      DATE,
    last_seen       DATE,
    UNIQUE (exchange, segment, tckr_symbol, expiry, strike, option_type)
);
CREATE INDEX IF NOT EXISTS idx_instr_master_lookup ON instrument_master (tckr_symbol, segment);

CREATE TABLE IF NOT EXISTS eod_ohlcv (
    trade_date    DATE        NOT NULL,
    instrument_id TEXT        NOT NULL,
    open          DOUBLE PRECISION,
    high          DOUBLE PRECISION,
    low           DOUBLE PRECISION,
    close         DOUBLE PRECISION,
    settle        DOUBLE PRECISION,
    volume        DOUBLE PRECISION,
    oi            DOUBLE PRECISION,
    delivery_qty  DOUBLE PRECISION,
    vwap          DOUBLE PRECISION,
    source        TEXT,
    is_adjusted   BOOLEAN     NOT NULL DEFAULT FALSE,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, instrument_id)
);
SELECT create_hypertable('eod_ohlcv', 'trade_date',
    chunk_time_interval => INTERVAL '1 month', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_eod_ohlcv_instr ON eod_ohlcv (instrument_id, trade_date DESC);

CREATE TABLE IF NOT EXISTS eod_option_quote (
    trade_date    DATE        NOT NULL,
    instrument_id TEXT        NOT NULL,
    underlying    TEXT        NOT NULL,
    expiry        DATE,
    strike        DOUBLE PRECISION,
    option_type   TEXT,
    open          DOUBLE PRECISION,
    high          DOUBLE PRECISION,
    low           DOUBLE PRECISION,
    close         DOUBLE PRECISION,
    settle        DOUBLE PRECISION,
    volume        DOUBLE PRECISION,
    oi            DOUBLE PRECISION,
    change_oi     DOUBLE PRECISION,
    underlying_px DOUBLE PRECISION,
    implied_vol   DOUBLE PRECISION,
    iv_method     TEXT,
    source        TEXT,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, instrument_id)
);
SELECT create_hypertable('eod_option_quote', 'trade_date',
    chunk_time_interval => INTERVAL '1 month', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_eod_optq_underlying ON eod_option_quote (underlying, expiry, trade_date DESC);

CREATE TABLE IF NOT EXISTS index_atm_iv30 (
    trade_date   DATE        NOT NULL,
    index_symbol TEXT        NOT NULL,
    spot_parity  DOUBLE PRECISION,
    spot_close   DOUBLE PRECISION,
    atm_iv30     DOUBLE PRECISION,
    method       TEXT        NOT NULL DEFAULT 'bhavcopy_b76_totvar_interp',
    source       TEXT,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, index_symbol)
);
CREATE INDEX IF NOT EXISTS idx_index_iv30_sym ON index_atm_iv30 (index_symbol, trade_date DESC);

CREATE TABLE IF NOT EXISTS index_spot (
    trade_date   DATE        NOT NULL,
    index_symbol TEXT        NOT NULL,
    open         DOUBLE PRECISION,
    high         DOUBLE PRECISION,
    low          DOUBLE PRECISION,
    close        DOUBLE PRECISION,
    source       TEXT,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, index_symbol)
);
SELECT create_hypertable('index_spot', 'trade_date',
    chunk_time_interval => INTERVAL '1 month', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_index_spot_sym ON index_spot (index_symbol, trade_date DESC);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id               BIGSERIAL   PRIMARY KEY,
    source           TEXT        NOT NULL,
    trade_date       DATE        NOT NULL,
    state            TEXT        NOT NULL,
    lease_owner      TEXT,
    lease_expires_at TIMESTAMPTZ,
    rows             INTEGER,
    detail           TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, trade_date)
);

-- ──────────────────────────────────────────────
-- Phase 3 corporate actions (ADDITIVE; see persistence.py corporate_actions
-- DDL). Point-in-time back-adjustment source for cash OHLCV. Raw eod_ohlcv is
-- NEVER mutated: MarketDataStore.adjusted_closes() applies a cumulative factor
-- built from ONLY the actions with ex_date <= the query's as_of date, so a
-- backtest "as of D" never sees a split that had not happened by D. ratio is
-- num:den = resulting shares per prior shares (2:1 split -> num=2,den=1; price
-- multiplier on ex-date = den/num). No retention policy: durable record.
-- ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS corporate_actions (
    id            BIGSERIAL   PRIMARY KEY,
    symbol        TEXT        NOT NULL,
    ex_date       DATE        NOT NULL,
    action_type   TEXT        NOT NULL,
    ratio_num     DOUBLE PRECISION,
    ratio_den     DOUBLE PRECISION,
    raw_text      TEXT,
    source        TEXT,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (symbol, ex_date, action_type, raw_text)
);
CREATE INDEX IF NOT EXISTS idx_corp_actions_sym ON corporate_actions (symbol, ex_date);