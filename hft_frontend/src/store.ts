import { create } from 'zustand'
import type {
  BacktestResult,
  DailyPnl,
  DBSummary,
  EquityCurvePoint,
  LiveFeedSnapshot,
  ModelCatalogEntry,
  MonitoringMetrics,
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
  applyWsSnapshot: (msg) =>
    set({
      runtimeState: msg.state,
      monitoring: msg.monitoring,
      liveFeed: msg.live_feed,
      dbSummary: msg.db,
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

  loading: { signals: false, strategies: false, backtest: false, walkForward: false, models: false, account: false },
  setLoading: (key, v) => set((s) => ({ loading: { ...s.loading, [key]: v } })),
  error: null,
  setError: (msg) => set({ error: msg }),
}))
