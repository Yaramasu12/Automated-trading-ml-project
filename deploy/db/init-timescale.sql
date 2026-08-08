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