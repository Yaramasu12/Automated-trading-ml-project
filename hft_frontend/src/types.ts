// ─── Runtime / State ──────────────────────────────────────────────────────────

export type ExecMode = 'BACKTEST' | 'PAPER' | 'SHADOW_LIVE' | 'LIVE' | 'LIVE_MANUAL_APPROVAL' | 'LIVE_AUTO_LIMITED'

export interface RuntimeState {
  execution_mode: ExecMode
  live_armed: boolean
  kill_switch_active: boolean
  broker: string
  angel_one_configured: boolean
  live_order_confirmation_ready: boolean
}

export interface RiskLimits {
  max_drawdown: number
  max_daily_loss: number
  max_position_pct: number
  max_margin_utilization: number
  max_orders_per_day: number
  max_order_to_trade_ratio: number
  block_naked_option_selling: boolean
}

export interface HealthResponse {
  status: string
  state: RuntimeState
  risk_limits: RiskLimits
  operational_status: string
  scheduler?: Record<string, unknown>
  event_bus?: EventBusSummary
  manual_approval?: ManualApprovalStatus
  broker_capabilities?: BrokerCapabilityStatus
  timestamp: string
}

export interface ManualApprovalRequest {
  request_id: string
  state: string
  created_at: string
  expires_at: string
  reviewed_at: string | null
  reviewer_note: string
  symbol: string
  side: string
  quantity: number
  strategy_name: string
}

export interface ManualApprovalStatus {
  pending_count: number
  approval_threshold_notional: number
  pending: ManualApprovalRequest[]
}

export interface EventBusSummary {
  event_count: number
  streams: Record<string, number>
  latest_event: { event_name: string; stream: string; published_at: string; payload: unknown } | null
  backend: string
}

export interface BrokerCapabilityStatus {
  active_broker: string
  active_capabilities: Record<string, unknown>
  brokers: Array<Record<string, unknown>>
}

// ─── Monitoring ───────────────────────────────────────────────────────────────

export interface MonitoringMetrics {
  status: 'HEALTHY' | 'DEGRADED' | 'HALTED'
  started_at: string
  uptime_seconds: number
  execution_mode: string
  live_armed: boolean
  kill_switch_active: boolean
  stale_market_data: boolean
  total_orders: number
  filled_orders: number
  rejected_orders: number
  rejection_rate: number
  average_latency_ms: number
  max_latency_ms: number
  event_count: number
}

// ─── Portfolio ────────────────────────────────────────────────────────────────

export interface PortfolioSnapshot {
  cash: number
  equity: number
  realized_pnl: number
  unrealized_pnl: number
  drawdown: number
  peak_equity: number
}

export interface TargetProgress {
  annual_target: number
  start_capital: number
  current_equity: number
  realized_pnl: number
  elapsed_days: number
  required_run_rate: number
  current_gap: number
  allocation_bias: string
  max_scaling_multiplier: number
}

// ─── Signals / Strategies ─────────────────────────────────────────────────────

export interface Candidate {
  underlying: string
  strategy_name: string
  instrument: { symbol: string; segment: string; type: string }
  signal: { side: string; confidence: number; price: number; reason: string } | null
  quantity: number
  risk_decision: { approved: boolean; reason: string; risk_score: number } | null
  reason: string
}

export interface SignalScanResult {
  mode: string
  approved_candidates: number
  rejected_candidates: number
  submitted_orders: number
  scans: Array<{
    underlying: string
    regime: string
    volatility_forecast: { model_name: string; annualized_volatility: number; daily_volatility: number }
    selected_strategies: string[]
    candidates: Candidate[]
  }>
}

export interface StrategyScore {
  strategy_name: string
  family: string
  score: number
  rank: number
  trade_count: number
  approved_orders: number
  rejected_orders: number
  metrics: {
    starting_capital: number
    ending_equity: number
    total_pnl: number
    return_pct: number
    max_drawdown: number
    trade_count: number
    win_rate: number
    profit_factor: number
    sharpe_like: number
  }
}

export interface StrategyEvaluationResult {
  start: string
  days: number
  underlyings: string[]
  leaderboard: StrategyScore[]
  best_strategy: string | null
}

// ─── Backtest ─────────────────────────────────────────────────────────────────

export interface BacktestResult {
  config: { starting_capital: number; start: string; days: number; underlyings: string[] }
  metrics: {
    starting_capital: number
    ending_equity: number
    total_pnl: number
    return_pct: number
    max_drawdown: number
    trade_count: number
    win_rate: number
    profit_factor: number
    sharpe_like: number
  }
  reports: unknown[]
}

// ─── Walk-Forward ─────────────────────────────────────────────────────────────

export interface WalkForwardResult {
  strategy_name: string
  total_days: number
  window_count: number
  mean_test_sharpe: number
  mean_test_return: number
  degradation_detected: boolean
  windows: Array<{
    window_index: number
    train_start: string
    test_start: string
    train_metrics: { return_pct: number; sharpe_like: number; max_drawdown: number }
    test_metrics: { return_pct: number; sharpe_like: number; max_drawdown: number }
  }>
}

// ─── Models ───────────────────────────────────────────────────────────────────

export interface VolatilityForecast {
  model_name: string
  annualized_volatility: number
  daily_volatility: number
  lower_95: number
  upper_95: number
  sample_size: number
}

export interface ModelCatalogEntry {
  name: string
  family: string
  status: string
  promotion_rule: string
}

// ─── Account ──────────────────────────────────────────────────────────────────

export interface AccountStatus {
  broker: string
  angel_one_configured: boolean
  read_only_available: boolean
  live_orders_possible: boolean
  live_armed: boolean
  kill_switch_active: boolean
}

// ─── Database ─────────────────────────────────────────────────────────────────

export interface DBSummary {
  db_path: string
  total_trades: number
  live_trades: number
  portfolio_snapshots: number
  risk_blocks: number
}

export interface EquityCurvePoint {
  recorded_at: string
  equity: number
  drawdown: number
}

export interface DailyPnl {
  trade_date: string
  realized_pnl: number
  unrealized_pnl: number
  total_trades: number
  winning_trades: number
  ending_equity: number
}

export interface Trade {
  trade_id: string
  symbol: string
  side: string
  quantity: number
  price: number
  charges: number
  timestamp: string
  strategy_name: string
  execution_mode: string
}

// ─── Live Feed ────────────────────────────────────────────────────────────────

export interface LiveFeedSnapshot {
  running: boolean
  subscribed_symbols: string[]
  tick_count: number
}

// ─── WebSocket message ────────────────────────────────────────────────────────

export interface WsDashboardMessage {
  type: 'snapshot'
  timestamp: string
  state: RuntimeState
  monitoring: MonitoringMetrics
  live_feed: LiveFeedSnapshot
  db: DBSummary
  manual_approvals?: number
  event_bus?: EventBusSummary
}
