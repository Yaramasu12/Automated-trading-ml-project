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

export interface LiveCanaryReadiness {
  can_consider_live_canary: boolean
  status: 'READY' | 'NOT_READY'
  blocking_reasons: string[]
  evaluated_at: string
  paper_window: {
    min_days: number
    target_max_days: number
    actual_days: number
    minimum_met: boolean
    within_target_review_window: boolean
    review_overdue: boolean
  }
  metrics: Record<string, unknown>
  checks: Array<{ name: string; passed: boolean; required?: unknown; actual?: unknown; reason?: string }>
  policy_candidates: Array<Record<string, unknown>>
  requirements: Record<string, unknown>
  evidence: { trace_count: number; trace_ids: string[]; paper_days: string[] }
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

// ─── AI Council (Phase 2) ─────────────────────────────────────────────────────

export interface GatewayModels {
  primary: string
  coordinator: string
  fast: string
}

export interface AICouncilStatus {
  enabled: boolean
  gateway_runtime: string
  primary_model: string
  gateway_available: boolean
  fallback_active: boolean
  gateway_note: string
  models: GatewayModels
}

export interface AgentVote {
  agent?: string
  agent_name?: string
  action: string
  confidence: number
  reasoning: string
  evidence_ids?: string[]
  model_id?: string
  schema_version?: string
  failure_mode?: string | null
  ts?: string
}

export interface StrategyProposal {
  agent_name: string
  symbol: string
  side: string
  edge_estimate: number
  risk_estimate: number
  confidence: number
  evidence_ids?: string[]
  invalidation_rule?: string
  reasoning?: string
  model_id?: string
}

export interface RiskCritique {
  veto: boolean
  risk_score: number
  concerns: string[]
  recommended_action: string
}

export interface PortfolioProposal {
  preferred_basket: string[]
  expected_return_estimate: number
  max_heat: number
  hedge_request?: string | null
  target_run_rate_ok: boolean
}

export interface ExecutionAdvice {
  avoid_windows: string[]
  preferred_order_type: string
  max_slice_size_pct: number
}

export interface AICouncilDecision {
  trace_id: string
  action: string
  confidence: number
  consensus_score: number
  votes: AgentVote[]
  strategy_proposals?: StrategyProposal[]
  risk_critique?: RiskCritique | null
  portfolio_proposal?: PortfolioProposal | null
  execution_advice?: ExecutionAdvice | null
  debate_summary?: string
  model_ids_used?: string[]
  evidence_ids?: string[]
  debate_triggered?: boolean
  risk_vetoed?: boolean
  ts?: string
  timestamp?: string
}

// ─── Neural Lab (Phase 3) ─────────────────────────────────────────────────────

export interface NeuralStatus {
  enabled: boolean
  models: {
    forecaster: string
    volatility: string
    tail_risk: string
    correlation: string
  }
}

export interface ForecastPrediction {
  symbol: string
  direction_probability: number
  expected_return: number
  model_uncertainty: number
}

export interface NeuralPredictionBundle {
  trace_id: string
  overall_uncertainty: number
  forecasts: ForecastPrediction[]
  volatility?: Array<{
    symbol: string
    predicted_volatility: number
    garch_volatility: number
    tail_risk_score: number
    model_id: string
    confidence: number
  }>
  correlation_risk?: {
    symbols: string[]
    max_pairwise_correlation: number
    contagion_risk_score: number
    model_id: string
  } | null
  tail_risks?: Array<{
    symbol: string
    extreme_move_probability: number
    expected_max_drawdown: number
    model_id: string
    confidence: number
  }>
  model_versions?: Record<string, string>
  should_trade?: boolean
  ts?: string
  timestamp?: string
}

// ─── Quantum Lab (Phase 4) ────────────────────────────────────────────────────

export interface QuantumBackendStatus {
  name: string
  available: boolean
  reason?: string
  error?: string | null
  latency_ms?: number | null
}

export interface QuantumStatus {
  enabled: boolean
  backend: string
  timeout_seconds: number
  backends: QuantumBackendStatus[]
}

export interface QuantumOptimizationResult {
  trace_id: string
  selected_symbols: string[]
  backend_used: string
  expected_edge_sum?: number
  risk_score?: number
  objective_value: number
  classical_baseline_objective?: number
  improvement_over_classical?: number
  beats_baseline?: boolean
  constraints_satisfied?: boolean
  stable?: boolean
  advisory_only?: boolean
  ts?: string
  timestamp?: string
}

// ─── Goal Governor (Phase 6) ──────────────────────────────────────────────────

export interface GoalGovernorStatus {
  enabled: boolean
  yearly_target: number
  initial_capital?: number
  current_equity?: number
  realized_pnl?: number
  drawdown_budget_remaining?: number
  days_elapsed: number
  target_probability: number
  required_daily_run_rate?: number
  actual_daily_run_rate?: number
  on_track?: boolean
  recommendation: string
  can_raise_risk_limits?: false
  message?: string
  ts?: string
}

// ─── Decision Trace (Phase 1) ─────────────────────────────────────────────────

export interface DecisionTrace {
  trace_id: string
  created_at: string
  execution_mode: string
  symbol_universe: string[]
  feature_snapshot_ids?: Record<string, string>
  agent_outputs?: Record<string, unknown>[]
  neural_model_versions?: Record<string, string>
  quantum_result_id?: string
  risk_decisions?: Record<string, unknown>[]
  order_intent_ids?: string[]
  broker_result_id?: string | null
  events?: { event_type: string; component: string; data?: Record<string, unknown>; ts: string }[]
  metadata?: Record<string, unknown>
}

export interface TraceReplayEvent {
  ts?: string
  source: 'trace' | 'oms' | 'label' | 'paper_journal'
  event_type: string
  component?: string
  order_id?: string | null
  symbol?: string | null
  reason?: string | null
  data?: Record<string, unknown>
}

export interface TraceReplayResponse {
  trace_id: string
  summary: {
    status: string
    execution_mode?: string
    symbols?: string[]
    order_count: number
    timeline_event_count: number
    trace_event_count: number
    oms_event_count: number
    journal_event_count?: number
    journal_event_counts?: Record<string, number>
    trade_count: number
    fill_count?: number
    slippage_count?: number
    label_count: number
    learning_update_count?: number
    rejection_count: number
    broker_submitted: boolean
    broker_filled: boolean
    lifecycle_complete: boolean
    missing_stages: string[]
  }
  trace: DecisionTrace
  timeline: TraceReplayEvent[]
  orders: Array<Record<string, unknown>>
  labels: Array<Record<string, unknown>>
  fills?: Array<Record<string, unknown>>
  slippage?: Array<Record<string, unknown>>
  trades: Array<Record<string, unknown>>
  risk_decisions: Array<Record<string, unknown>>
  paper_journal?: { event_count: number; events: Array<Record<string, unknown>> }
  raw?: Record<string, unknown>
}

// ─── RL Policies (Phase 5) ────────────────────────────────────────────────────

export type PolicyStatus =
  | 'research'
  | 'shadow'
  | 'paper'
  | 'live_canary'
  | 'live_approved'
  | 'disabled'

export interface PolicyPromotionCheck {
  name: string
  passed: boolean
  required?: unknown
  actual?: unknown
  reason?: string
}

export interface PolicyPromotionGate {
  approved: boolean
  reason: string
  current_status?: PolicyStatus | null
  target_status?: PolicyStatus | null
  checks: PolicyPromotionCheck[]
  metrics?: Record<string, unknown>
  requirements?: Record<string, unknown>
}

export interface PolicyInfo {
  policy_id: string
  name?: string
  role?: string
  status: PolicyStatus
  version?: number
  can_submit_live_orders?: boolean
  promoted_at?: string
  rollback_pointer?: string
  metadata?: Record<string, unknown>
  promotion_gate?: PolicyPromotionGate
}

export interface MarlStatus {
  enabled: boolean
  policy_count: number
  active_policy_count: number
  policies: PolicyInfo[]
  note: string
}

export interface MarlPolicyVote {
  policy_id: string
  role?: string
  status: PolicyStatus
  action: number
  action_label: string
  can_submit_live_orders?: boolean
}

export interface MarlAdvisoryResult {
  advisory_only: boolean
  majority?: {
    action: number
    action_label: string
    confidence: number
  }
  enabled?: boolean
  policy_count?: number
  majority_action?: number
  majority_action_label?: string
  majority_confidence?: number
  policy_votes?: MarlPolicyVote[]
  votes?: MarlPolicyVote[]
  note?: string
}

export interface EnsembleOutput {
  trace_id: string
  proceed: boolean
  action: string
  confidence: number
  weighted_score: number
  regime: string
  uncertainty_penalty: number
  champion_policy_id?: string | null
  reasoning?: string[]
  ts?: string
}

// ─── High-End Scan (Phase 7) ──────────────────────────────────────────────────

export interface HighEndScanResult {
  trace_id: string
  execution_mode: string
  symbols: string[]
  ai_council?: AICouncilDecision | null
  neural?: NeuralPredictionBundle | { overall_uncertainty: number; should_trade?: boolean } | null
  quantum?: QuantumOptimizationResult | { selected_symbols: string[]; backend_used: string } | null
  marl?: MarlAdvisoryResult | null
  ensemble?: EnsembleOutput | null
  goal_governor?: GoalGovernorStatus | Record<string, unknown> | null
  trace_metadata?: {
    trace_id: string
    components_active: Record<string, boolean>
  }
  ensemble_action?: string
  ensemble_confidence?: number
  risk_vetoed?: boolean
  elapsed_ms?: number
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
  portfolio?: LivePortfolioSnapshot
}
