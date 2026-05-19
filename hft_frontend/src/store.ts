import { create } from 'zustand'
import type {
  AICouncilDecision,
  AICouncilStatus,
  BacktestResult,
  DailyPnl,
  DBSummary,
  DecisionTrace,
  EquityCurvePoint,
  GoalGovernorStatus,
  HighEndScanResult,
  LiveFeedSnapshot,
  LivePortfolioSnapshot,
  ModelCatalogEntry,
  MonitoringMetrics,
  NeuralBundle,
  NeuralStatus,
  PolicyInfo,
  QuantumOptimizationResult,
  QuantumStatus,
  RuntimeState,
  SignalScanResult,
  StrategyEvaluationResult,
  TargetProgress,
  Trade,
  WalkForwardResult,
  WsDashboardMessage,
} from './types'

export type NavView =
  | 'dashboard'
  | 'engine'
  | 'signals'
  | 'strategies'
  | 'backtest'
  | 'models'
  | 'risk'
  | 'execution'
  | 'intelligence'
  | 'account'

interface LoadingState {
  signals: boolean
  strategies: boolean
  backtest: boolean
  walkForward: boolean
  models: boolean
  account: boolean
  highEndScan: boolean
}

interface Store {
  // ── Navigation ────────────────────────────────────────────────────────────
  activeView: NavView
  setActiveView: (v: NavView) => void

  // ── WebSocket connection ──────────────────────────────────────────────────
  wsConnected: boolean
  setWsConnected: (v: boolean) => void

  // ── Live dashboard state (from WS) ────────────────────────────────────────
  runtimeState: RuntimeState | null
  monitoring: MonitoringMetrics | null
  liveFeed: LiveFeedSnapshot | null
  dbSummary: DBSummary | null
  livePortfolio: LivePortfolioSnapshot | null
  applyWsSnapshot: (msg: WsDashboardMessage) => void

  // ── Equity / PnL ──────────────────────────────────────────────────────────
  equityCurve: EquityCurvePoint[]
  dailyPnl: DailyPnl[]
  recentTrades: Trade[]
  setEquityCurve: (data: EquityCurvePoint[]) => void
  setDailyPnl: (data: DailyPnl[]) => void
  setRecentTrades: (data: Trade[]) => void

  // ── Target progress ───────────────────────────────────────────────────────
  targetProgress: TargetProgress | null
  setTargetProgress: (data: TargetProgress) => void

  // ── Signals ───────────────────────────────────────────────────────────────
  signalResult: SignalScanResult | null
  setSignalResult: (r: SignalScanResult) => void

  // ── Strategies ────────────────────────────────────────────────────────────
  strategyResult: StrategyEvaluationResult | null
  setStrategyResult: (r: StrategyEvaluationResult) => void

  // ── Backtest ──────────────────────────────────────────────────────────────
  backtestResult: BacktestResult | null
  setBacktestResult: (r: BacktestResult) => void
  walkForwardResult: WalkForwardResult | null
  setWalkForwardResult: (r: WalkForwardResult) => void

  // ── Models ────────────────────────────────────────────────────────────────
  modelCatalog: ModelCatalogEntry[]
  setModelCatalog: (m: ModelCatalogEntry[]) => void

  // ── AI Council ────────────────────────────────────────────────────────────
  aiCouncilStatus: AICouncilStatus | null
  setAICouncilStatus: (s: AICouncilStatus) => void
  aiCouncilDecisions: AICouncilDecision[]
  setAICouncilDecisions: (d: AICouncilDecision[]) => void

  // ── Neural Lab ────────────────────────────────────────────────────────────
  neuralStatus: NeuralStatus | null
  setNeuralStatus: (s: NeuralStatus) => void
  latestNeuralBundle: NeuralBundle | null
  setLatestNeuralBundle: (b: NeuralBundle) => void

  // ── Quantum Lab ───────────────────────────────────────────────────────────
  quantumStatus: QuantumStatus | null
  setQuantumStatus: (s: QuantumStatus) => void
  latestQuantumResult: QuantumOptimizationResult | null
  setLatestQuantumResult: (r: QuantumOptimizationResult) => void

  // ── Goal Governor ─────────────────────────────────────────────────────────
  goalGovernorStatus: GoalGovernorStatus | null
  setGoalGovernorStatus: (s: GoalGovernorStatus) => void

  // ── High-End Scan ─────────────────────────────────────────────────────────
  latestHighEndScan: HighEndScanResult | null
  setLatestHighEndScan: (r: HighEndScanResult) => void

  // ── Traces ────────────────────────────────────────────────────────────────
  recentTraces: DecisionTrace[]
  setRecentTraces: (t: DecisionTrace[]) => void

  // ── Policies ──────────────────────────────────────────────────────────────
  policies: PolicyInfo[]
  setPolicies: (p: PolicyInfo[]) => void

  // ── Loading & errors ──────────────────────────────────────────────────────
  loading: LoadingState
  setLoading: (key: keyof LoadingState, v: boolean) => void
  error: string | null
  setError: (msg: string | null) => void
}

export const useStore = create<Store>((set) => ({
  activeView: 'dashboard',
  setActiveView: (v) => set({ activeView: v }),

  wsConnected: false,
  setWsConnected: (v) => set({ wsConnected: v }),

  runtimeState: null,
  monitoring: null,
  liveFeed: null,
  dbSummary: null,
  livePortfolio: null,
  applyWsSnapshot: (msg) =>
    set({
      runtimeState: msg.state,
      monitoring: msg.monitoring,
      liveFeed: msg.live_feed,
      dbSummary: msg.db,
      livePortfolio: msg.portfolio ?? null,
    }),

  equityCurve: [],
  dailyPnl: [],
  recentTrades: [],
  setEquityCurve: (data) => set({ equityCurve: data }),
  setDailyPnl: (data) => set({ dailyPnl: data }),
  setRecentTrades: (data) => set({ recentTrades: data }),

  targetProgress: null,
  setTargetProgress: (data) => set({ targetProgress: data }),

  signalResult: null,
  setSignalResult: (r) => set({ signalResult: r }),

  strategyResult: null,
  setStrategyResult: (r) => set({ strategyResult: r }),

  backtestResult: null,
  setBacktestResult: (r) => set({ backtestResult: r }),
  walkForwardResult: null,
  setWalkForwardResult: (r) => set({ walkForwardResult: r }),

  modelCatalog: [],
  setModelCatalog: (m) => set({ modelCatalog: m }),

  aiCouncilStatus: null,
  setAICouncilStatus: (s) => set({ aiCouncilStatus: s }),
  aiCouncilDecisions: [],
  setAICouncilDecisions: (d) => set({ aiCouncilDecisions: d }),

  neuralStatus: null,
  setNeuralStatus: (s) => set({ neuralStatus: s }),
  latestNeuralBundle: null,
  setLatestNeuralBundle: (b) => set({ latestNeuralBundle: b }),

  quantumStatus: null,
  setQuantumStatus: (s) => set({ quantumStatus: s }),
  latestQuantumResult: null,
  setLatestQuantumResult: (r) => set({ latestQuantumResult: r }),

  goalGovernorStatus: null,
  setGoalGovernorStatus: (s) => set({ goalGovernorStatus: s }),

  latestHighEndScan: null,
  setLatestHighEndScan: (r) => set({ latestHighEndScan: r }),

  recentTraces: [],
  setRecentTraces: (t) => set({ recentTraces: t }),

  policies: [],
  setPolicies: (p) => set({ policies: p }),

  loading: { signals: false, strategies: false, backtest: false, walkForward: false, models: false, account: false, highEndScan: false },
  setLoading: (key, v) => set((s) => ({ loading: { ...s.loading, [key]: v } })),
  error: null,
  setError: (msg) => set({ error: msg }),
}))
