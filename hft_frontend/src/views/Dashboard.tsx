import { useEffect, useRef } from 'react'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  ReferenceLine,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { Activity, RefreshCw, Target, TrendingDown, TrendingUp, Layers } from 'lucide-react'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { MetricCard } from '../components/shared/MetricCard'
import { Table } from '../components/shared/Table'
import { execModeBadge } from '../components/shared/Badge'
import { useStore } from '../store'
import {
  fmtDate,
  fmtDateTime,
  inr,
  pct,
  fmtUptime,
} from '../utils'
import {
  getEquityCurve,
  getDailyPnl,
  getRecentTrades,
  getTargetProgress,
} from '../api'
import type { Trade } from '../types'

const TOOLTIP_STYLE = {
  backgroundColor: '#161b22',
  border: '1px solid #30363d',
  borderRadius: 6,
  fontSize: 12,
  color: '#e6edf3',
}

export function Dashboard() {
  const runtimeState = useStore((s) => s.runtimeState)
  const monitoring = useStore((s) => s.monitoring)
  const livePortfolio = useStore((s) => s.livePortfolio)
  const equityCurve = useStore((s) => s.equityCurve)
  const dailyPnl = useStore((s) => s.dailyPnl)
  const recentTrades = useStore((s) => s.recentTrades)
  const targetProgress = useStore((s) => s.targetProgress)
  const setEquityCurve = useStore((s) => s.setEquityCurve)
  const setDailyPnl = useStore((s) => s.setDailyPnl)
  const setRecentTrades = useStore((s) => s.setRecentTrades)
  const setTargetProgress = useStore((s) => s.setTargetProgress)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  async function refresh() {
    const [curve, pnl, trades, progress] = await Promise.allSettled([
      getEquityCurve(),
      getDailyPnl(),
      getRecentTrades(),
      getTargetProgress({}),
    ])
    if (curve.status === 'fulfilled') setEquityCurve(curve.value.curve)
    if (pnl.status === 'fulfilled') setDailyPnl(pnl.value.history)
    if (trades.status === 'fulfilled') setRecentTrades(trades.value.trades)
    if (progress.status === 'fulfilled') setTargetProgress(progress.value)
  }

  useEffect(() => {
    refresh()
    pollRef.current = setInterval(refresh, 10_000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [])

  const latestEquity = livePortfolio?.portfolio.equity ?? equityCurve[equityCurve.length - 1]?.equity ?? 0
  const latestDrawdown = livePortfolio?.portfolio.drawdown != null
    ? livePortfolio.portfolio.drawdown / 100
    : equityCurve[equityCurve.length - 1]?.drawdown ?? 0
  const unrealizedPnl = livePortfolio?.portfolio.unrealized_pnl ?? 0
  const totalPnl = dailyPnl.reduce((s, d) => s + d.realized_pnl, 0)
  const todayPnl = dailyPnl[dailyPnl.length - 1]?.realized_pnl ?? 0
  const winRate =
    dailyPnl.length > 0
      ? (dailyPnl.filter((d) => d.realized_pnl > 0).length / dailyPnl.length) * 100
      : 0

  // Win/loss streak from most-recent day backwards
  const streak = (() => {
    if (dailyPnl.length === 0) return { count: 0, type: 'none' as const }
    const last = dailyPnl[dailyPnl.length - 1].realized_pnl
    const type = last >= 0 ? 'win' as const : 'loss' as const
    let count = 0
    for (let i = dailyPnl.length - 1; i >= 0; i--) {
      if ((dailyPnl[i].realized_pnl >= 0) === (type === 'win')) count++
      else break
    }
    return { count, type }
  })()

  const tradeColumns = [
    { key: 'symbol', header: 'Symbol', render: (r: Trade) => <span className="text-brand-blue font-semibold">{r.symbol}</span> },
    { key: 'side', header: 'Side', render: (r: Trade) => <span className={r.side === 'BUY' ? 'text-brand-green' : 'text-brand-red'}>{r.side}</span> },
    { key: 'qty', header: 'Qty', align: 'right' as const, render: (r: Trade) => r.quantity },
    { key: 'price', header: 'Price', align: 'right' as const, render: (r: Trade) => inr(r.price, 2) },
    { key: 'strategy', header: 'Strategy', render: (r: Trade) => <span className="text-xs text-gray-400">{r.strategy_name}</span> },
    { key: 'time', header: 'Time', render: (r: Trade) => <span className="text-xs text-gray-500">{fmtDateTime(r.timestamp)}</span> },
  ]

  const progressPct = targetProgress
    ? Math.min(100, (targetProgress.realized_pnl / targetProgress.annual_target) * 100)
    : 0

  const isSystemFresh = equityCurve.length === 0 && recentTrades.length === 0 && !livePortfolio

  return (
    <div className="space-y-5">
      {/* Header row */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-gray-100">Dashboard</h1>
          <p className="text-xs text-gray-500 mt-0.5">Live performance overview</p>
        </div>
        <div className="flex items-center gap-2">
          {runtimeState && execModeBadge(runtimeState.execution_mode)}
          <button
            onClick={refresh}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-surface-elevated border border-surface-border text-xs text-gray-400 hover:text-gray-200 transition-colors"
          >
            <RefreshCw size={12} />
            Refresh
          </button>
        </div>
      </div>

      {/* Onboarding guide — shown when system is fresh */}
      {isSystemFresh && (
        <div className="bg-indigo-950/30 border border-indigo-800/50 rounded-lg p-4">
          <h3 className="text-sm font-semibold text-indigo-300 mb-3">Welcome — No trading data yet</h3>
          <p className="text-xs text-gray-400 mb-3">
            This dashboard shows live P&L, equity curve, and trade history once the engine has run.
            Follow these steps to get started:
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            {[
              { step: '1', title: 'Start the Agent', desc: 'Go to Engine view → click Start Agent. The agent scans markets every 5 minutes during trading hours (9:15–15:20 IST).' },
              { step: '2', title: 'Run a Manual Scan', desc: 'Go to Signal Scanner → click Run Scan to preview signals right now without waiting for the agent cycle.' },
              { step: '3', title: 'Come back here', desc: 'After trades are executed (paper mode), this dashboard populates with equity curve, P&L bars, and trade history.' },
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

      {/* KPI strip */}
      <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-7 gap-3">
        <MetricCard
          label="Portfolio Equity"
          value={inr(latestEquity)}
          accent="blue"
          icon={<Activity size={14} />}
          trend={latestEquity > 2_000_000 ? 'up' : 'down'}
          sub={livePortfolio ? `${livePortfolio.count} open` : undefined}
        />
        <MetricCard
          label="Unrealized P&L"
          value={inr(unrealizedPnl)}
          accent={unrealizedPnl >= 0 ? 'green' : 'red'}
          icon={<Layers size={14} />}
          sub="open positions"
        />
        <MetricCard
          label="Today's Realized"
          value={inr(todayPnl)}
          accent={todayPnl >= 0 ? 'green' : 'red'}
          icon={todayPnl >= 0 ? <TrendingUp size={14} /> : <TrendingDown size={14} />}
        />
        <MetricCard
          label="Total P&L"
          value={inr(totalPnl)}
          accent={totalPnl >= 0 ? 'green' : 'red'}
        />
        <MetricCard
          label="Max Drawdown"
          value={pct(latestDrawdown * 100)}
          accent="yellow"
        />
        <MetricCard
          label="Win Rate"
          value={`${winRate.toFixed(1)}%`}
          sub={
            streak.count > 0
              ? `${streak.count}-day ${streak.type === 'win' ? '🟢' : '🔴'} streak`
              : `${dailyPnl.length} trading days`
          }
          accent="cyan"
        />
        <MetricCard
          label="Uptime"
          value={monitoring ? fmtUptime(monitoring.uptime_seconds) : '—'}
          sub={`${monitoring?.total_orders ?? 0} orders`}
          accent="purple"
          icon={<Activity size={14} />}
        />
      </div>

      {/* Target progress */}
      {targetProgress && (
        <Card>
          <CardHeader title="Annual Target Progress" icon={<Target size={14} />} />
          <CardBody>
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs text-gray-400">
                {inr(targetProgress.realized_pnl)} of {inr(targetProgress.annual_target)} target
              </span>
              <span className="text-xs font-mono text-brand-blue">{progressPct.toFixed(1)}%</span>
            </div>
            <div className="h-2 bg-surface-elevated rounded-full overflow-hidden">
              <div
                className="h-full bg-gradient-to-r from-brand-blue to-brand-cyan rounded-full transition-all duration-500"
                style={{ width: `${progressPct}%` }}
              />
            </div>
            <div className="flex justify-between mt-2 text-[11px] text-gray-500">
              <span>Day {targetProgress.elapsed_days} / ~252</span>
              <span>Run rate needed: {inr(targetProgress.required_run_rate)}/day</span>
              <span>Bias: <span className="text-brand-yellow">{targetProgress.allocation_bias}</span></span>
            </div>
          </CardBody>
        </Card>
      )}

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-5">
        {/* Equity curve */}
        <Card className="xl:col-span-2">
          <CardHeader title="Equity Curve" subtitle={`${equityCurve.length} snapshots`} />
          <CardBody className="h-56 !p-2">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={equityCurve} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                <defs>
                  <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#58a6ff" stopOpacity={0.3} />
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
                  tickFormatter={(v) => `₹${(v / 100000).toFixed(0)}L`}
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
          </CardBody>
        </Card>

        {/* Daily P&L bars */}
        <Card>
          <CardHeader title="Daily P&L" subtitle="Last 30 days" />
          <CardBody className="h-56 !p-2">
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
                  tickFormatter={(v) => `${v >= 0 ? '+' : ''}${(v / 1000).toFixed(0)}k`}
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
          </CardBody>
        </Card>
      </div>

      {/* Execution health strip */}
      {monitoring && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div className="bg-surface-card border border-surface-border rounded-lg p-3">
            <div className="text-[10px] text-gray-500 mb-1 uppercase tracking-wider">Fill Rate</div>
            <div className={`text-lg font-bold font-mono ${monitoring.filled_orders / Math.max(1, monitoring.total_orders) > 0.85 ? 'text-brand-green' : 'text-brand-yellow'}`}>
              {monitoring.total_orders > 0
                ? `${((monitoring.filled_orders / monitoring.total_orders) * 100).toFixed(0)}%`
                : '—'}
            </div>
            <div className="text-[10px] text-gray-600 mt-0.5">{monitoring.filled_orders}/{monitoring.total_orders} orders</div>
          </div>
          <div className="bg-surface-card border border-surface-border rounded-lg p-3">
            <div className="text-[10px] text-gray-500 mb-1 uppercase tracking-wider">Rejection Rate</div>
            <div className={`text-lg font-bold font-mono ${monitoring.rejection_rate < 0.05 ? 'text-brand-green' : monitoring.rejection_rate < 0.10 ? 'text-brand-yellow' : 'text-brand-red'}`}>
              {(monitoring.rejection_rate * 100).toFixed(1)}%
            </div>
            <div className="text-[10px] text-gray-600 mt-0.5">{monitoring.rejected_orders} rejected</div>
          </div>
          <div className="bg-surface-card border border-surface-border rounded-lg p-3">
            <div className="text-[10px] text-gray-500 mb-1 uppercase tracking-wider">Avg Latency</div>
            <div className={`text-lg font-bold font-mono ${monitoring.average_latency_ms < 50 ? 'text-brand-green' : monitoring.average_latency_ms < 200 ? 'text-brand-yellow' : 'text-brand-red'}`}>
              {monitoring.average_latency_ms.toFixed(0)}ms
            </div>
            <div className="text-[10px] text-gray-600 mt-0.5">max {monitoring.max_latency_ms.toFixed(0)}ms</div>
          </div>
          <div className="bg-surface-card border border-surface-border rounded-lg p-3">
            <div className="text-[10px] text-gray-500 mb-1 uppercase tracking-wider">System Status</div>
            <div className={`text-lg font-bold font-mono ${monitoring.status === 'HEALTHY' ? 'text-brand-green' : monitoring.status === 'DEGRADED' ? 'text-brand-yellow' : 'text-brand-red'}`}>
              {monitoring.status}
            </div>
            <div className="text-[10px] text-gray-600 mt-0.5">{monitoring.event_count} events</div>
          </div>
        </div>
      )}

      {/* Recent trades */}
      <Card>
        <CardHeader
          title="Recent Trades"
          subtitle={`${recentTrades.length} trades shown`}
        />
        <Table
          columns={tradeColumns}
          data={recentTrades.slice(0, 15)}
          keyFn={(r) => r.trade_id}
          emptyText="No trades yet"
          compact
        />
      </Card>
    </div>
  )
}
