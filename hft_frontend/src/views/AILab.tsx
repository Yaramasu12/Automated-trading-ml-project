import { useEffect, useRef, useState, useCallback } from 'react'
import { clsx } from 'clsx'
import {
  Atom, Brain, Cpu, Loader2, RefreshCw, Target, Zap, GitMerge, Database,
  ChevronDown, ChevronRight as ChevronRightIcon, TrendingUp, ShieldCheck, Layers,
} from 'lucide-react'
import { RadialBarChart, RadialBar, ResponsiveContainer, Tooltip } from 'recharts'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Table } from '../components/shared/Table'
import { Tag } from '../components/shared/Badge'
import { useStore } from '../store'
import {
  getAICouncilStatus, getAICouncilDecisions, runAICouncilPreview,
  getNeuralStatus, runNeuralPredictPreview,
  getQuantumStatus, runQuantumOptimizePreview, getQuantumResults,
  getGoalGovernorStatus,
  listTraces, runHighEndScan,
  runMarlAdvisoryPreview,
  getOrchestratorStats, getOrchestratorProfitGuard, getOrchestratorReflections,
  getDBReflectionsHistory,
} from '../api'
import { pct, fmtDateTime } from '../utils'
import type { AgentVote, ForecastPrediction, MarlAdvisoryResult } from '../types'

const TABS = [
  { id: 'overview',      label: 'Overview',     icon: <Atom size={13} /> },
  { id: 'council',       label: 'AI Council',   icon: <Brain size={13} /> },
  { id: 'neural',        label: 'Neural Lab',   icon: <Zap size={13} /> },
  { id: 'quantum',       label: 'Quantum',      icon: <Cpu size={13} /> },
  { id: 'marl',          label: 'MARL',         icon: <GitMerge size={13} /> },
  { id: 'orchestrator',  label: 'Orchestrator', icon: <Database size={13} /> },
]

const TOOLTIP_STYLE = {
  backgroundColor: '#161b22', border: '1px solid #30363d', borderRadius: 6, fontSize: 12, color: '#e6edf3',
}

export function AILab() {
  const [tab, setTab] = useState('overview')

  const aiCouncilStatus    = useStore((s) => s.aiCouncilStatus)
  const aiCouncilDecisions = useStore((s) => s.aiCouncilDecisions)
  const neuralStatus       = useStore((s) => s.neuralStatus)
  const latestNeuralBundle = useStore((s) => s.latestNeuralBundle)
  const quantumStatus      = useStore((s) => s.quantumStatus)
  const latestQuantumResult = useStore((s) => s.latestQuantumResult)
  const goalStatus         = useStore((s) => s.goalGovernorStatus)
  const latestHighEndScan  = useStore((s) => s.latestHighEndScan)
  const recentTraces       = useStore((s) => s.recentTraces)

  const setAICouncilStatus    = useStore((s) => s.setAICouncilStatus)
  const setAICouncilDecisions = useStore((s) => s.setAICouncilDecisions)
  const setNeuralStatus       = useStore((s) => s.setNeuralStatus)
  const setLatestNeuralBundle = useStore((s) => s.setLatestNeuralBundle)
  const setQuantumStatus      = useStore((s) => s.setQuantumStatus)
  const setLatestQuantumResult = useStore((s) => s.setLatestQuantumResult)
  const setGoalStatus         = useStore((s) => s.setGoalGovernorStatus)
  const setLatestHighEndScan  = useStore((s) => s.setLatestHighEndScan)
  const setRecentTraces       = useStore((s) => s.setRecentTraces)

  const setLoadingStore = useStore((s) => s.setLoading)
  const loadingStore    = useStore((s) => s.loading)

  // Council tab state
  const [councilSymbols, setCouncilSymbols]   = useState('NIFTY,BANKNIFTY')
  const [councilRegime, setCouncilRegime]     = useState('NEUTRAL')
  const [councilLoading, setCouncilLoading]   = useState(false)
  const [councilResult, setCouncilResult]     = useState<typeof aiCouncilDecisions[0] | null>(null)

  // Neural tab state
  const [neuralSymbols, setNeuralSymbols]     = useState('NIFTY,BANKNIFTY')
  const [neuralLoading, setNeuralLoading]     = useState(false)

  // Quantum tab state
  const [quantumCandidates, setQuantumCandidates] = useState(JSON.stringify([
    { symbol: 'NIFTY', side: 'BUY', expected_edge: 0.014, risk_estimate: 0.31 },
    { symbol: 'BANKNIFTY', side: 'BUY', expected_edge: 0.018, risk_estimate: 0.42 },
    { symbol: 'RELIANCE', side: 'BUY', expected_edge: 0.011, risk_estimate: 0.26 },
  ], null, 2))
  const [quantumRiskAversion, setQuantumRiskAversion] = useState(0.5)
  const [quantumCardinality, setQuantumCardinality]   = useState(5)
  const [quantumLoading, setQuantumLoading]   = useState(false)
  const [quantumHistory, setQuantumHistory]   = useState<Record<string, unknown>[]>([])

  // MARL tab state
  const [marlForm, setMarlForm] = useState({ portfolio_pnl: 0, open_positions: 0, drawdown: 0 })
  const [marlResult, setMarlResult] = useState<MarlAdvisoryResult | null>(null)
  const [marlLoading, setMarlLoading] = useState(false)

  // High-End Scan
  const [scanSymbols, setScanSymbols] = useState('NIFTY,BANKNIFTY')
  const [scanLoading, setScanLoading] = useState(false)
  const [scanExpanded, setScanExpanded] = useState<string | null>(null)

  // Orchestrator tab state
  const [orchStats, setOrchStats]           = useState<Record<string, unknown> | null>(null)
  const [orchPG, setOrchPG]                 = useState<Record<string, unknown> | null>(null)
  const [orchReflections, setOrchReflections] = useState<Record<string, unknown>[]>([])
  const [orchDBReflections, setOrchDBReflections] = useState<Record<string, unknown>[]>([])
  const [orchLoading, setOrchLoading]       = useState(false)
  const [orchUnderlying, setOrchUnderlying] = useState('NIFTY')

  const loadOrchestrator = useCallback(async () => {
    setOrchLoading(true)
    try {
      const [stats, pg, refl, dbRefl] = await Promise.allSettled([
        getOrchestratorStats(),
        getOrchestratorProfitGuard(),
        getOrchestratorReflections(20),
        getDBReflectionsHistory(20),
      ])
      if (stats.status === 'fulfilled') setOrchStats(stats.value)
      if (pg.status === 'fulfilled')    setOrchPG(pg.value)
      if (refl.status === 'fulfilled')  setOrchReflections(refl.value.reflections ?? [])
      if (dbRefl.status === 'fulfilled') setOrchDBReflections(dbRefl.value.reflections ?? [])
    } finally {
      setOrchLoading(false)
    }
  }, [])

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadAll = useCallback(async () => {
    const [cs, ns, qs, gs, traces] = await Promise.allSettled([
      getAICouncilStatus(),
      getNeuralStatus(),
      getQuantumStatus(),
      getGoalGovernorStatus(),
      listTraces(10),
    ])
    if (cs.status === 'fulfilled')     setAICouncilStatus(cs.value)
    if (ns.status === 'fulfilled')     setNeuralStatus(ns.value)
    if (qs.status === 'fulfilled')     setQuantumStatus(qs.value)
    if (gs.status === 'fulfilled')     setGoalStatus(gs.value)
    if (traces.status === 'fulfilled') setRecentTraces(traces.value.traces)
  }, [setAICouncilStatus, setNeuralStatus, setQuantumStatus, setGoalStatus, setRecentTraces])

  useEffect(() => {
    loadAll()
    pollRef.current = setInterval(loadAll, 30_000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [loadAll])

  useEffect(() => {
    if (tab === 'council' && aiCouncilDecisions.length === 0) {
      getAICouncilDecisions(10).then(r => setAICouncilDecisions(r.decisions)).catch(() => {})
    }
    if (tab === 'quantum') {
      getQuantumResults(10).then(r => setQuantumHistory(r.results as unknown as Record<string, unknown>[])).catch(() => {})
    }
    if (tab === 'orchestrator') {
      loadOrchestrator()
    }
  }, [tab, loadOrchestrator])

  const handleCouncilPreview = async () => {
    setCouncilLoading(true)
    try {
      const syms = councilSymbols.split(',').map(s => s.trim()).filter(Boolean)
      const res = await runAICouncilPreview({ symbols: syms, regime: councilRegime })
      setCouncilResult(res)
    } catch { /* ignore */ }
    finally { setCouncilLoading(false) }
  }

  const handleNeuralPreview = async () => {
    setNeuralLoading(true)
    try {
      const syms = neuralSymbols.split(',').map(s => s.trim()).filter(Boolean)
      const res = await runNeuralPredictPreview({ symbols: syms })
      setLatestNeuralBundle(res)
    } catch { /* ignore */ }
    finally { setNeuralLoading(false) }
  }

  const handleQuantumPreview = async () => {
    setQuantumLoading(true)
    try {
      let candidates: unknown[]
      try { candidates = JSON.parse(quantumCandidates) } catch { candidates = [] }
      const res = await runQuantumOptimizePreview({
        candidates,
        risk_aversion: quantumRiskAversion,
        cardinality_limit: quantumCardinality,
      })
      setLatestQuantumResult(res)
    } catch { /* ignore */ }
    finally { setQuantumLoading(false) }
  }

  const handleHighEndScan = async () => {
    setScanLoading(true)
    setLoadingStore('highEndScan', true)
    try {
      const syms = scanSymbols.split(',').map(s => s.trim()).filter(Boolean)
      const res = await runHighEndScan({ symbols: syms })
      setLatestHighEndScan(res)
    } catch { /* ignore */ }
    finally { setScanLoading(false); setLoadingStore('highEndScan', false) }
  }

  const systems = [
    {
      id: 'council', phase: 'Phase 2', label: 'AI Council',
      icon: <Brain size={14} className="text-brand-purple" />,
      enabled: aiCouncilStatus?.enabled,
      live: aiCouncilStatus?.gateway_available,
      detail: `Runtime: ${aiCouncilStatus?.gateway_runtime ?? '—'} · Fallback: ${aiCouncilStatus?.fallback_active ? 'yes' : 'no'}`,
      color: 'purple',
    },
    {
      id: 'neural', phase: 'Phase 3', label: 'Neural Lab',
      icon: <Zap size={14} className="text-brand-blue" />,
      enabled: neuralStatus?.enabled,
      live: true,
      detail: `Forecaster: ${neuralStatus?.models?.forecaster ?? '—'}`,
      color: 'blue',
    },
    {
      id: 'quantum', phase: 'Phase 4', label: 'Quantum',
      icon: <Cpu size={14} className="text-brand-cyan" />,
      enabled: quantumStatus?.enabled,
      live: quantumStatus?.backends?.some(b => b.available),
      detail: `Backend: ${quantumStatus?.backend ?? '—'}`,
      color: 'cyan',
    },
    {
      id: 'goal', phase: 'Phase 6', label: 'Goal Governor',
      icon: <Target size={14} className="text-brand-green" />,
      enabled: goalStatus?.enabled,
      live: true,
      detail: goalStatus ? `p=${pct(goalStatus.target_probability)} · ${goalStatus.recommendation}` : '—',
      color: 'green',
    },
    {
      id: 'blackboard', phase: 'Phase 8', label: 'Decision Blackboard',
      icon: <Database size={14} className="text-brand-orange" />,
      enabled: true, live: true,
      detail: 'Always active',
      color: 'orange',
    },
    {
      id: 'tracestore', phase: 'Phase 1', label: 'TraceStore',
      icon: <Atom size={14} className="text-brand-yellow" />,
      enabled: true, live: true,
      detail: `${recentTraces.length} recent traces`,
      color: 'yellow',
    },
    {
      id: 'ensemble', phase: 'Phase 8', label: 'Ensemble Engine',
      icon: <GitMerge size={14} className="text-brand-purple" />,
      enabled: true, live: true,
      detail: 'Always active',
      color: 'purple',
    },
  ]

  const scanAction = latestHighEndScan?.ensemble?.action ?? latestHighEndScan?.ensemble_action ?? 'NO_TRADE'
  const scanConfidence = latestHighEndScan?.ensemble?.confidence ?? latestHighEndScan?.ensemble_confidence ?? 0
  const scanRiskVetoed = latestHighEndScan?.risk_vetoed ?? latestHighEndScan?.ensemble?.action === 'HALT'
  const scanElapsed = latestHighEndScan?.elapsed_ms
  const neuralShouldTrade = latestNeuralBundle?.should_trade ?? ((latestNeuralBundle?.overall_uncertainty ?? 1) < 0.65)

  return (
    <div className="space-y-4">

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Atom size={16} className="text-brand-purple" />
          <div>
            <h1 className="text-lg font-bold text-gray-100">AI Lab</h1>
            <p className="text-xs text-gray-500 mt-0.5">Quantum Multi-Agent Inference · Phase 1–9</p>
          </div>
        </div>
        <button
          onClick={loadAll}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-surface-elevated border border-surface-border text-xs text-gray-400 hover:text-gray-200 transition-colors"
        >
          <RefreshCw size={12} /> Refresh
        </button>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-1 overflow-x-auto pb-1">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={clsx(
              'flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium whitespace-nowrap border transition-colors',
              tab === t.id
                ? 'bg-brand-purple/15 border-brand-purple/30 text-brand-purple'
                : 'text-gray-400 hover:text-gray-200 hover:bg-surface-elevated border-transparent',
            )}
          >
            {t.icon}
            {t.label}
          </button>
        ))}
      </div>

      {/* Tab: Overview */}
      {tab === 'overview' && (
        <div className="space-y-4">
          {/* System Status Grid */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            {systems.map((s) => (
              <div
                key={s.id}
                className={clsx(
                  'rounded-lg border p-3 space-y-1.5',
                  !s.enabled
                    ? 'bg-surface-card border-surface-border'
                    : s.live
                      ? 'bg-brand-purple/5 border-brand-purple/20'
                      : 'bg-brand-yellow/5 border-brand-yellow/20',
                )}
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    {s.icon}
                    <span className="text-xs font-semibold text-gray-200">{s.label}</span>
                  </div>
                  <span className={clsx(
                    'text-[9px] font-mono font-bold px-1.5 py-0.5 rounded',
                    !s.enabled ? 'text-gray-700 bg-gray-800'
                      : s.live   ? 'text-brand-green bg-brand-green/10'
                               : 'text-brand-yellow bg-brand-yellow/10',
                  )}>
                    {!s.enabled ? 'OFF' : s.live ? 'ON' : 'STUB'}
                  </span>
                </div>
                <div className="text-[10px] text-brand-purple/70">{s.phase}</div>
                <div className="text-[10px] text-gray-500 truncate">{s.detail}</div>
              </div>
            ))}
          </div>

          {/* High-End Scan */}
          <Card className="border-brand-purple/20 bg-brand-purple/5">
            <CardHeader
              title="Full AI Scan (Phase 7)"
              subtitle="Chains all phases · advisory only"
              icon={<Atom size={14} className="text-brand-purple" />}
            />
            <CardBody className="space-y-3">
              <div className="flex flex-wrap items-center gap-3">
                <input
                  value={scanSymbols}
                  onChange={(e) => setScanSymbols(e.target.value)}
                  placeholder="Symbols (comma-separated)"
                  className="flex-1 min-w-32 bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-2 py-1.5 font-mono focus:outline-none focus:border-brand-purple"
                />
                <button
                  onClick={handleHighEndScan}
                  disabled={scanLoading}
                  className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-purple/15 border border-brand-purple/30 text-brand-purple text-xs font-medium hover:bg-brand-purple/25 disabled:opacity-50 transition-colors"
                >
                  {scanLoading ? <Loader2 size={12} className="animate-spin" /> : <Atom size={12} />}
                  Run Full AI Scan
                </button>
              </div>

              {latestHighEndScan && (
                <div className="space-y-3 border-t border-brand-purple/10 pt-3">
                  <div className="flex items-center gap-3 flex-wrap">
                    <Tag
                      label={scanAction}
                      color={
                        scanAction.includes('BUY') || scanAction === 'PROCEED' ? 'green'
                        : scanAction.includes('SELL') || scanAction === 'HALT' ? 'red'
                        : 'gray'
                      }
                    />
                    <span className="text-xs text-gray-300 font-mono">
                      Confidence: <strong>{(scanConfidence * 100).toFixed(0)}%</strong>
                    </span>
                    {scanElapsed != null && <span className="text-xs text-gray-500 font-mono">{scanElapsed.toFixed(0)}ms</span>}
                    {scanRiskVetoed && <Tag label="Risk Vetoed" color="red" />}
                  </div>
                  {/* Component breakdown */}
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                    {[
                      { label: 'Council', data: latestHighEndScan.ai_council, key: 'action' },
                      { label: 'Neural', data: latestHighEndScan.neural, key: 'overall_uncertainty' },
                      { label: 'Quantum', data: latestHighEndScan.quantum, key: 'backend_used' },
                    ].map((c) => (
                      c.data && (
                        <div key={c.label} className="bg-surface-elevated rounded p-2">
                          <div className="text-[10px] text-gray-500 uppercase">{c.label}</div>
                          <div className="text-xs font-mono text-gray-300 mt-1">
                            {String((c.data as Record<string, unknown>)[c.key] ?? '—')}
                          </div>
                        </div>
                      )
                    ))}
                  </div>
                  <div className="text-[10px] text-gray-600 font-mono">ID: {latestHighEndScan.trace_id}</div>
                </div>
              )}
            </CardBody>
          </Card>

          {/* Recent Traces */}
          <Card>
            <CardHeader title="Recent Decision Traces" subtitle={`Last ${recentTraces.length} traces`} icon={<Database size={14} />} />
            <Table
              columns={[
                { key: 'trace_id', label: 'Trace ID', render: (v) => <span className="text-xs font-mono text-brand-blue">{String(v).slice(0, 16)}</span> },
                { key: 'execution_mode', label: 'Mode' },
                { key: 'symbol_universe', label: 'Symbols', render: (v) => (
                  <span className="text-xs text-gray-400">{(v as string[])?.join(', ') ?? '—'}</span>
                )},
                { key: 'created_at', label: 'Time', render: (v) => <span className="text-xs text-gray-500">{fmtDateTime(String(v))}</span> },
              ]}
              rows={recentTraces as unknown as Record<string, unknown>[]}
              keyFn={(r) => String(r.trace_id)}
              emptyMessage="No traces yet"
              compact
            />
          </Card>
        </div>
      )}

      {/* Tab: AI Council */}
      {tab === 'council' && (
        <div className="space-y-4">
          {/* Gateway Status */}
          <Card>
            <CardHeader title="Gateway Status" icon={<Brain size={14} className="text-brand-purple" />} />
            <CardBody>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                {[
                  { label: 'Enabled', value: String(aiCouncilStatus?.enabled ?? false) },
                  { label: 'Runtime', value: aiCouncilStatus?.gateway_runtime ?? '—' },
                  { label: 'Available', value: String(aiCouncilStatus?.gateway_available ?? false) },
                  { label: 'Fallback', value: String(aiCouncilStatus?.fallback_active ?? false) },
                ].map((s) => (
                  <div key={s.label} className="bg-surface-elevated rounded p-2">
                    <div className="text-[10px] text-gray-500 uppercase">{s.label}</div>
                    <div className="text-sm font-bold font-mono text-gray-100 mt-1">{s.value}</div>
                  </div>
                ))}
              </div>
              {aiCouncilStatus?.models && (
                <div className="mt-3 pt-3 border-t border-surface-border">
                  <div className="text-xs text-gray-400 mb-2">Models</div>
                  <div className="grid grid-cols-3 gap-2">
                    {Object.entries(aiCouncilStatus.models).map(([k, v]) => (
                      <div key={k} className="text-xs">
                        <span className="text-gray-500">{k}: </span>
                        <span className="text-gray-300 font-mono">{String(v)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </CardBody>
          </Card>

          {/* Preview Form */}
          <Card>
            <CardHeader title="Preview Council Decision" icon={<Brain size={14} />} />
            <CardBody className="space-y-3">
              <div className="flex flex-wrap gap-3">
                <div className="flex-1 min-w-40">
                  <label className="block text-xs text-gray-400 mb-1 uppercase tracking-wider">Symbols</label>
                  <input
                    value={councilSymbols}
                    onChange={(e) => setCouncilSymbols(e.target.value)}
                    className="w-full bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-2 py-1.5 font-mono focus:outline-none focus:border-brand-purple"
                  />
                </div>
                <div>
                  <label className="block text-xs text-gray-400 mb-1 uppercase tracking-wider">Regime</label>
                  <select
                    value={councilRegime}
                    onChange={(e) => setCouncilRegime(e.target.value)}
                    className="bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-2 py-1.5 font-mono focus:outline-none focus:border-brand-purple"
                  >
                    {['NEUTRAL', 'BULLISH', 'BEARISH', 'VOLATILE', 'TRENDING_UP', 'TRENDING_DOWN'].map(r => (
                      <option key={r} value={r}>{r}</option>
                    ))}
                  </select>
                </div>
                <div className="self-end">
                  <button
                    onClick={handleCouncilPreview}
                    disabled={councilLoading}
                    className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-purple/15 border border-brand-purple/30 text-brand-purple text-xs font-medium hover:bg-brand-purple/25 disabled:opacity-50 transition-colors"
                  >
                    {councilLoading ? <Loader2 size={12} className="animate-spin" /> : <Brain size={12} />}
                    Run Preview
                  </button>
                </div>
              </div>

              {councilResult && (
                <div className="space-y-3 border-t border-surface-border pt-3">
                  <div className="flex items-center gap-3 flex-wrap">
                    <Tag
                      label={councilResult.action}
                      color={
                        councilResult.action.includes('BUY') ? 'green'
                        : councilResult.action.includes('SELL') ? 'red'
                        : 'gray'
                      }
                    />
                    <span className="text-xs font-mono text-gray-300">
                      Conf: {(councilResult.confidence * 100).toFixed(0)}%
                    </span>
                    <span className="text-xs font-mono text-gray-400">
                      Consensus: {(councilResult.consensus_score * 100).toFixed(0)}%
                    </span>
                    {councilResult.debate_triggered && <Tag label="Debate Triggered" color="yellow" />}
                    {councilResult.risk_vetoed && <Tag label="Risk Vetoed" color="red" />}
                  </div>
                  <Table<AgentVote>
                    columns={[
                      { key: 'agent', label: 'Agent' },
                      { key: 'action', label: 'Action', render: (v) => (
                        <span className={String(v).includes('BUY') ? 'text-brand-green' : String(v).includes('SELL') ? 'text-brand-red' : 'text-gray-400'}>
                          {String(v)}
                        </span>
                      )},
                      { key: 'confidence', label: 'Confidence', align: 'right', render: (v) => (
                        <span className={clsx('font-mono', Number(v) > 0.7 ? 'text-brand-green' : 'text-gray-300')}>
                          {(Number(v) * 100).toFixed(0)}%
                        </span>
                      )},
                      { key: 'reasoning', label: 'Reasoning', render: (v) => (
                        <span className="text-xs text-gray-500 truncate max-w-xs">{String(v).slice(0, 80)}</span>
                      )},
                    ]}
                    rows={councilResult.votes}
                    keyFn={(r, i) => `${r.agent_name ?? r.agent ?? 'agent'}-${i}`}
                    emptyMessage="No votes"
                    compact
                  />
                </div>
              )}
            </CardBody>
          </Card>

          {/* Decision History */}
          <Card>
            <CardHeader title="Decision History" subtitle={`${aiCouncilDecisions.length} recent`} />
            <Table
              columns={[
                { key: 'trace_id', label: 'Trace ID', render: (v) => <span className="text-xs font-mono text-brand-purple">{String(v).slice(0, 12)}</span> },
                { key: 'action', label: 'Action', render: (v) => (
                  <Tag
                    label={String(v)}
                    color={String(v).includes('BUY') ? 'green' : String(v).includes('SELL') ? 'red' : 'gray'}
                  />
                )},
                { key: 'confidence', label: 'Conf', align: 'right', render: (v) => `${(Number(v)*100).toFixed(0)}%` },
                { key: 'timestamp', label: 'Time', render: (v) => <span className="text-xs text-gray-500">{fmtDateTime(String(v))}</span> },
              ]}
              rows={aiCouncilDecisions as unknown as Record<string, unknown>[]}
              keyFn={(r) => String(r.trace_id)}
              emptyMessage="No decision history"
              compact
            />
          </Card>
        </div>
      )}

      {/* Tab: Neural Lab */}
      {tab === 'neural' && (
        <div className="space-y-4">
          <Card>
            <CardHeader title="Neural Lab Status" icon={<Zap size={14} className="text-brand-blue" />} />
            <CardBody>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                {neuralStatus?.models ? Object.entries(neuralStatus.models).map(([k, v]) => (
                  <div key={k} className="bg-surface-elevated rounded p-2">
                    <div className="text-[10px] text-gray-500 uppercase capitalize">{k}</div>
                    <div className="text-xs font-mono text-gray-200 mt-1">{String(v)}</div>
                  </div>
                )) : (
                  <div className="col-span-4 text-xs text-gray-500 text-center py-2">No neural status</div>
                )}
              </div>
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Predict Preview" icon={<Zap size={14} />} />
            <CardBody className="space-y-3">
              <div className="flex flex-wrap items-center gap-3">
                <input
                  value={neuralSymbols}
                  onChange={(e) => setNeuralSymbols(e.target.value)}
                  placeholder="Symbols"
                  className="flex-1 bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-2 py-1.5 font-mono focus:outline-none focus:border-brand-blue"
                />
                <button
                  onClick={handleNeuralPreview}
                  disabled={neuralLoading}
                  className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-blue/15 border border-brand-blue/30 text-brand-blue text-xs font-medium hover:bg-brand-blue/25 disabled:opacity-50 transition-colors"
                >
                  {neuralLoading ? <Loader2 size={12} className="animate-spin" /> : <Zap size={12} />}
                  Run Prediction
                </button>
              </div>

              {latestNeuralBundle && (
                <div className="space-y-3 border-t border-surface-border pt-3">
                  <div className="flex items-center gap-3 flex-wrap">
                    <Tag
                      label={neuralShouldTrade ? 'TRADE' : 'SKIP'}
                      color={neuralShouldTrade ? 'green' : 'red'}
                    />
                    <span className="text-xs text-gray-300 font-mono">
                      Uncertainty: {(latestNeuralBundle.overall_uncertainty * 100).toFixed(1)}%
                    </span>
                  </div>
                  {/* Uncertainty gauge */}
                  <div className="h-32">
                    <ResponsiveContainer width="100%" height="100%">
                      <RadialBarChart
                        cx="50%" cy="50%"
                        innerRadius="60%" outerRadius="90%"
                        data={[{ name: 'Uncertainty', value: latestNeuralBundle.overall_uncertainty * 100, fill: '#58a6ff' }]}
                        startAngle={180} endAngle={0}
                      >
                        <RadialBar dataKey="value" cornerRadius={4} />
                        <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v: number) => [`${v.toFixed(1)}%`, 'Uncertainty']} />
                      </RadialBarChart>
                    </ResponsiveContainer>
                  </div>
                  <Table<ForecastPrediction>
                    columns={[
                      { key: 'symbol', label: 'Symbol', render: (v) => <span className="text-brand-blue">{String(v)}</span> },
                      { key: 'direction_probability', label: 'Direction', align: 'right', render: (v) => (
                        <span className={clsx('font-mono font-bold', Number(v) > 0.6 ? 'text-brand-green' : Number(v) < 0.4 ? 'text-brand-red' : 'text-brand-yellow')}>
                          {(Number(v) * 100).toFixed(1)}%
                        </span>
                      )},
                      { key: 'expected_return', label: 'Expected Return', align: 'right', render: (v) => (
                        <span className={clsx('font-mono', Number(v) > 0 ? 'text-brand-green' : 'text-brand-red')}>
                          {(Number(v) * 100).toFixed(2)}%
                        </span>
                      )},
                      { key: 'model_uncertainty', label: 'Uncertainty', align: 'right', render: (v) => (
                        <span className="font-mono text-gray-400">{(Number(v) * 100).toFixed(1)}%</span>
                      )},
                    ]}
                    rows={latestNeuralBundle.forecasts}
                    keyFn={(r) => r.symbol}
                    emptyMessage="No forecasts"
                    compact
                  />
                </div>
              )}
            </CardBody>
          </Card>
        </div>
      )}

      {/* Tab: Quantum */}
      {tab === 'quantum' && (
        <div className="space-y-4">
          <Card>
            <CardHeader title="Quantum Status" icon={<Cpu size={14} className="text-brand-cyan" />} />
            <CardBody>
              <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                {[
                  { label: 'Enabled', value: String(quantumStatus?.enabled ?? false) },
                  { label: 'Backend', value: quantumStatus?.backend ?? '—' },
                  { label: 'Timeout', value: `${quantumStatus?.timeout_seconds ?? '—'}s` },
                ].map((s) => (
                  <div key={s.label} className="bg-surface-elevated rounded p-2">
                    <div className="text-[10px] text-gray-500 uppercase">{s.label}</div>
                    <div className="text-sm font-bold font-mono text-gray-100 mt-1">{s.value}</div>
                  </div>
                ))}
              </div>
              {quantumStatus?.backends && (
                <div className="mt-3 pt-3 border-t border-surface-border">
                  <div className="text-xs text-gray-400 mb-2">Available Backends</div>
                  <div className="flex flex-wrap gap-2">
                    {quantumStatus.backends.map((b) => (
                      <div key={b.name} className="flex items-center gap-1.5">
                        <span className={clsx('w-1.5 h-1.5 rounded-full', b.available ? 'bg-brand-green' : 'bg-gray-600')} />
                        <span className="text-xs font-mono text-gray-300">{b.name}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Optimize Preview" icon={<Cpu size={14} />} />
            <CardBody className="space-y-3">
              <div>
                <label className="block text-xs text-gray-400 mb-1 uppercase tracking-wider">Candidates (JSON array)</label>
                <textarea
                  value={quantumCandidates}
                  onChange={(e) => setQuantumCandidates(e.target.value)}
                  rows={3}
                  placeholder='["NIFTY", "BANKNIFTY"]'
                  className="w-full bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-2 py-1.5 font-mono focus:outline-none focus:border-brand-cyan resize-none"
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs text-gray-400 mb-1 uppercase tracking-wider">
                    Risk Aversion: {quantumRiskAversion.toFixed(2)}
                  </label>
                  <input
                    type="range" min={0} max={1} step={0.05}
                    value={quantumRiskAversion}
                    onChange={(e) => setQuantumRiskAversion(Number(e.target.value))}
                    className="w-full"
                  />
                </div>
                <div>
                  <label className="block text-xs text-gray-400 mb-1 uppercase tracking-wider">Cardinality</label>
                  <input
                    type="number" min={1} max={20}
                    value={quantumCardinality}
                    onChange={(e) => setQuantumCardinality(Number(e.target.value))}
                    className="w-full bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-2 py-1.5 font-mono focus:outline-none focus:border-brand-cyan"
                  />
                </div>
              </div>
              <button
                onClick={handleQuantumPreview}
                disabled={quantumLoading}
                className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-cyan/15 border border-brand-cyan/30 text-brand-cyan text-xs font-medium hover:bg-brand-cyan/25 disabled:opacity-50 transition-colors"
              >
                {quantumLoading ? <Loader2 size={12} className="animate-spin" /> : <Cpu size={12} />}
                Run Optimization
              </button>

              {latestQuantumResult && (
                <div className="space-y-3 border-t border-surface-border pt-3">
                  <div className="flex flex-wrap gap-2">
                    {latestQuantumResult.selected_symbols.map((s) => (
                      <Tag key={s} label={s} color="cyan" />
                    ))}
                  </div>
                  <div className="grid grid-cols-3 gap-2">
                    {[
                      { label: 'Backend', value: latestQuantumResult.backend_used },
                      { label: 'Objective', value: latestQuantumResult.objective_value.toFixed(4) },
                      { label: 'Improvement', value: latestQuantumResult.improvement_over_classical != null
                        ? `${(latestQuantumResult.improvement_over_classical * 100).toFixed(1)}%` : 'N/A' },
                    ].map((s) => (
                      <div key={s.label} className="bg-surface-elevated rounded p-2">
                        <div className="text-[10px] text-gray-500 uppercase">{s.label}</div>
                        <div className="text-xs font-bold font-mono text-brand-cyan mt-1">{s.value}</div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </CardBody>
          </Card>

          {/* History */}
          {quantumHistory.length > 0 && (
            <Card>
              <CardHeader title="Recent Results" subtitle={`${quantumHistory.length} results`} />
              <Table
                columns={[
                  { key: 'trace_id', label: 'Trace ID', render: (v) => <span className="text-xs font-mono text-brand-cyan">{String(v).slice(0, 12)}</span> },
                  { key: 'backend_used', label: 'Backend' },
                  { key: 'selected_symbols', label: 'Selected', render: (v) => (
                    <span className="text-xs text-gray-400">{(v as string[])?.join(', ') ?? '—'}</span>
                  )},
                  { key: 'objective_value', label: 'Objective', align: 'right', render: (v) => Number(v).toFixed(4) },
                ]}
                rows={quantumHistory}
                keyFn={(r) => String(r.trace_id ?? Math.random())}
                emptyMessage="No history"
                compact
              />
            </Card>
          )}
        </div>
      )}

      {/* Tab: Orchestrator (pgvector Learning State) */}
      {tab === 'orchestrator' && (
        <div className="space-y-4">

          {/* Header row */}
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Database size={14} className="text-brand-purple" />
              <span className="text-sm font-medium text-gray-200">pgvector Learning State</span>
              {orchStats && (
                <span className="text-[10px] px-2 py-0.5 rounded-full bg-green-500/10 border border-green-500/30 text-green-400 font-mono">
                  pgvector LIVE
                </span>
              )}
            </div>
            <button
              onClick={loadOrchestrator}
              disabled={orchLoading}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-surface-elevated border border-surface-border text-xs text-gray-400 hover:text-gray-200 transition-colors"
            >
              {orchLoading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
              Refresh
            </button>
          </div>

          {/* Top stat pills */}
          {orchStats && (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <div className="bg-surface-elevated rounded-lg p-3 border border-surface-border">
                <div className="text-[10px] text-gray-500 uppercase mb-1">Pipeline Cycles</div>
                <div className="text-lg font-mono text-gray-100">
                  {String((orchStats as Record<string, unknown>).cycle_count ?? 0)}
                </div>
              </div>
              <div className="bg-surface-elevated rounded-lg p-3 border border-surface-border">
                <div className="text-[10px] text-gray-500 uppercase mb-1">RAG Patterns</div>
                <div className="text-lg font-mono text-gray-100">
                  {String(((orchStats.market_rag as Record<string, unknown>)?.pattern_count ?? 0))}
                </div>
              </div>
              <div className="bg-surface-elevated rounded-lg p-3 border border-surface-border">
                <div className="text-[10px] text-gray-500 uppercase mb-1">RAG Avg Win Rate</div>
                <div className="text-lg font-mono text-brand-green">
                  {(Number(((orchStats.market_rag as Record<string, unknown>)?.avg_win_rate ?? 0)) * 100).toFixed(1)}%
                </div>
              </div>
              <div className="bg-surface-elevated rounded-lg p-3 border border-surface-border">
                <div className="text-[10px] text-gray-500 uppercase mb-1">Total Reflections</div>
                <div className="text-lg font-mono text-gray-100">
                  {String(((orchStats.reflection as Record<string, unknown>)?.total_reflections ?? 0))}
                </div>
              </div>
            </div>
          )}

          {/* MarketRAG Card */}
          <Card>
            <CardHeader
              title="MarketRAG — Corrective RAG Pattern Store"
              icon={<Layers size={14} className="text-brand-purple" />}
            />
            <CardBody className="space-y-3">
              {orchStats ? (
                <>
                  <div className="flex flex-wrap gap-3 text-xs text-gray-400">
                    <span>Queries: <span className="font-mono text-gray-200">
                      {String((orchStats.market_rag as Record<string, unknown>)?.query_count ?? 0)}
                    </span></span>
                    <span>Seeded: <span className="font-mono text-gray-200">
                      {String((orchStats.market_rag as Record<string, unknown>)?.seeded ?? false)}
                    </span></span>
                  </div>
                  <div className="text-xs text-brand-purple bg-brand-purple/5 border border-brand-purple/20 rounded p-2">
                    Pattern retrieval uses pgvector IVFFlat cosine index — sub-millisecond at any scale.
                    Patterns persist across restarts; learned win rates compound over every trade.
                  </div>
                </>
              ) : (
                <div className="text-xs text-gray-500">No data — start the agent to populate patterns.</div>
              )}
            </CardBody>
          </Card>

          {/* ProfitGuard Card */}
          <Card>
            <CardHeader
              title="ProfitGuard — Rolling Win Rate (persisted)"
              icon={<ShieldCheck size={14} className="text-brand-green" />}
            />
            <CardBody className="space-y-3">
              {/* Global stats */}
              {orchPG && (
                <div className="flex flex-wrap gap-4 text-xs">
                  <div>
                    <span className="text-gray-500">Global win rate: </span>
                    <span className="font-mono text-brand-green">
                      {orchPG.global_win_rate != null
                        ? `${(Number(orchPG.global_win_rate) * 100).toFixed(1)}%`
                        : '—'}
                    </span>
                  </div>
                  <div>
                    <span className="text-gray-500">Total trades: </span>
                    <span className="font-mono text-gray-200">{String(orchPG.total_trades ?? 0)}</span>
                  </div>
                </div>
              )}
              {/* Per-underlying lookup */}
              <div className="flex items-center gap-2">
                <input
                  value={orchUnderlying}
                  onChange={e => setOrchUnderlying(e.target.value.toUpperCase())}
                  placeholder="Underlying (e.g. NIFTY)"
                  className="flex-1 bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-2 py-1.5 font-mono focus:outline-none focus:border-brand-green"
                />
                <button
                  onClick={() => getOrchestratorProfitGuard(orchUnderlying).then(setOrchPG).catch(() => {})}
                  className="px-3 py-1.5 rounded bg-brand-green/15 border border-brand-green/30 text-brand-green text-xs font-medium hover:bg-brand-green/25 transition-colors"
                >
                  Lookup
                </button>
              </div>
              {/* Per-underlying table from full stats */}
              {orchPG?.per_underlying && Object.keys(orchPG.per_underlying as Record<string, unknown>).length > 0 ? (
                <Table
                  columns={[
                    { key: 'underlying', label: 'Underlying' },
                    { key: 'rolling_win_rate', label: 'Rolling Win %', render: (v) =>
                      v != null ? (
                        <span className={`font-mono ${Number(v) >= 0.5 ? 'text-brand-green' : 'text-red-400'}`}>
                          {(Number(v) * 100).toFixed(1)}%
                        </span>
                      ) : <span className="text-gray-500">—</span>
                    },
                    { key: 'consecutive_losses', label: 'Consec. Losses', render: (v) =>
                      <span className={`font-mono ${Number(v) >= 3 ? 'text-red-400' : 'text-gray-300'}`}>{String(v)}</span>
                    },
                  ]}
                  rows={Object.entries(orchPG.per_underlying as Record<string, Record<string, unknown>>).map(([k, v]) => ({
                    underlying: k,
                    rolling_win_rate: v.rolling_win_rate,
                    consecutive_losses: v.consecutive_losses ?? 0,
                  }))}
                  keyFn={(r) => r.underlying}
                  emptyMessage="No per-underlying history yet"
                />
              ) : orchPG?.underlying ? (
                <div className="flex flex-wrap gap-4 text-xs border-t border-surface-border pt-3">
                  <div><span className="text-gray-500">Underlying: </span><span className="font-mono text-gray-200">{String(orchPG.underlying)}</span></div>
                  <div><span className="text-gray-500">Rolling win rate: </span>
                    <span className={`font-mono ${orchPG.rolling_win_rate != null && Number(orchPG.rolling_win_rate) >= 0.5 ? 'text-brand-green' : 'text-red-400'}`}>
                      {orchPG.rolling_win_rate != null ? `${(Number(orchPG.rolling_win_rate) * 100).toFixed(1)}%` : '—'}
                    </span>
                  </div>
                  <div><span className="text-gray-500">Consecutive losses: </span>
                    <span className={`font-mono ${Number(orchPG.consecutive_losses ?? 0) >= 3 ? 'text-red-400' : 'text-gray-300'}`}>
                      {String(orchPG.consecutive_losses ?? 0)}
                    </span>
                  </div>
                </div>
              ) : (
                <div className="text-xs text-gray-500">No trades recorded yet. Rolling state persists across restarts once trades close.</div>
              )}
            </CardBody>
          </Card>

          {/* ReflectionEngine — Agent Weights */}
          <Card>
            <CardHeader
              title="Reflection Engine — Agent Weights (persisted)"
              icon={<TrendingUp size={14} className="text-brand-yellow" />}
            />
            <CardBody className="space-y-3">
              {orchStats && (orchStats.reflection as Record<string, unknown>)?.agent_weights &&
               Object.keys((orchStats.reflection as Record<string, unknown>).agent_weights as Record<string, unknown>).length > 0 ? (
                <Table
                  columns={[
                    { key: 'agent', label: 'Agent' },
                    { key: 'weight', label: 'Weight', render: (v) => (
                      <div className="flex items-center gap-2">
                        <div className="w-20 h-1.5 rounded bg-surface-border overflow-hidden">
                          <div
                            className="h-full rounded bg-brand-yellow"
                            style={{ width: `${Math.min(100, (Number(v) / 2.5) * 100)}%` }}
                          />
                        </div>
                        <span className="font-mono text-gray-200">{Number(v).toFixed(3)}</span>
                      </div>
                    )},
                    { key: 'accuracy', label: 'Accuracy', render: (v) =>
                      v != null ? (
                        <span className={`font-mono text-xs ${Number(v) >= 0.5 ? 'text-brand-green' : 'text-red-400'}`}>
                          {(Number(v) * 100).toFixed(1)}%
                        </span>
                      ) : <span className="text-gray-500">—</span>
                    },
                  ]}
                  rows={Object.entries(
                    (orchStats.reflection as Record<string, unknown>).agent_weights as Record<string, number>
                  ).map(([agent, weight]) => {
                    const acc = ((orchStats.reflection as Record<string, unknown>).agent_accuracy as Record<string, Record<string, unknown>>)?.[agent]
                    return { agent, weight, accuracy: acc?.accuracy }
                  })}
                  keyFn={(r) => r.agent}
                  emptyMessage="No agent weights yet"
                />
              ) : (
                <div className="text-xs text-gray-500">
                  Weights accumulate as trades close. They persist across restarts via pgvector DB.
                </div>
              )}

              {/* Overall win rate */}
              {orchStats && (
                <div className="flex flex-wrap gap-4 text-xs border-t border-surface-border pt-3">
                  <div>
                    <span className="text-gray-500">Overall win rate: </span>
                    <span className="font-mono text-brand-green">
                      {(orchStats.reflection as Record<string, unknown>).overall_win_rate != null
                        ? `${(Number((orchStats.reflection as Record<string, unknown>).overall_win_rate) * 100).toFixed(1)}%`
                        : '—'}
                    </span>
                  </div>
                </div>
              )}
            </CardBody>
          </Card>

          {/* Recent Reflections from DB */}
          <Card>
            <CardHeader
              title="Recent Reflections (from DB — survive restarts)"
              icon={<Database size={14} className="text-brand-blue" />}
            />
            <CardBody>
              {orchDBReflections.length > 0 ? (
                <Table
                  columns={[
                    { key: 'ts', label: 'Time', render: (v) => <span className="font-mono text-[10px] text-gray-400">{String(v).slice(0, 19).replace('T', ' ')}</span> },
                    { key: 'underlying', label: 'Symbol' },
                    { key: 'won', label: 'Result', render: (v) => (
                      <Tag label={v ? 'WIN' : 'LOSS'} color={v ? 'green' : 'red'} />
                    )},
                    { key: 'pnl_pct', label: 'P&L %', render: (v) => (
                      <span className={`font-mono ${Number(v) >= 0 ? 'text-brand-green' : 'text-red-400'}`}>
                        {(Number(v) * 100).toFixed(2)}%
                      </span>
                    )},
                    { key: 'quality', label: 'Quality', render: (v) => (
                      <span className="font-mono text-gray-300">{Number(v).toFixed(2)}</span>
                    )},
                    { key: 'regime', label: 'Regime', render: (v) => (
                      <span className="text-[10px] text-gray-400">{String(v ?? '—')}</span>
                    )},
                  ]}
                  rows={orchDBReflections}
                  keyFn={(_, i) => String(i)}
                  emptyMessage="No reflections in DB yet"
                />
              ) : orchReflections.length > 0 ? (
                <Table
                  columns={[
                    { key: 'ts', label: 'Time', render: (v) => <span className="font-mono text-[10px] text-gray-400">{String(v).slice(0, 19).replace('T', ' ')}</span> },
                    { key: 'underlying', label: 'Symbol' },
                    { key: 'won', label: 'Result', render: (v) => (
                      <Tag label={v ? 'WIN' : 'LOSS'} color={v ? 'green' : 'red'} />
                    )},
                    { key: 'pnl_pct', label: 'P&L %', render: (v) => (
                      <span className={`font-mono ${Number(v) >= 0 ? 'text-brand-green' : 'text-red-400'}`}>
                        {(Number(v) * 100).toFixed(2)}%
                      </span>
                    )},
                    { key: 'quality_score', label: 'Quality', render: (v) => (
                      <span className="font-mono text-gray-300">{Number(v).toFixed(2)}</span>
                    )},
                    { key: 'regime', label: 'Regime', render: (v) => (
                      <span className="text-[10px] text-gray-400">{String(v ?? '—')}</span>
                    )},
                  ]}
                  rows={orchReflections}
                  keyFn={(_, i) => String(i)}
                  emptyMessage="No reflections yet"
                />
              ) : (
                <div className="text-xs text-gray-500">No reflections yet. Reflections are written after each trade closes.</div>
              )}
            </CardBody>
          </Card>

        </div>
      )}

      {/* Tab: MARL */}
      {tab === 'marl' && (
        <div className="space-y-4">
          <Card>
            <CardHeader title="MARL Advisory Preview" icon={<GitMerge size={14} className="text-brand-blue" />} />
            <CardBody className="space-y-3">
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                {[
                  { label: 'Portfolio P&L', key: 'portfolio_pnl' },
                  { label: 'Open Positions', key: 'open_positions' },
                  { label: 'Drawdown', key: 'drawdown' },
                ].map(({ label, key }) => (
                  <div key={key}>
                    <label className="block text-xs text-gray-400 mb-1 uppercase tracking-wider">{label}</label>
                    <input
                      type="number"
                      value={marlForm[key as keyof typeof marlForm]}
                      onChange={(e) => setMarlForm(f => ({ ...f, [key]: Number(e.target.value) }))}
                      className="w-full bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-2 py-1.5 font-mono focus:outline-none focus:border-brand-blue"
                    />
                  </div>
                ))}
              </div>
              <button
                onClick={async () => {
                  setMarlLoading(true)
                  try {
                    const res = await runMarlAdvisoryPreview(marlForm)
                    setMarlResult(res)
                  } catch { /* ignore */ }
                  finally { setMarlLoading(false) }
                }}
                disabled={marlLoading}
                className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-blue/15 border border-brand-blue/30 text-brand-blue text-xs font-medium hover:bg-brand-blue/25 disabled:opacity-50 transition-colors"
              >
                {marlLoading ? <Loader2 size={12} className="animate-spin" /> : <GitMerge size={12} />}
                Run Advisory Preview
              </button>

              {marlResult && (
                <div className="space-y-3 border-t border-surface-border pt-3">
                  <div className="flex items-center gap-3">
                    <Tag
                      label={String(marlResult.majority?.action_label ?? marlResult.majority_action_label ?? 'NOOP')}
                      color={
                        String(marlResult.majority?.action_label ?? marlResult.majority_action_label).includes('ENTRY') ? 'green'
                        : String(marlResult.majority?.action_label ?? marlResult.majority_action_label).includes('EXIT') ? 'red'
                        : 'gray'
                      }
                    />
                    <span className="text-xs font-mono text-gray-300">
                      Confidence: {(Number(marlResult.majority?.confidence ?? marlResult.majority_confidence ?? 0) * 100).toFixed(0)}%
                    </span>
                  </div>
                  <div className="text-xs text-brand-yellow bg-brand-yellow/5 border border-brand-yellow/20 rounded p-2">
                    Advisory only — MARL policies never submit live orders directly
                  </div>
                </div>
              )}
            </CardBody>
          </Card>
        </div>
      )}

    </div>
  )
}
