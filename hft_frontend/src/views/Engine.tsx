import { useState, useEffect, useCallback, useRef } from 'react'
import {
  Play, Square, Activity, Shield, Cpu, Bot, AlertTriangle,
  Atom, Loader2, DollarSign, Eye, Layers, CheckCircle, XCircle,
  Clock, TrendingUp, TrendingDown,
} from 'lucide-react'
import { clsx } from 'clsx'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Table } from '../components/shared/Table'
import { MetricCard } from '../components/shared/MetricCard'
import { execModeBadge, Tag } from '../components/shared/Badge'
import { useStore } from '../store'
import {
  getAgentStatus, startAgent, stopAgent, setAgentInterval,
  getBatchTicks, getSchedulerStats, getOmsEvents,
  getPortfolioPositions, getRiskRejections,
  getGovernanceDashboard, getActiveExitPlans,
  getOptionChain, getOptionExpiries, calculateGreeks,
  getManualApprovals, approveManualApproval, rejectManualApproval,
  setExecutionMode, runHighEndScan, getAgentTrades,
  getFeedSnapshot, startFeed, stopFeed,
} from '../api'
import { inr, pct, fmtDateTime } from '../utils'

const FALLBACK_NSE_INDICES   = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY']
const FALLBACK_BSE_INDICES   = ['SENSEX', 'BANKEX']
const FALLBACK_CORE_EQUITIES = ['RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 'SBIN']
const FALLBACK_EXT_EQUITIES  = ['WIPRO', 'KOTAKBANK', 'AXISBANK', 'MARUTI', 'SUNPHARMA',
                                'TATAMOTORS', 'BAJFINANCE', 'HINDUNILVR', 'BHARTIARTL', 'NTPC']

const EXEC_MODES = ['BACKTEST', 'PAPER', 'SHADOW_LIVE', 'LIVE']

const TABS = [
  { id: 'agent',       label: 'Agent',      icon: <Bot size={13} /> },
  { id: 'market',      label: 'Live Market', icon: <Activity size={13} /> },
  { id: 'portfolio',   label: 'Portfolio',   icon: <Layers size={13} /> },
  { id: 'orders',      label: 'Orders & Risk', icon: <DollarSign size={13} /> },
  { id: 'governance',  label: 'Governance',  icon: <Shield size={13} /> },
  { id: 'derivatives', label: 'Derivatives', icon: <Cpu size={13} /> },
]

type Tick = {
  ltp?: number
  last_price?: number
  close?: number
  change?: number
  volume?: number
  live?: boolean
  available?: boolean
  timestamp?: string
}
type Position = {
  symbol: string; quantity: number; side: string; average_price: number
  mark_price: number; unrealized_pnl: number; pnl_pct: number; live: boolean
}

function finiteNumber(value: unknown): number | null {
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function agentTradePrice(row: Record<string, unknown>): number | null {
  return finiteNumber(row.price ?? row.exit_price ?? row.entry_price)
}

function agentTradeTime(row: Record<string, unknown>): string | null {
  const raw = row.timestamp ?? row.ts
  if (!raw) return null
  const d = new Date(String(raw))
  return Number.isNaN(d.getTime()) ? null : String(raw)
}

function agentTradeStrategy(row: Record<string, unknown>): string {
  return String(row.strategy_name ?? row.strategy ?? row.type ?? '—')
}

function tickPrice(t: Tick | undefined): number | null {
  if (!t || t.available === false) return null
  return finiteNumber(t.ltp ?? t.last_price)
}

function tickChangePct(t: Tick | undefined): number | null {
  if (!t || t.available === false) return null
  const explicit = finiteNumber(t.change)
  if (explicit !== null) return explicit

  const last = finiteNumber(t.last_price ?? t.ltp)
  const close = finiteNumber(t.close)
  if (last === null || close === null || close === 0) return null
  return ((last - close) / close) * 100
}

function tickAgeSeconds(t: Tick | undefined): number | null {
  if (!t?.timestamp) return null
  const time = new Date(t.timestamp).getTime()
  if (Number.isNaN(time)) return null
  return Math.max(0, Math.round((Date.now() - time) / 1000))
}

function tickStatus(t: Tick | undefined): { label: string; className: string } {
  if (!t || t.available === false) {
    return { label: 'NOT SUBSCRIBED', className: 'text-gray-500 border-gray-700 bg-surface' }
  }

  const age = tickAgeSeconds(t)
  if (age !== null && age > 60) {
    return { label: 'STALE', className: 'text-brand-red border-brand-red/40 bg-brand-red/10' }
  }
  if (age !== null && age > 15) {
    return { label: `${age}s`, className: 'text-yellow-300 border-yellow-400/40 bg-yellow-400/10' }
  }
  return { label: age === null ? 'LIVE' : `${age}s`, className: 'text-brand-green border-brand-green/40 bg-brand-green/10' }
}

export function Engine() {
  const runtimeState    = useStore((s) => s.runtimeState)
  const monitoring      = useStore((s) => s.monitoring)
  const aiCouncilStatus = useStore((s) => s.aiCouncilStatus)
  const neuralStatus    = useStore((s) => s.neuralStatus)
  const quantumStatus   = useStore((s) => s.quantumStatus)

  const [tab, setTab]         = useState('agent')
  const [agentStatus, setAgentStatus] = useState<Record<string, unknown> | null>(null)
  const [agentTrades, setAgentTrades] = useState<Record<string, unknown>[]>([])
  const [feedSnapshot, setFeedSnapshot] = useState<Record<string, unknown> | null>(null)
  const [scanInterval, setScanInterval] = useState(300)
  const [agentLoading, setAgentLoading] = useState(false)
  const [feedControlLoading, setFeedControlLoading] = useState(false)

  // Market data
  const [ticks, setTicks] = useState<Record<string, Tick>>({})
  const [ticksLoading, setTicksLoading] = useState(false)
  const [lastTickTime, setLastTickTime] = useState<string>('')

  // Portfolio
  const livePortfolio = useStore((s) => s.livePortfolio)
  const [positions, setPositions] = useState<Position[]>([])

  // Orders & Risk
  const [schedulerStats, setSchedulerStats] = useState<Record<string, unknown> | null>(null)
  const [omsEvents, setOmsEvents] = useState<Record<string, unknown>[]>([])
  const [riskRejections, setRiskRejections] = useState<Record<string, unknown>[]>([])

  // Governance
  const [exitPlans, setExitPlans] = useState<Record<string, unknown>[]>([])
  const [governance, setGovernance] = useState<Record<string, unknown> | null>(null)
  const [approvals, setApprovals] = useState<Record<string, unknown>[]>([])

  // Derivatives
  const [optionUnderlying, setOptionUnderlying] = useState('NIFTY')
  const [optionExpiries, setOptionExpiries] = useState<string[]>([])
  const [selectedExpiry, setSelectedExpiry] = useState('')
  const [optionChain, setOptionChain] = useState<Record<string, unknown> | null>(null)
  const [greeksForm, setGreeksForm] = useState({
    spot: 24500, strike: 24500, dte: 7, vol: 0.15, type: 'CE',
  })
  const [greeksResult, setGreeksResult] = useState<Record<string, unknown> | null>(null)
  const [greeksLoading, setGreeksLoading] = useState(false)

  // AI Scan
  const [scanSymbols, setScanSymbols] = useState('NIFTY,BANKNIFTY')
  const [scanResult, setScanResult] = useState<Record<string, unknown> | null>(null)
  const [scanLoading, setScanLoading] = useState(false)

  // Mode change
  const [modeLoading, setModeLoading] = useState(false)

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const tickPollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const anyAiEnabled = aiCouncilStatus?.enabled || neuralStatus?.enabled || quantumStatus?.enabled

  // ── Agent tab ────────────────────────────────────────────────────────────────
  const loadAgentData = useCallback(async () => {
    const [statusRes, tradesRes, feedRes] = await Promise.allSettled([
      getAgentStatus(),
      getAgentTrades(20),
      getFeedSnapshot(),
    ])
    if (statusRes.status === 'fulfilled') setAgentStatus(statusRes.value)
    if (tradesRes.status === 'fulfilled') {
      const v = tradesRes.value as { trades?: Record<string, unknown>[] }
      setAgentTrades(v.trades ?? [])
    }
    if (feedRes.status === 'fulfilled') setFeedSnapshot(feedRes.value as Record<string, unknown>)
  }, [])

  // ── Market tab ───────────────────────────────────────────────────────────────
  const loadTicks = useCallback(async () => {
    const symbols = [...FALLBACK_NSE_INDICES, ...FALLBACK_BSE_INDICES, ...FALLBACK_CORE_EQUITIES, ...FALLBACK_EXT_EQUITIES]
    setTicksLoading(true)
    try {
      const res = await getBatchTicks(symbols, true)
      const merged: Record<string, Tick> = {}
      for (const sym of symbols) {
        const t = res.ticks[sym] as Tick | undefined
        merged[sym] = t ?? {}
      }
      setTicks(merged)
      setLastTickTime(new Date().toLocaleTimeString('en-IN'))
    } catch { /* ignore */ }
    finally { setTicksLoading(false) }
  }, [])

  // ── Orders tab ───────────────────────────────────────────────────────────────
  const loadOrders = useCallback(async () => {
    const [sched, oms, risk] = await Promise.allSettled([
      getSchedulerStats(),
      getOmsEvents(30),
      getRiskRejections(30),
    ])
    if (sched.status === 'fulfilled') setSchedulerStats(sched.value)
    if (oms.status === 'fulfilled') {
      const v = oms.value as { events?: Record<string, unknown>[] }
      setOmsEvents(v.events ?? [])
    }
    if (risk.status === 'fulfilled') {
      const v = risk.value as { rejections?: Record<string, unknown>[] }
      setRiskRejections(v.rejections ?? [])
    }
  }, [])

  // ── Governance tab ───────────────────────────────────────────────────────────
  const loadGovernance = useCallback(async () => {
    const [exits, gov, apps] = await Promise.allSettled([
      getActiveExitPlans(),
      getGovernanceDashboard(),
      getManualApprovals(),
    ])
    if (exits.status === 'fulfilled') {
      const v = exits.value as { exit_plans?: Record<string, unknown>[] }
      setExitPlans(v.exit_plans ?? [])
    }
    if (gov.status === 'fulfilled') setGovernance(gov.value)
    if (apps.status === 'fulfilled') {
      const v = apps.value as unknown as { pending?: Record<string, unknown>[] }
      setApprovals(v.pending ?? [])
    }
  }, [])

  // ── Portfolio tab ────────────────────────────────────────────────────────────
  const loadPortfolio = useCallback(async () => {
    try {
      const res = await getPortfolioPositions()
      const v = res as { positions?: Position[] }
      setPositions(v.positions ?? [])
    } catch { /* ignore */ }
  }, [])

  // ── Derivatives tab ──────────────────────────────────────────────────────────
  const loadExpiries = useCallback(async (underlying: string) => {
    try {
      const res = await getOptionExpiries(underlying)
      setOptionExpiries(res.expiries ?? [])
      if (res.expiries?.length) setSelectedExpiry(res.expiries[0])
    } catch { /* ignore */ }
  }, [])

  const loadOptionChain = useCallback(async () => {
    try {
      const res = await getOptionChain(optionUnderlying, selectedExpiry)
      setOptionChain(res)
    } catch { /* ignore */ }
  }, [optionUnderlying, selectedExpiry])

  useEffect(() => {
    loadAgentData()
    pollRef.current = setInterval(loadAgentData, 15_000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [loadAgentData])

  useEffect(() => {
    if (tab === 'market') {
      loadTicks()
      tickPollRef.current = setInterval(loadTicks, 10_000)
    }
    if (tab === 'portfolio') loadPortfolio()
    if (tab === 'orders') loadOrders()
    if (tab === 'governance') loadGovernance()
    if (tab === 'derivatives') loadExpiries(optionUnderlying)
    return () => { if (tickPollRef.current) { clearInterval(tickPollRef.current); tickPollRef.current = null } }
  }, [tab])

  useEffect(() => {
    if (tab === 'derivatives' && selectedExpiry) loadOptionChain()
  }, [tab, selectedExpiry, optionUnderlying])

  const handleStartAgent = async () => {
    setAgentLoading(true)
    try { await startAgent(scanInterval); await loadAgentData() }
    catch { /* ignore */ }
    finally { setAgentLoading(false) }
  }

  const handleStopAgent = async () => {
    setAgentLoading(true)
    try { await stopAgent(); await loadAgentData() }
    catch { /* ignore */ }
    finally { setAgentLoading(false) }
  }

  const handleStartPaperFeed = async () => {
    setFeedControlLoading(true)
    try { await startFeed(['NIFTY', 'BANKNIFTY', 'RELIANCE', 'SBIN']); await loadAgentData() }
    catch { /* ignore */ }
    finally { setFeedControlLoading(false) }
  }

  const handleStopPaperFeed = async () => {
    setFeedControlLoading(true)
    try { await stopFeed(); await loadAgentData() }
    catch { /* ignore */ }
    finally { setFeedControlLoading(false) }
  }

  const handleSetInterval = async () => {
    try { await setAgentInterval(scanInterval); await loadAgentData() }
    catch { /* ignore */ }
  }

  const handleModeChange = async (mode: string) => {
    setModeLoading(true)
    try { await setExecutionMode(mode) }
    catch { /* ignore */ }
    finally { setModeLoading(false) }
  }

  const handleAiScan = async () => {
    setScanLoading(true)
    try {
      const syms = scanSymbols.split(',').map(s => s.trim()).filter(Boolean)
      const res = await runHighEndScan({ symbols: syms })
      setScanResult(res as unknown as Record<string, unknown>)
    } catch { /* ignore */ }
    finally { setScanLoading(false) }
  }

  const handleApprove = async (reqId: string) => {
    try { await approveManualApproval(reqId, 'Approved via UI'); await loadGovernance() }
    catch { /* ignore */ }
  }

  const handleReject = async (reqId: string) => {
    try { await rejectManualApproval(reqId, 'Rejected via UI'); await loadGovernance() }
    catch { /* ignore */ }
  }

  const handleCalcGreeks = async () => {
    setGreeksLoading(true)
    try {
      const res = await calculateGreeks({
        spot_price: greeksForm.spot,
        strike_price: greeksForm.strike,
        days_to_expiry: greeksForm.dte,
        volatility: greeksForm.vol,
        option_type: greeksForm.type,
      })
      setGreeksResult(res)
    } catch { /* ignore */ }
    finally { setGreeksLoading(false) }
  }

  const agentRunning = Boolean(agentStatus?.running)
  const feedRunning = Boolean(feedSnapshot?.running)

  // Tick grid helper
  const TickCell = ({ sym }: { sym: string }) => {
    const t = ticks[sym]
    const price = tickPrice(t)
    const chg = tickChangePct(t)
    const status = tickStatus(t)
    return (
      <div className="flex items-center justify-between px-2 py-1.5 rounded bg-surface-elevated hover:bg-surface-border/30 transition-colors">
        <div className="min-w-0">
          <span className="block text-xs font-mono text-gray-300 truncate">{sym}</span>
          <span className={clsx('mt-0.5 inline-flex rounded border px-1 py-0.5 text-[9px] font-mono leading-none', status.className)}>
            {status.label}
          </span>
        </div>
        <div className="text-right">
          <div className="text-xs font-mono font-semibold text-gray-100">{price === null ? '—' : inr(price, 2)}</div>
          <div className={clsx('text-[10px] font-mono', (chg ?? 0) > 0 ? 'text-brand-green' : (chg ?? 0) < 0 ? 'text-brand-red' : 'text-gray-500')}>
            {chg !== null && chg !== 0 ? pct(chg) : '—'}
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">

      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Bot size={16} className="text-brand-blue" />
          <div>
            <h1 className="text-lg font-bold text-gray-100">Trading Engine</h1>
            <p className="text-xs text-gray-500 mt-0.5">Agent control · market data · portfolio · governance</p>
          </div>
        </div>
      </div>

      {/* ── Control Bar ─────────────────────────────────────────────────────── */}
      <Card>
        <CardBody className="!py-3">
          <div className="flex flex-wrap items-center gap-3">
            {/* Execution mode */}
            <div className="flex items-center gap-2">
              <span className="text-xs text-gray-500">Mode:</span>
              <select
                value={runtimeState?.execution_mode ?? 'PAPER'}
                onChange={(e) => handleModeChange(e.target.value)}
                disabled={modeLoading}
                className="bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-2 py-1 font-mono focus:outline-none focus:border-brand-blue"
              >
                {EXEC_MODES.map(m => <option key={m} value={m}>{m}</option>)}
              </select>
              {modeLoading && <Loader2 size={12} className="animate-spin text-gray-400" />}
            </div>

            {/* Agent toggle */}
            <div className="flex items-center gap-2">
              {agentRunning ? (
                <button
                  onClick={handleStopAgent}
                  disabled={agentLoading}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-brand-red/15 border border-brand-red/30 text-brand-red text-xs font-medium hover:bg-brand-red/25 transition-colors"
                >
                  {agentLoading ? <Loader2 size={12} className="animate-spin" /> : <Square size={12} />}
                  Stop Agent
                </button>
              ) : (
                <button
                  onClick={handleStartAgent}
                  disabled={agentLoading}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-brand-green/15 border border-brand-green/30 text-brand-green text-xs font-medium hover:bg-brand-green/25 transition-colors"
                >
                  {agentLoading ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
                  Start Agent
                </button>
              )}
              <span className={clsx('w-2 h-2 rounded-full', agentRunning ? 'bg-brand-green animate-pulse-slow' : 'bg-gray-600')} />
              <span className={clsx('text-xs font-mono', agentRunning ? 'text-brand-green' : 'text-gray-500')}>
                {agentRunning ? 'RUNNING' : 'STOPPED'}
              </span>
            </div>

            {/* Paper feed toggle */}
            <div className="flex items-center gap-2">
              {feedRunning ? (
                <button
                  onClick={handleStopPaperFeed}
                  disabled={feedControlLoading}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-brand-red/15 border border-brand-red/30 text-brand-red text-xs font-medium hover:bg-brand-red/25 transition-colors"
                >
                  {feedControlLoading ? <Loader2 size={12} className="animate-spin" /> : <Square size={12} />}
                  Stop Feed
                </button>
              ) : (
                <button
                  onClick={handleStartPaperFeed}
                  disabled={feedControlLoading}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-brand-blue/15 border border-brand-blue/30 text-brand-blue text-xs font-medium hover:bg-brand-blue/25 transition-colors"
                >
                  {feedControlLoading ? <Loader2 size={12} className="animate-spin" /> : <Activity size={12} />}
                  Start Feed
                </button>
              )}
              <span className={clsx('w-2 h-2 rounded-full', feedRunning ? 'bg-brand-green animate-pulse-slow' : 'bg-gray-600')} />
              <span className={clsx('text-xs font-mono', feedRunning ? 'text-brand-green' : 'text-gray-500')}>
                {feedRunning ? 'FEED LIVE' : 'FEED OFF'}
              </span>
            </div>

            {/* Kill switch & feed status */}
            <div className="ml-auto flex items-center gap-2">
              {monitoring && (
                <span className="hidden sm:block text-xs text-gray-500 font-mono">
                  Health: {monitoring.status}
                </span>
              )}
              {runtimeState?.kill_switch_active && (
                <span className="px-2 py-0.5 rounded bg-brand-red/15 border border-brand-red/30 text-brand-red text-xs font-mono font-bold">
                  KILL ACTIVE
                </span>
              )}
            </div>
          </div>
        </CardBody>
      </Card>

      {/* ── AI Scan Bar ──────────────────────────────────────────────────────── */}
      {anyAiEnabled && (
        <Card className="border-brand-purple/20 bg-brand-purple/5">
          <CardBody className="!py-3">
            <div className="flex flex-wrap items-center gap-3">
              <Atom size={14} className="text-brand-purple flex-shrink-0" />
              <span className="text-xs text-brand-purple font-semibold">AI Scan</span>
              <input
                value={scanSymbols}
                onChange={(e) => setScanSymbols(e.target.value)}
                placeholder="Symbols (comma-separated)"
                className="flex-1 min-w-32 bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-2 py-1 font-mono focus:outline-none focus:border-brand-purple"
              />
              <button
                onClick={handleAiScan}
                disabled={scanLoading}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-brand-purple/15 border border-brand-purple/30 text-brand-purple text-xs font-medium hover:bg-brand-purple/25 transition-colors"
              >
                {scanLoading ? <Loader2 size={12} className="animate-spin" /> : <Atom size={12} />}
                Full AI Scan
              </button>
              {scanResult && (
                <span className={clsx(
                  'px-2 py-0.5 rounded text-xs font-mono font-bold border',
                  String(scanResult.ensemble_action ?? '').includes('BUY') ? 'text-brand-green border-brand-green/30 bg-brand-green/10'
                  : String(scanResult.ensemble_action ?? '').includes('SELL') ? 'text-brand-red border-brand-red/30 bg-brand-red/10'
                  : 'text-gray-400 border-surface-border',
                )}>
                  {String(scanResult.ensemble_action ?? 'HOLD')} · {((Number(scanResult.ensemble_confidence ?? 0)) * 100).toFixed(0)}%
                </span>
              )}
            </div>
          </CardBody>
        </Card>
      )}

      {/* ── Tab System ──────────────────────────────────────────────────────── */}
      <div className="flex items-center gap-1 overflow-x-auto pb-1">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={clsx(
              'flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium whitespace-nowrap transition-colors',
              tab === t.id
                ? 'bg-brand-blue/15 text-brand-blue border border-brand-blue/30'
                : 'text-gray-400 hover:text-gray-200 hover:bg-surface-elevated',
            )}
          >
            {t.icon}
            {t.label}
          </button>
        ))}
      </div>

      {/* ── Tab 1: Agent ────────────────────────────────────────────────────── */}
      {tab === 'agent' && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <MetricCard label="Status" value={agentRunning ? 'RUNNING' : 'STOPPED'} color={agentRunning ? 'green' : 'gray'} />
            <MetricCard
              label="Scan Interval"
              value={`${Number(agentStatus?.scan_interval ?? scanInterval)}s`}
              color="blue"
            />
            <MetricCard
              label="Total Scans"
              value={String(agentStatus?.scan_count ?? 0)}
              color="cyan"
            />
            <MetricCard
              label="Enqueued"
              value={String(agentStatus?.enqueued_total ?? 0)}
              color="green"
            />
          </div>

          <Card>
            <CardHeader title="Interval Control" icon={<Clock size={14} />} />
            <CardBody className="!py-3">
              <div className="flex items-center gap-3">
                <span className="text-xs text-gray-400">Scan every:</span>
                {[30, 60, 300, 900].map((s) => (
                  <button
                    key={s}
                    onClick={() => setScanInterval(s)}
                    className={clsx(
                      'px-3 py-1 rounded text-xs font-mono border transition-colors',
                      scanInterval === s
                        ? 'bg-brand-blue/15 border-brand-blue/30 text-brand-blue'
                        : 'bg-surface-elevated border-surface-border text-gray-400 hover:text-gray-200',
                    )}
                  >
                    {s < 60 ? `${s}s` : `${s / 60}m`}
                  </button>
                ))}
                <button
                  onClick={handleSetInterval}
                  className="px-3 py-1 rounded text-xs bg-brand-green/15 border border-brand-green/30 text-brand-green hover:bg-brand-green/25 transition-colors"
                >
                  Apply
                </button>
              </div>
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Recent Agent Trades" subtitle={`${agentTrades.length} shown`} icon={<Bot size={14} />} />
            <Table
              columns={[
                { key: 'symbol', label: 'Symbol', render: (v) => <span className="text-brand-blue font-semibold">{String(v)}</span> },
                { key: 'side', label: 'Side', render: (v) => <span className={v === 'BUY' ? 'text-brand-green' : 'text-brand-red'}>{String(v)}</span> },
                { key: 'quantity', label: 'Qty', align: 'right', render: (v) => finiteNumber(v)?.toLocaleString('en-IN') ?? '—' },
                { key: 'strategy_name', label: 'Strategy', render: (_, row) => agentTradeStrategy(row as Record<string, unknown>) },
                { key: 'price', label: 'Price', align: 'right', render: (_, row) => {
                  const price = agentTradePrice(row as Record<string, unknown>)
                  return price == null ? '—' : inr(price, 2)
                } },
                { key: 'timestamp', label: 'Time', render: (_, row) => {
                  const ts = agentTradeTime(row as Record<string, unknown>)
                  return <span className="text-xs text-gray-500">{ts ? fmtDateTime(ts) : '—'}</span>
                } },
              ]}
              rows={agentTrades.slice(0, 20)}
              keyFn={(r, i) => String(r.trade_id ?? `${r.ts ?? r.timestamp ?? i}:${r.symbol ?? ''}:${r.type ?? ''}`)}
              emptyMessage="No agent trades yet"
              compact
            />
          </Card>
        </div>
      )}

      {/* ── Tab 2: Live Market ───────────────────────────────────────────────── */}
      {tab === 'market' && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <span className="text-xs text-gray-500">
              {ticksLoading ? <Loader2 size={12} className="animate-spin inline mr-1" /> : null}
              Last update: {lastTickTime || '—'}
            </span>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
            {[
              { label: 'NSE Indices',      symbols: FALLBACK_NSE_INDICES },
              { label: 'BSE Indices',      symbols: FALLBACK_BSE_INDICES },
              { label: 'Core Equities',    symbols: FALLBACK_CORE_EQUITIES },
              { label: 'Extended Equities', symbols: FALLBACK_EXT_EQUITIES },
            ].map((group) => (
              <Card key={group.label}>
                <CardHeader title={group.label} />
                <CardBody className="!p-2 space-y-1">
                  {group.symbols.map((sym) => <TickCell key={sym} sym={sym} />)}
                </CardBody>
              </Card>
            ))}
          </div>
        </div>
      )}

      {/* ── Tab 3: Portfolio ─────────────────────────────────────────────────── */}
      {tab === 'portfolio' && (
        <div className="space-y-4">
          {livePortfolio && (
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
              <MetricCard label="Equity"      value={inr(livePortfolio.portfolio.equity)}          color="blue"   />
              <MetricCard label="Cash"        value={inr(livePortfolio.portfolio.cash)}             color="cyan"   />
              <MetricCard label="Unrealized"  value={inr(livePortfolio.portfolio.unrealized_pnl)}  color={livePortfolio.portfolio.unrealized_pnl >= 0 ? 'green' : 'red'} />
              <MetricCard label="Realized"    value={inr(livePortfolio.portfolio.realized_pnl)}    color={livePortfolio.portfolio.realized_pnl >= 0 ? 'green' : 'red'} />
              <MetricCard label="Drawdown"    value={pct(livePortfolio.portfolio.drawdown * 100)}   color="yellow" />
            </div>
          )}

          <Card>
            <CardHeader title="Open Positions" subtitle={`${positions.length} positions`} />
            <Table
              columns={[
                { key: 'symbol', label: 'Symbol', render: (v) => <span className="text-brand-blue font-semibold">{String(v)}</span> },
                { key: 'side', label: 'Side', render: (v) => <span className={v === 'BUY' ? 'text-brand-green' : 'text-brand-red'}>{String(v)}</span> },
                { key: 'quantity', label: 'Qty', align: 'right' },
                { key: 'average_price', label: 'Avg Price', align: 'right', render: (v) => inr(Number(v), 2) },
                { key: 'mark_price', label: 'Mark Price', align: 'right', render: (v) => inr(Number(v), 2) },
                {
                  key: 'unrealized_pnl', label: 'Unrealized P&L', align: 'right',
                  render: (v) => (
                    <span className={Number(v) >= 0 ? 'text-brand-green' : 'text-brand-red'}>
                      {inr(Number(v), 2)}
                    </span>
                  ),
                },
                {
                  key: 'pnl_pct', label: 'P&L%', align: 'right',
                  render: (v) => (
                    <span className={Number(v) >= 0 ? 'text-brand-green' : 'text-brand-red'}>
                      {pct(Number(v))}
                    </span>
                  ),
                },
              ]}
              rows={positions as unknown as Record<string, unknown>[]}
              keyFn={(r) => String(r.symbol)}
              emptyMessage="No open positions"
              compact
            />
          </Card>
        </div>
      )}

      {/* ── Tab 4: Orders & Risk ─────────────────────────────────────────────── */}
      {tab === 'orders' && (
        <div className="space-y-4">
          {schedulerStats && (
            <Card>
              <CardHeader title="Scheduler Stats" icon={<Clock size={14} />} />
              <CardBody>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                  {[
                    { label: 'Queued', value: String(schedulerStats.queued_tasks ?? 0) },
                    { label: 'Processing', value: String(schedulerStats.active_tasks ?? 0) },
                    { label: 'Avg Latency', value: `${Number(schedulerStats.avg_latency_ms ?? 0).toFixed(0)}ms` },
                    { label: 'Fill Rate', value: `${Number(schedulerStats.fill_rate ?? 0).toFixed(1)}%` },
                  ].map((s) => (
                    <div key={s.label} className="bg-surface-elevated rounded-lg p-3">
                      <div className="text-[10px] text-gray-500 uppercase tracking-wider">{s.label}</div>
                      <div className="text-sm font-bold font-mono text-gray-100 mt-1">{s.value}</div>
                    </div>
                  ))}
                </div>
              </CardBody>
            </Card>
          )}

          <Card>
            <CardHeader title="OMS Events" subtitle="Recent orders" icon={<DollarSign size={14} />} />
            <Table
              columns={[
                { key: 'order_id', label: 'Order ID', render: (v) => <span className="text-xs font-mono text-gray-400">{String(v).slice(0, 12)}</span> },
                { key: 'symbol', label: 'Symbol', render: (v) => <span className="text-brand-blue">{String(v)}</span> },
                { key: 'event_type', label: 'Type' },
                { key: 'status', label: 'Status', render: (v) => (
                  <Tag
                    label={String(v)}
                    color={String(v) === 'FILLED' ? 'green' : String(v) === 'REJECTED' ? 'red' : 'yellow'}
                  />
                )},
                { key: 'timestamp', label: 'Time', render: (v) => <span className="text-xs text-gray-500">{fmtDateTime(String(v))}</span> },
              ]}
              rows={omsEvents.slice(0, 20)}
              keyFn={(r) => String(r.order_id ?? Math.random())}
              emptyMessage="No OMS events"
              compact
            />
          </Card>

          <Card>
            <CardHeader title="Risk Rejections" icon={<AlertTriangle size={14} />} />
            <Table
              columns={[
                { key: 'symbol', label: 'Symbol', render: (v) => <span className="text-brand-blue">{String(v)}</span> },
                { key: 'reason', label: 'Reason' },
                { key: 'timestamp', label: 'Time', render: (v) => <span className="text-xs text-gray-500">{fmtDateTime(String(v))}</span> },
              ]}
              rows={riskRejections.slice(0, 20)}
              keyFn={(r) => String(r.id ?? Math.random())}
              emptyMessage="No risk rejections"
              compact
            />
          </Card>
        </div>
      )}

      {/* ── Tab 5: Governance ────────────────────────────────────────────────── */}
      {tab === 'governance' && (
        <div className="space-y-4">
          {governance && (
            <Card>
              <CardHeader title="Governance Dashboard" icon={<Shield size={14} />} />
              <CardBody>
                <div className="grid grid-cols-2 md:grid-cols-3 gap-3 text-xs">
                  {Object.entries(governance).slice(0, 6).map(([k, v]) => (
                    <div key={k} className="bg-surface-elevated rounded p-2">
                      <div className="text-gray-500 capitalize">{k.replace(/_/g, ' ')}</div>
                      <div className="text-gray-200 font-mono mt-0.5">{String(v)}</div>
                    </div>
                  ))}
                </div>
              </CardBody>
            </Card>
          )}

          <Card>
            <CardHeader title="Active Exit Plans" subtitle={`${exitPlans.length} plans`} />
            <Table
              columns={[
                { key: 'symbol', label: 'Symbol' },
                { key: 'exit_reason', label: 'Reason' },
                { key: 'target_price', label: 'Target', align: 'right', render: (v) => v ? inr(Number(v), 2) : '—' },
                { key: 'created_at', label: 'Created', render: (v) => <span className="text-xs text-gray-500">{fmtDateTime(String(v))}</span> },
              ]}
              rows={exitPlans}
              keyFn={(r) => String(r.plan_id ?? Math.random())}
              emptyMessage="No active exit plans"
              compact
            />
          </Card>

          {approvals.length > 0 && (
            <Card>
              <CardHeader title="Pending Approvals" subtitle={`${approvals.length} pending`} icon={<Eye size={14} />} />
              <div className="divide-y divide-surface-border">
                {approvals.map((req) => (
                  <div key={String(req.request_id)} className="px-4 py-3 flex items-center justify-between gap-3">
                    <div>
                      <div className="text-sm font-medium text-gray-200">
                        {String(req.side)} {String(req.symbol)} × {String(req.quantity)}
                      </div>
                      <div className="text-xs text-gray-500">{String(req.strategy_name)} · {String(req.state)}</div>
                    </div>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => handleApprove(String(req.request_id))}
                        className="flex items-center gap-1 px-2 py-1 rounded text-xs bg-brand-green/15 border border-brand-green/30 text-brand-green hover:bg-brand-green/25 transition-colors"
                      >
                        <CheckCircle size={12} /> Approve
                      </button>
                      <button
                        onClick={() => handleReject(String(req.request_id))}
                        className="flex items-center gap-1 px-2 py-1 rounded text-xs bg-brand-red/15 border border-brand-red/30 text-brand-red hover:bg-brand-red/25 transition-colors"
                      >
                        <XCircle size={12} /> Reject
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>
      )}

      {/* ── Tab 6: Derivatives ───────────────────────────────────────────────── */}
      {tab === 'derivatives' && (
        <div className="space-y-4">
          {/* Underlying & expiry selectors */}
          <Card>
            <CardBody className="!py-3">
              <div className="flex flex-wrap items-center gap-3">
                <span className="text-xs text-gray-400">Underlying:</span>
                {['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'RELIANCE', 'TCS'].map((u) => (
                  <button
                    key={u}
                    onClick={() => { setOptionUnderlying(u); loadExpiries(u) }}
                    className={clsx(
                      'px-2.5 py-1 rounded text-xs font-mono border transition-colors',
                      optionUnderlying === u
                        ? 'bg-brand-blue/15 border-brand-blue/30 text-brand-blue'
                        : 'bg-surface-elevated border-surface-border text-gray-400 hover:text-gray-200',
                    )}
                  >
                    {u}
                  </button>
                ))}
                {optionExpiries.length > 0 && (
                  <>
                    <span className="text-xs text-gray-400">Expiry:</span>
                    <select
                      value={selectedExpiry}
                      onChange={(e) => setSelectedExpiry(e.target.value)}
                      className="bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-2 py-1 font-mono focus:outline-none focus:border-brand-blue"
                    >
                      {optionExpiries.map(e => <option key={e} value={e}>{e}</option>)}
                    </select>
                  </>
                )}
              </div>
            </CardBody>
          </Card>

          {/* Option Chain */}
          {optionChain && (
            <Card>
              <CardHeader title={`Option Chain — ${optionUnderlying}`} subtitle={selectedExpiry} />
              <Table
                columns={[
                  { key: 'strike', label: 'Strike', align: 'center', render: (v) => <span className="font-bold text-gray-200">{String(v)}</span> },
                  { key: 'ce_ltp', label: 'CE LTP', align: 'right', render: (v) => v ? inr(Number(v), 2) : '—' },
                  { key: 'ce_oi', label: 'CE OI', align: 'right' },
                  { key: 'pe_ltp', label: 'PE LTP', align: 'right', render: (v) => v ? inr(Number(v), 2) : '—' },
                  { key: 'pe_oi', label: 'PE OI', align: 'right' },
                  { key: 'delta', label: 'CE Delta', align: 'right', render: (v) => v ? Number(v).toFixed(3) : '—' },
                ]}
                rows={
                  ((optionChain.rows as Array<{ strike: number; call?: { ltp?: number; oi?: number; delta?: number } | null; put?: { ltp?: number; oi?: number } | null }> | undefined) ?? [])
                    .map((row) => ({
                      strike: row.strike,
                      ce_ltp: row.call?.ltp ?? null,
                      ce_oi: row.call?.oi ?? null,
                      pe_ltp: row.put?.ltp ?? null,
                      pe_oi: row.put?.oi ?? null,
                      delta: row.call?.delta ?? null,
                    }))
                }
                keyFn={(r) => String(r.strike)}
                emptyMessage="No option chain data"
                compact
              />
            </Card>
          )}

          {/* Greeks Calculator */}
          <Card>
            <CardHeader title="Greeks Calculator" icon={<Cpu size={14} />} />
            <CardBody>
              <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-3">
                {[
                  { label: 'Spot', key: 'spot', step: 50 },
                  { label: 'Strike', key: 'strike', step: 50 },
                  { label: 'DTE', key: 'dte', step: 1 },
                  { label: 'Vol %', key: 'vol', step: 0.01 },
                ].map(({ label, key, step }) => (
                  <div key={key}>
                    <label className="block text-[10px] text-gray-500 mb-1 uppercase tracking-wider">{label}</label>
                    <input
                      type="number"
                      step={step}
                      value={greeksForm[key as keyof typeof greeksForm]}
                      onChange={(e) => setGreeksForm(f => ({ ...f, [key]: Number(e.target.value) }))}
                      className="w-full bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-2 py-1.5 font-mono focus:outline-none focus:border-brand-blue"
                    />
                  </div>
                ))}
                <div>
                  <label className="block text-[10px] text-gray-500 mb-1 uppercase tracking-wider">Type</label>
                  <select
                    value={greeksForm.type}
                    onChange={(e) => setGreeksForm(f => ({ ...f, type: e.target.value }))}
                    className="w-full bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-2 py-1.5 font-mono focus:outline-none focus:border-brand-blue"
                  >
                    <option value="CE">CE</option>
                    <option value="PE">PE</option>
                  </select>
                </div>
              </div>
              <button
                onClick={handleCalcGreeks}
                disabled={greeksLoading}
                className="flex items-center gap-1.5 px-4 py-2 rounded-md bg-brand-blue/15 border border-brand-blue/30 text-brand-blue text-xs font-medium hover:bg-brand-blue/25 transition-colors"
              >
                {greeksLoading ? <Loader2 size={12} className="animate-spin" /> : <TrendingUp size={12} />}
                Calculate Greeks
              </button>
              {greeksResult && (
                <div className="mt-3 grid grid-cols-2 md:grid-cols-4 gap-3">
                  {['delta', 'gamma', 'theta', 'vega'].map((g) => (
                    <div key={g} className="bg-surface-elevated rounded p-2">
                      <div className="text-[10px] text-gray-500 uppercase tracking-wider">{g}</div>
                      <div className="text-sm font-bold font-mono text-brand-cyan mt-1">
                        {typeof greeksResult[g] === 'number' ? Number(greeksResult[g]).toFixed(4) : '—'}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardBody>
          </Card>
        </div>
      )}

    </div>
  )
}
