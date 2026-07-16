// ─── AI Council ───────────────────────────────────────────────────────────────

export interface AgentVote {
  agent_name?: string
  agent?: string
  action: string
  confidence: number
  reasoning: string
}

export interface RiskCritique {
  veto?: boolean
  risk_score?: number
  concerns?: string[]
  [key: string]: unknown
}

export interface PortfolioProposal {
  max_heat?: number
  [key: string]: unknown
}

export interface ExecutionAdvice {
  preferred_order_type?: string
  [key: string]: unknown
}

export interface AICouncilDecision {
  trace_id: string
  action: string
  confidence: number
  consensus_score: number
  votes: AgentVote[]
  debate_triggered?: boolean
  risk_vetoed?: boolean
  timestamp?: string
  ts?: string
  strategy_proposals?: StrategyProposal[]
  risk_critique?: RiskCritique | null
  portfolio_proposal?: PortfolioProposal | null
  execution_advice?: ExecutionAdvice | null
  debate_summary?: string | null
  model_ids_used?: string[]
  evidence_ids?: string[]
  [key: string]: unknown
}

export interface StrategyProposal {
  strategy_name: string
  side: string
  confidence: number
  reasoning?: string
  agent_name?: string
  symbol?: string
  [key: string]: unknown
}

export interface AICouncilStatus {
  enabled: boolean
  gateway_available: boolean
  gateway_runtime: string
  fallback_active: boolean
  primary_model?: string
  models?: Record<string, string>
  [key: string]: unknown
}

// ─── Neural Lab ───────────────────────────────────────────────────────────────

export interface ForecastPrediction {
  symbol: string
  direction_probability: number
  expected_return: number
  model_uncertainty: number
  model_id?: string
}

export interface CorrelationRisk {
  max_pairwise_correlation: number
  contagion_risk_score: number
  model_id: string
  symbols: string[]
  [key: string]: unknown
}

export interface TailRisk {
  symbol?: string
  risk_label?: string
  severity?: number
  extreme_move_probability?: number
  expected_max_drawdown?: number
  [key: string]: unknown
}

export interface NeuralBundle {
  should_trade?: boolean
  overall_uncertainty: number
  forecasts: ForecastPrediction[]
  trace_id?: string
  model_versions?: Record<string, string>
  tail_risks?: TailRisk[]
  correlation_risk?: CorrelationRisk
  [key: string]: unknown
}

export interface NeuralStatus {
  enabled: boolean
  models?: Record<string, string>
  [key: string]: unknown
}

// ─── Quantum Lab ──────────────────────────────────────────────────────────────

export interface QuantumOptimizationResult {
  trace_id: string
  selected_symbols: string[]
  backend_used: string
  objective_value: number
  classical_baseline_objective: number
  improvement_over_classical: number | null
  created_at?: string
  beats_baseline?: boolean
  constraints_satisfied?: boolean
  stable?: boolean
  expected_edge_sum?: number
  risk_score?: number
  [key: string]: unknown
}

export interface QuantumBackendInfo {
  name: string
  available: boolean
  error?: string
  reason?: string
  latency_ms?: number
  [key: string]: unknown
}

export interface QuantumStatus {
  enabled: boolean
  backend: string
  timeout_seconds: number
  backends?: QuantumBackendInfo[]
  [key: string]: unknown
}

// ─── MARL ─────────────────────────────────────────────────────────────────────

export interface MarlAdvisoryResult {
  action: string
  confidence: number
  reasoning?: string
  majority?: { action_label?: string; confidence?: number; [key: string]: unknown }
  majority_action_label?: string
  majority_confidence?: number
  [key: string]: unknown
}

// ─── Goal Governor ────────────────────────────────────────────────────────────

export interface GoalGovernorStatus {
  enabled: boolean
  target_probability: number
  recommendation: string
  yearly_target?: number
  realized_pnl?: number
  current_equity?: number
  required_daily_run_rate?: number
  actual_daily_run_rate?: number
  days_elapsed?: number
  drawdown_budget_remaining?: number
  on_track?: boolean
  [key: string]: unknown
}

// ─── High-End Scan ────────────────────────────────────────────────────────────

export interface HighEndScanResult {
  trace_id: string
  ensemble?: { action: string; confidence: number }
  ensemble_action?: string
  ensemble_confidence?: number
  risk_vetoed?: boolean
  elapsed_ms?: number
  ai_council?: Record<string, unknown>
  neural?: Record<string, unknown>
  quantum?: Record<string, unknown>
}

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

// ─── Live Portfolio (from WS) ─────────────────────────────────────────────────

export interface LivePortfolioMetrics {
  cash: number
  equity: number
  unrealized_pnl: number
  realized_pnl: number
  drawdown: number
  peak_equity: number
  open_positions: number
}

export interface LivePosition {
  symbol: string
  quantity: number
  side: string
  average_price: number
  mark_price: number
  unrealized_pnl: number
  realized_pnl: number
  pnl_pct: number
  live: boolean
}

export interface LivePortfolioSnapshot {
  count: number
  positions: LivePosition[]
  portfolio: LivePortfolioMetrics
}

// ─── WebSocket message ────────────────────────────────────────────────────────

export interface WsDashboardMessage {
  type: 'snapshot'
  timestamp: string
  authenticated?: boolean
  state: RuntimeState
  monitoring: MonitoringMetrics
  live_feed: LiveFeedSnapshot
  // Auth-gated fields: absent when the connection is unauthenticated.
  db?: DBSummary
  manual_approvals?: number
  event_bus?: EventBusSummary
  portfolio?: LivePortfolioSnapshot
}

// ─── Policies ────────────────────────────────────────────────────────────────

export type PolicyStatus = 'research' | 'shadow' | 'paper' | 'live_canary' | 'live_approved' | 'disabled'

export interface PolicyGate {
  approved: boolean
  reason: string
  [key: string]: unknown
}

export interface PolicyInfo {
  policy_id: string
  name: string
  status: PolicyStatus
  created_at?: string
  promoted_at?: string
  description?: string
  gates?: Record<string, PolicyGate>
  promotion_gate?: PolicyGate
  can_submit_live_orders?: boolean
  [key: string]: unknown
}

// ─── Traces ──────────────────────────────────────────────────────────────────

export interface DecisionTrace {
  trace_id: string
  execution_mode: string
  created_at: string
  symbol_universe: string[]
  quantum_result_id?: string
  order_intent_ids?: string[]
  risk_decisions?: Record<string, unknown>[]
  neural_model_versions?: Record<string, string>
  [key: string]: unknown
}

export interface TraceReplaySummary {
  status?: string
  lifecycle_complete?: boolean
  order_count?: number
  fill_count?: number
  journal_event_count?: number
  slippage_count?: number
  label_count?: number
  learning_update_count?: number
  [key: string]: unknown
}

export interface TraceReplayResponse {
  trace_id: string
  trace?: DecisionTrace
  summary?: TraceReplaySummary
  timeline?: Record<string, unknown>[]
  risk_decisions?: Record<string, unknown>[]
  events?: Record<string, unknown>[]
  [key: string]: unknown
}

export interface ShortVolLeg {
  symbol: string
  strike: number
  option_type: string
  side: string
  is_wing: boolean
  price: number
  quantity: number
}

export interface ShortVolPreview {
  mode: string
  underlying: string
  spot: number
  vix: number
  realized_vol: number
  vrp: number
  enter: boolean
  reason: string
  lots: number
  net_credit_pts: number
  max_loss_pts: number
  legs: ShortVolLeg[]
  expiry?: string
}
