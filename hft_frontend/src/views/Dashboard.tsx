import { useEffect, useRef, useCallback, useState } from 'react'
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell,
  ResponsiveContainer, ReferenceLine, Tooltip, XAxis, YAxis,
} from 'recharts'
import { clsx } from 'clsx'
import {
  Activity, Atom, Brain, Cpu, Loader2, RefreshCw,
  Target, TrendingDown, TrendingUp, Zap, LayoutDashboard,
} from 'lucide-react'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { MetricCard } from '../components/shared/MetricCard'
import { Table } from '../components/shared/Table'
import { execModeBadge } from '../components/shared/Badge'
import { useStore } from '../store'
import { fmtDate, fmtDateTime, inr, pct, fmtUptime } from '../utils'
import {
  getEquityCurve, getDailyPnl, getRecentTrades, getTargetProgress,
  getAICouncilStatus, getNeuralStatus, getGoalGovernorStatus,
  getBatchTicks, getFeedSnapshot,
} from '../api'
import type { Trade } from '../types'

const MARKET_SYMBOLS = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'SENSEX', 'RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 'SBIN']

type DashboardTick = {
  ltp?: number
  last_price?: number
  close?: number
  change?: number
  available?: boolean
  timestamp?: string
}

const TOOLTIP_STYLE = {
  backgroundColor: '#161b22',
  border: '1px solid #30363d',
  borderRadius: 6,
  fontSize: 12,
  color: '#e6edf3',
}

function finiteNumber(value: unknown): number | null {
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function tickPrice(t: DashboardTick | undefined): number | null {
  if (!t || t.available === false) return null
  return finiteNumber(t.ltp ?? t.last_price)
}

function tickChangePct(t: DashboardTick | undefined): number | null {
  if (!t || t.available === false) return null
  const explicit = finiteNumber(t.change)
  if (explicit !== null) return explicit

  const last = finiteNumber(t.last_price ?? t.ltp)
  const close = finiteNumber(t.close)
  if (last === null || close === null || close === 0) return null
  return ((last - close) / close) * 100
}

function tickAgeSeconds(t: DashboardTick | undefined): number | null {
  if (!t?.timestamp) return null
  const time = new Date(t.timestamp).getTime()
  if (Number.isNaN(time)) return null
  return Math.max(0, Math.round((Date.now() - time) / 1000))
}

function tickStatus(t: DashboardTick | undefined): { label: string; className: string } {
  if (!t || t.available === false) {
    return { label: 'NOT SUBSCRIBED', className: 'text-gray-500 border-gray-700 bg-surface' }
  }

  const age = tickAgeSeconds(t)
  if (age !== null && age > 60) {
    return { label: 'STALE', className: 'text-brand-red border-brand-red/40 bg-brand-red/10' }
  }
  if (age !== null && age > 15) {
    return { label: `${age}s`, className: 'text-brand-yellow border-brand-yellow/40 bg-brand-yellow/10' }
  }
  return { label: age === null ? 'LIVE' : `${age}s`, className: 'text-brand-green border-brand-green/40 bg-brand-green/10' }
}

export function Dashboard() {
  const runtimeState   = useStore((s) => s.runtimeState)
  const monitoring     = useStore((s) => s.monitoring)
  const livePortfolio  = useStore((s) => s.livePortfolio)
  const wsConnected    = useStore((s) => s.wsConnected)
  const equityCurve    = useStore((s) => s.equityCurve)
  const dailyPnl       = useStore((s) => s.dailyPnl)
  const recentTrades   = useStore((s) => s.recentTrades)
  const targetProgress = useStore((s) => s.targetProgress)

  const setEquityCurve    = useStore((s) => s.setEquityCurve)
  const setDailyPnl       = useStore((s) => s.setDailyPnl)
  const setRecentTrades   = useStore((s) => s.setRecentTrades)
  const setTargetProgress = useStore((s) => s.setTargetProgress)

  const aiCouncilStatus = useStore((s) => s.aiCouncilStatus)
  const neuralStatus    = useStore((s) => s.neuralStatus)
  const goalStatus      = useStore((s) => s.goalGovernorStatus)

  const setAICouncilStatus = useStore((s) => s.setAICouncilStatus)
  const setNeuralStatus    = useStore((s) => s.setNeuralStatus)
  const setGoalStatus      = useStore((s) => s.setGoalGovernorStatus)

  const [marketTicks, setMarketTicks] = useState<Record<string, DashboardTick>>({})
  const [feedSnapshot, setFeedSnapshot] = useState<Record<string, unknown> | null>(null)
  const [marketUpdatedAt, setMarketUpdatedAt] = useState<string | null>(null)

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const marketPollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const refresh = useCallback(async () => {
    const [curve, pnl, trades, progress] = await Promise.allSettled([
      getEquityCurve(),
      getDailyPnl(),
      getRecentTrades(),
      getTargetProgress({}),
    ])
    if (curve.status === 'fulfilled')    setEquityCurve(curve.value.curve)
    if (pnl.status === 'fulfilled')      setDailyPnl(pnl.value.history)
    if (trades.status === 'fulfilled')   setRecentTrades(trades.value.trades)
    if (progress.status === 'fulfilled') setTargetProgress(progress.value)
  }, [setEquityCurve, setDailyPnl, setRecentTrades, setTargetProgress])

  const refreshMarket = useCallback(async () => {
    const [ticksRes, feedRes] = await Promise.allSettled([
      getBatchTicks(MARKET_SYMBOLS, true),
      getFeedSnapshot(),
    ])

    if (ticksRes.status === 'fulfilled') {
      const merged: Record<string, DashboardTick> = {}
      for (const sym of MARKET_SYMBOLS) {
        merged[sym] = (ticksRes.value.ticks[sym] as DashboardTick | undefined) ?? {}
      }
      setMarketTicks(merged)
      setMarketUpdatedAt(new Date().toLocaleTimeString('en-IN'))
    }
    if (feedRes.status === 'fulfilled') setFeedSnapshot(feedRes.value as Record<string, unknown>)
  }, [])

  useEffect(() => {
    refresh()
    pollRef.current = setInterval(refresh, 30_000)
    Promise.allSettled([getAICouncilStatus(), getNeuralStatus(), getGoalGovernorStatus()])
      .then(([cs, ns, gs]) => {
        if (cs.status === 'fulfilled') setAICouncilStatus(cs.value)
        if (ns.status === 'fulfilled') setNeuralStatus(ns.value)
        if (gs.status === 'fulfilled') setGoalStatus(gs.value)
      })
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [refresh])

  useEffect(() => {
    refreshMarket()
    marketPollRef.current = setInterval(refreshMarket, 2_000)
    return () => { if (marketPollRef.current) clearInterval(marketPollRef.current) }
  }, [refreshMarket])

  // ── Derived metrics ──────────────────────────────────────────────────────────
  const latestEquity    = livePortfolio?.portfolio.equity ?? equityCurve[equityCurve.length - 1]?.equity ?? 0
  // WS portfolio.drawdown is a fraction (e.g. 0.0028 = 0.28%); equity curve
  // drawdown is stored the same way. Multiply by 100 only at display time.
  const latestDrawdown  = livePortfolio?.portfolio.drawdown
    ?? equityCurve[equityCurve.length - 1]?.drawdown
    ?? 0
  const unrealizedPnl   = livePortfolio?.portfolio.unrealized_pnl ?? 0
  const realizedPnl     = livePortfolio?.portfolio.realized_pnl ?? 0
  const totalPnl        = dailyPnl.reduce((s, d) => s + d.realized_pnl, 0)
  const todayPnl        = dailyPnl[dailyPnl.length - 1]?.realized_pnl ?? 0

  const progressPct = targetProgress
    ? Math.min(100, (targetProgress.realized_pnl / targetProgress.annual_target) * 100)
    : 0

  const isSystemFresh = equityCurve.length === 0 && recentTrades.length === 0 && !livePortfolio

  // AI system status count
  const aiSystems = [aiCouncilStatus?.enabled, neuralStatus?.enabled, goalStatus?.enabled]
  const aiEnabledCount = aiSystems.filter(Boolean).length
  const feedRunning = Boolean(feedSnapshot?.running)

  // ── Trade table columns ───────────────────────────────────────────────────────
  const tradeColumns = [
    {
      key: 'symbol', label: 'Symbol',
      render: (_: unknown, r: Trade) => <span className="text-brand-blue font-semibold">{r.symbol}</span>,
    },
    {
      key: 'side', label: 'Side',
      render: (_: unknown, r: Trade) => (
        <span className={r.side === 'BUY' ? 'text-brand-green' : 'text-brand-red'}>{r.side}</span>
      ),
    },
    {
      key: 'quantity', label: 'Qty', align: 'right' as const,
      render: (_: unknown, r: Trade) => r.quantity,
    },
    {
      key: 'price', label: 'Price', align: 'right' as const,
      render: (_: unknown, r: Trade) => inr(r.price, 2),
    },
    {
      key: 'strategy_name', label: 'Strategy',
      render: (_: unknown, r: Trade) => <span className="text-xs text-gray-400">{r.strategy_name}</span>,
    },
    {
      key: 'timestamp', label: 'Time',
      render: (_: unknown, r: Trade) => <span className="text-xs text-gray-500">{fmtDateTime(r.timestamp)}</span>,
    },
  ]

  return (
    <div className="space-y-5">

      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <LayoutDashboard size={16} className="text-brand-blue" />
          <div>
            <h1 className="text-lg font-bold text-gray-100">Command Center</h1>
            <p className="text-xs text-gray-500 mt-0.5">Live performance · multi-agent overview</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {runtimeState && execModeBadge(runtimeState.execution_mode)}
          <button
            onClick={() => { refresh(); refreshMarket() }}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-surface-elevated border border-surface-border text-xs text-gray-400 hover:text-gray-200 transition-colors"
          >
            <RefreshCw size={12} />
            Refresh
          </button>
        </div>
      </div>

      {/* ── System Status Row ────────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {/* Runtime */}
        <div className="bg-surface-card border border-surface-border rounded-lg p-3 space-y-1.5">
          <div className="text-[10px] text-gray-500 uppercase tracking-wider font-semibold">Runtime</div>
          <div className="flex items-center gap-2">
            {runtimeState ? execModeBadge(runtimeState.execution_mode) : <span className="text-xs text-gray-600">—</span>}
          </div>
          <div className="flex items-center gap-1.5">
            <span className={clsx('w-1.5 h-1.5 rounded-full', wsConnected ? 'bg-brand-green animate-pulse-slow' : 'bg-brand-red')} />
            <span className={clsx('text-[10px] font-mono', wsConnected ? 'text-brand-green' : 'text-brand-red')}>
              {wsConnected ? 'WS LIVE' : 'WS OFF'}
            </span>
          </div>
        </div>

        {/* Engine Health */}
        <div className="bg-surface-card border border-surface-border rounded-lg p-3 space-y-1.5">
          <div className="text-[10px] text-gray-500 uppercase tracking-wider font-semibold">Engine Health</div>
          <div className={clsx(
            'text-sm font-bold font-mono',
            monitoring?.status === 'HEALTHY' ? 'text-brand-green'
            : monitoring?.status === 'DEGRADED' ? 'text-brand-yellow'
            : 'text-brand-red',
          )}>
            {monitoring?.status ?? '—'}
          </div>
          <div className="text-[10px] text-gray-600 font-mono">
            {monitoring ? `${monitoring.total_orders} orders · ${monitoring.average_latency_ms.toFixed(0)}ms` : '—'}
          </div>
        </div>

        {/* Portfolio */}
        <div className="bg-surface-card border border-surface-border rounded-lg p-3 space-y-1.5">
          <div className="text-[10px] text-gray-500 uppercase tracking-wider font-semibold">Portfolio</div>
          <div className="text-sm font-bold font-mono text-brand-blue">{inr(latestEquity)}</div>
          <div className={clsx('text-[10px] font-mono', latestDrawdown > 0.05 ? 'text-brand-red' : 'text-gray-500')}>
            DD: {pct(latestDrawdown * 100)}
          </div>
        </div>

        {/* AI Systems */}
        <div className="bg-surface-card border border-surface-border rounded-lg p-3 space-y-1.5">
          <div className="text-[10px] text-gray-500 uppercase tracking-wider font-semibold">AI Systems</div>
          <div className={clsx(
            'text-sm font-bold font-mono',
            aiEnabledCount > 0 ? 'text-brand-purple' : 'text-gray-600',
          )}>
            {aiEnabledCount} / 4
          </div>
          <div className="text-[10px] text-gray-600">
            {aiEnabledCount > 0 ? 'systems enabled' : 'all disabled'}
          </div>
        </div>
      </div>

      {/* ── AI Status Row ────────────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        {[
          {
            icon: <Brain size={13} className="text-brand-purple" />,
            label: 'AI Council',
            phase: 'Phase 2',
            enabled: aiCouncilStatus?.enabled,
            live: aiCouncilStatus?.gateway_available,
            detail: aiCouncilStatus?.gateway_runtime,
            color: 'purple',
          },
          {
            icon: <Zap size={13} className="text-brand-blue" />,
            label: 'Neural Lab',
            phase: 'Phase 3',
            enabled: neuralStatus?.enabled,
            live: true,
            detail: neuralStatus?.models?.forecaster,
            color: 'blue',
          },
          {
            icon: <Target size={13} className="text-brand-green" />,
            label: 'Goal Governor',
            phase: 'Phase 6',
            enabled: goalStatus?.enabled,
            live: true,
            detail: goalStatus ? `p=${pct(goalStatus.target_probability)}` : undefined,
            color: 'green',
          },
        ].map((s) => (
          <div
            key={s.label}
            className={clsx(
              'flex items-center gap-2 px-3 py-2.5 rounded-lg border text-xs',
              !s.enabled
                ? 'border-surface-border bg-surface-card text-gray-600'
                : s.live
                  ? 'border-brand-purple/20 bg-brand-purple/5'
                  : 'border-brand-yellow/20 bg-brand-yellow/5',
            )}
          >
            {s.icon}
            <div className="min-w-0 flex-1">
              <div className={clsx(
                'font-semibold truncate',
                !s.enabled ? 'text-gray-600' : s.live ? 'text-gray-200' : 'text-brand-yellow',
              )}>
                {s.label}
              </div>
              <div className="text-[10px] text-gray-600">{s.phase}</div>
              {s.enabled && s.detail && (
                <div className="text-[10px] font-mono text-gray-500 truncate">{s.detail}</div>
              )}
            </div>
            <span className={clsx(
              'ml-auto text-[9px] font-mono font-bold flex-shrink-0 px-1.5 py-0.5 rounded',
              !s.enabled ? 'text-gray-700 bg-gray-800'
                : s.live   ? 'text-brand-green bg-brand-green/10'
                           : 'text-brand-yellow bg-brand-yellow/10',
            )}>
              {!s.enabled ? 'OFF' : s.live ? 'ON' : 'STUB'}
            </span>
          </div>
        ))}
      </div>

      {/* ── Key Metrics Row ──────────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <MetricCard
          label="Portfolio Equity"
          value={inr(latestEquity)}
          color="blue"
          icon={<Activity size={14} />}
          sub={livePortfolio ? `${livePortfolio.count} open positions` : 'no live portfolio'}
        />
        <MetricCard
          label="Realized P&L"
          value={inr(realizedPnl || totalPnl)}
          color={realizedPnl >= 0 ? 'green' : 'red'}
          icon={realizedPnl >= 0 ? <TrendingUp size={14} /> : <TrendingDown size={14} />}
          sub={unrealizedPnl !== 0 ? `Unrealized: ${inr(unrealizedPnl)}` : undefined}
        />
        <MetricCard
          label="Drawdown"
          value={pct(latestDrawdown * 100)}
          color={latestDrawdown > 0.1 ? 'red' : latestDrawdown > 0.05 ? 'yellow' : 'green'}
          sub="from peak equity"
        />
        <MetricCard
          label="Target Gap"
          value={targetProgress ? inr(targetProgress.current_gap) : '—'}
          color="yellow"
          icon={<Target size={14} />}
          sub={targetProgress ? `${inr(targetProgress.required_run_rate)}/day needed` : undefined}
        />
      </div>

      {/* ── Live Market Strip ───────────────────────────────────────────────── */}
      <Card>
        <CardHeader
          title="Live Market"
          subtitle={`Updated ${marketUpdatedAt ?? '—'}`}
          icon={<Activity size={14} />}
          action={
            <span className={clsx(
              'text-[10px] font-mono font-bold px-2 py-0.5 rounded border',
              feedRunning ? 'text-brand-green border-brand-green/30 bg-brand-green/10' : 'text-brand-red border-brand-red/30 bg-brand-red/10',
            )}>
              {feedRunning ? 'FEED LIVE' : 'FEED OFF'}
            </span>
          }
        />
        <CardBody className="!p-2">
          <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-5 gap-2">
            {MARKET_SYMBOLS.map((sym) => {
              const tick = marketTicks[sym]
              const price = tickPrice(tick)
              const change = tickChangePct(tick)
              const status = tickStatus(tick)
              return (
                <div key={sym} className="flex items-center justify-between gap-2 rounded-md bg-surface-elevated border border-surface-border px-2.5 py-2">
                  <div className="min-w-0">
                    <div className="text-xs font-mono font-semibold text-gray-200 truncate">{sym}</div>
                    <span className={clsx('mt-1 inline-flex rounded border px-1 py-0.5 text-[9px] font-mono leading-none', status.className)}>
                      {status.label}
                    </span>
                  </div>
                  <div className="text-right shrink-0">
                    <div className="text-xs font-mono font-bold text-gray-100">{price === null ? '—' : inr(price, 2)}</div>
                    <div className={clsx('text-[10px] font-mono', (change ?? 0) > 0 ? 'text-brand-green' : (change ?? 0) < 0 ? 'text-brand-red' : 'text-gray-500')}>
                      {change !== null && change !== 0 ? pct(change) : '—'}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        </CardBody>
      </Card>

      {/* ── Charts Row ───────────────────────────────────────────────────────── */}
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-5">
        <Card className="xl:col-span-2">
          <CardHeader
            title="Equity Curve"
            subtitle={`${equityCurve.length} snapshots`}
            icon={<Activity size={14} />}
          />
          <CardBody className="h-56 !p-2">
            {equityCurve.length === 0 ? (
              <div className="h-full flex items-center justify-center">
                <div className="text-center">
                  <Loader2 size={20} className="animate-spin text-gray-600 mx-auto mb-2" />
                  <p className="text-xs text-gray-500">No equity data yet</p>
                </div>
              </div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={equityCurve} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                  <defs>
                    <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%"  stopColor="#58a6ff" stopOpacity={0.3} />
                      <stop offset="95%" stopColor="#58a6ff" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="#30363d" vertical={false} />
                  <XAxis
                    dataKey="recorded_at"
                    tickFormatter={(v) => fmtDate(v)}
                    tick={{ fill: '#6e7681', fontSize: 10 }}
                    axisLine={false}
                    tickLine={false}
                    interval="preserveStartEnd"
                  />
                  <YAxis
                    tickFormatter={(v: number) => `₹${(v / 100000).toFixed(0)}L`}
                    tick={{ fill: '#6e7681', fontSize: 10 }}
                    axisLine={false}
                    tickLine={false}
                    width={52}
                  />
                  <Tooltip
                    contentStyle={TOOLTIP_STYLE}
                    labelFormatter={(v) => fmtDate(String(v))}
                    formatter={(v: number) => [inr(v), 'Equity']}
                  />
                  <Area
                    type="monotone"
                    dataKey="equity"
                    stroke="#58a6ff"
                    strokeWidth={2}
                    fill="url(#eqGrad)"
                    dot={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Daily P&L" subtitle="Last 30 days" />
          <CardBody className="h-56 !p-2">
            {dailyPnl.length === 0 ? (
              <div className="h-full flex items-center justify-center">
                <p className="text-xs text-gray-500">No daily P&L data</p>
              </div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={dailyPnl} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#30363d" vertical={false} />
                  <XAxis
                    dataKey="trade_date"
                    tickFormatter={(v) => fmtDate(v).slice(0, 6)}
                    tick={{ fill: '#6e7681', fontSize: 9 }}
                    axisLine={false}
                    tickLine={false}
                    interval={4}
                  />
                  <YAxis
                    tickFormatter={(v: number) => `${v >= 0 ? '+' : ''}${(v / 1000).toFixed(0)}k`}
                    tick={{ fill: '#6e7681', fontSize: 9 }}
                    axisLine={false}
                    tickLine={false}
                    width={40}
                  />
                  <ReferenceLine y={0} stroke="#30363d" strokeWidth={1} />
                  <Tooltip
                    contentStyle={TOOLTIP_STYLE}
                    formatter={(v: number) => [inr(v), 'Realized P&L']}
                  />
                  <Bar dataKey="realized_pnl" radius={[2, 2, 0, 0]}>
                    {dailyPnl.map((entry, index) => (
                      <Cell
                        key={`cell-${index}`}
                        fill={entry.realized_pnl >= 0 ? '#3fb950' : '#f85149'}
                      />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            )}
          </CardBody>
        </Card>
      </div>

      {/* ── Annual Target Progress ───────────────────────────────────────────── */}
      {targetProgress && (
        <Card>
          <CardHeader title="Annual Target Progress" icon={<Target size={14} />} subtitle="5Cr goal tracking" />
          <CardBody>
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs text-gray-400">
                {inr(targetProgress.realized_pnl)} of {inr(targetProgress.annual_target)} target
              </span>
              <span className="text-xs font-mono text-brand-blue font-bold">{progressPct.toFixed(1)}%</span>
            </div>
            <div className="h-2.5 bg-surface-elevated rounded-full overflow-hidden">
              <div
                className="h-full bg-gradient-to-r from-brand-blue to-brand-cyan rounded-full transition-all duration-500"
                style={{ width: `${progressPct}%` }}
              />
            </div>
            <div className="flex flex-wrap justify-between mt-2 gap-2 text-[11px] text-gray-500">
              <span>Day {targetProgress.elapsed_days} of ~252</span>
              <span>Run rate: <span className="text-brand-yellow">{inr(targetProgress.required_run_rate)}/day</span></span>
              <span>Gap: <span className="text-brand-red">{inr(targetProgress.current_gap)}</span></span>
              <span>Bias: <span className="text-brand-yellow">{targetProgress.allocation_bias}</span></span>
            </div>
            {goalStatus?.enabled && goalStatus.target_probability != null && (
              <div className="mt-3 pt-3 border-t border-surface-border flex items-center justify-between">
                <div className="flex items-center gap-1.5">
                  <Atom size={12} className="text-brand-purple" />
                  <span className="text-[11px] text-gray-500">Monte Carlo target probability</span>
                </div>
                <span className={clsx(
                  'text-sm font-bold font-mono',
                  goalStatus.target_probability > 0.6 ? 'text-brand-green'
                  : goalStatus.target_probability > 0.3 ? 'text-brand-yellow' : 'text-brand-red',
                )}>
                  {pct(goalStatus.target_probability)}
                </span>
              </div>
            )}
          </CardBody>
        </Card>
      )}

      {/* ── Execution Health Strip ───────────────────────────────────────────── */}
      {monitoring && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <MetricCard
            label="Fill Rate"
            value={monitoring.total_orders > 0
              ? `${((monitoring.filled_orders / monitoring.total_orders) * 100).toFixed(0)}%`
              : '—'}
            color={monitoring.filled_orders / Math.max(1, monitoring.total_orders) > 0.85 ? 'green' : 'yellow'}
            sub={`${monitoring.filled_orders}/${monitoring.total_orders} orders`}
          />
          <MetricCard
            label="Rejection Rate"
            value={`${(monitoring.rejection_rate * 100).toFixed(1)}%`}
            color={monitoring.rejection_rate < 0.05 ? 'green' : monitoring.rejection_rate < 0.1 ? 'yellow' : 'red'}
            sub={`${monitoring.rejected_orders} rejected`}
          />
          <MetricCard
            label="Avg Latency"
            value={`${monitoring.average_latency_ms.toFixed(0)}ms`}
            color={monitoring.average_latency_ms < 50 ? 'green' : monitoring.average_latency_ms < 200 ? 'yellow' : 'red'}
            sub={`max ${monitoring.max_latency_ms.toFixed(0)}ms`}
          />
          <MetricCard
            label="Uptime"
            value={fmtUptime(monitoring.uptime_seconds)}
            color="blue"
            sub={`${monitoring.event_count} events`}
          />
        </div>
      )}

      {/* ── Onboarding Guide ─────────────────────────────────────────────────── */}
      {isSystemFresh && (
        <div className="bg-indigo-950/30 border border-indigo-800/50 rounded-lg p-4">
          <h3 className="text-sm font-semibold text-indigo-300 mb-3">Welcome — No trading data yet</h3>
          <p className="text-xs text-gray-400 mb-3">
            This dashboard shows live P&L, equity curve, and trade history once the engine has run.
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            {[
              { step: '1', title: 'Start the Agent', desc: 'Go to Engine → click Start Agent. The agent scans markets every 5 minutes during trading hours (9:15–15:20 IST).' },
              { step: '2', title: 'Run a Manual Scan', desc: 'Go to Signals → click Scan Signals to preview signals right now.' },
              { step: '3', title: 'Come back here', desc: 'After trades execute (paper mode), this dashboard populates with equity curve, P&L bars, and trade history.' },
            ].map(({ step, title, desc }) => (
              <div key={step} className="flex gap-3">
                <span className="w-6 h-6 rounded-full bg-indigo-700 text-indigo-100 flex items-center justify-center text-xs font-bold shrink-0 mt-0.5">{step}</span>
                <div>
                  <div className="text-xs font-semibold text-gray-200">{title}</div>
                  <div className="text-[11px] text-gray-500 mt-0.5">{desc}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Recent Trades ────────────────────────────────────────────────────── */}
      <Card>
        <CardHeader
          title="Recent Trades"
          subtitle={`${recentTrades.slice(0, 10).length} shown`}
          action={
            <span className="text-xs text-gray-500">{recentTrades.length} total</span>
          }
        />
        <Table
          columns={tradeColumns}
          rows={recentTrades.slice(0, 10)}
          keyFn={(r) => String(r.trade_id)}
          emptyMessage="No trades yet — start the agent or run a scan"
          compact
        />
      </Card>

    </div>
  )
}
