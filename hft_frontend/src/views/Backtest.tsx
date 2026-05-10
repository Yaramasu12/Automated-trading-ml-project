import { useState, useCallback } from 'react'
import {
  Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { BookOpen, Loader2, Play } from 'lucide-react'
import { clsx } from 'clsx'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Table } from '../components/shared/Table'
import { Tag } from '../components/shared/Badge'
import { useStore } from '../store'
import { runBacktest } from '../api'
import { inr, pct, fmtDate } from '../utils'

const UNDERLYINGS = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'RELIANCE', 'TCS', 'INFY', 'HDFCBANK']

const TOOLTIP_STYLE = {
  backgroundColor: '#161b22',
  border: '1px solid #30363d',
  borderRadius: 6,
  fontSize: 12,
  color: '#e6edf3',
}

export function Backtest() {
  const backtestResult    = useStore((s) => s.backtestResult)
  const setBacktestResult = useStore((s) => s.setBacktestResult)
  const loading           = useStore((s) => s.loading.backtest)
  const setLoading        = useStore((s) => s.setLoading)
  const setError          = useStore((s) => s.setError)

  const [capital, setCapital]   = useState(2000000)
  const [startDate, setStartDate] = useState(() => {
    const d = new Date(); d.setMonth(d.getMonth() - 1)
    return d.toISOString().split('T')[0]
  })
  const [days, setDays]         = useState(30)
  const [selectedUnderlyings, setSelectedUnderlyings] = useState<string[]>(['NIFTY', 'BANKNIFTY'])
  const [strategyFilter, setStrategyFilter] = useState('')
  const [tradeLogExpanded, setTradeLogExpanded] = useState(false)

  function toggleUnderlying(u: string) {
    setSelectedUnderlyings(prev =>
      prev.includes(u) ? prev.filter(x => x !== u) : [...prev, u]
    )
  }

  const runBt = useCallback(async () => {
    setLoading('backtest', true)
    setError(null)
    try {
      const payload = {
        starting_capital: capital,
        start: startDate,
        days,
        underlyings: selectedUnderlyings,
        strategy_name: strategyFilter || undefined,
      }
      const res = await runBacktest(payload)
      setBacktestResult(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Backtest failed')
    } finally {
      setLoading('backtest', false)
    }
  }, [capital, startDate, days, selectedUnderlyings, strategyFilter, setLoading, setError, setBacktestResult])

  const m = backtestResult?.metrics

  // Fake equity curve from starting capital + total pnl
  const equityCurveData = m ? [
    { day: 'Start', equity: m.starting_capital },
    { day: 'End',   equity: m.ending_equity },
  ] : []

  return (
    <div className="space-y-5">

      {/* Header */}
      <div className="flex items-center gap-2">
        <BookOpen size={16} className="text-brand-blue" />
        <div>
          <h1 className="text-lg font-bold text-gray-100">Backtest Engine</h1>
          <p className="text-xs text-gray-500 mt-0.5">Historical strategy simulation with full trade log</p>
        </div>
      </div>

      {/* Form */}
      <Card>
        <CardHeader title="Backtest Configuration" icon={<BookOpen size={14} />} />
        <CardBody className="space-y-4">
          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-3">
            <div>
              <label className="block text-xs text-gray-400 mb-1 uppercase tracking-wider">Starting Capital</label>
              <input
                type="number"
                value={capital}
                onChange={(e) => setCapital(Number(e.target.value))}
                step={100000}
                className="w-full bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-2 py-2 font-mono focus:outline-none focus:border-brand-blue"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1 uppercase tracking-wider">Start Date</label>
              <input
                type="date"
                value={startDate}
                onChange={(e) => setStartDate(e.target.value)}
                className="w-full bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-2 py-2 font-mono focus:outline-none focus:border-brand-blue"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1 uppercase tracking-wider">Days</label>
              <div className="flex gap-1">
                {[7, 14, 30, 60].map((d) => (
                  <button
                    key={d}
                    onClick={() => setDays(d)}
                    className={clsx(
                      'flex-1 py-2 rounded text-xs font-mono border transition-colors',
                      days === d
                        ? 'bg-brand-cyan/15 border-brand-cyan/30 text-brand-cyan'
                        : 'bg-surface-elevated border-surface-border text-gray-400 hover:text-gray-200',
                    )}
                  >
                    {d}d
                  </button>
                ))}
              </div>
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1 uppercase tracking-wider">Strategy Filter</label>
              <input
                type="text"
                value={strategyFilter}
                onChange={(e) => setStrategyFilter(e.target.value)}
                placeholder="All strategies"
                className="w-full bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-2 py-2 font-mono focus:outline-none focus:border-brand-blue"
              />
            </div>
          </div>

          {/* Underlyings */}
          <div>
            <label className="block text-xs text-gray-400 mb-2 uppercase tracking-wider">Underlyings</label>
            <div className="flex flex-wrap gap-1.5">
              {UNDERLYINGS.map((u) => (
                <button
                  key={u}
                  onClick={() => toggleUnderlying(u)}
                  className={clsx(
                    'px-2.5 py-1 rounded text-xs font-mono border transition-colors',
                    selectedUnderlyings.includes(u)
                      ? 'bg-brand-blue/15 border-brand-blue/30 text-brand-blue'
                      : 'bg-surface-elevated border-surface-border text-gray-400 hover:text-gray-200',
                  )}
                >
                  {u}
                </button>
              ))}
            </div>
          </div>

          <button
            onClick={runBt}
            disabled={loading || selectedUnderlyings.length === 0}
            className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-blue/15 border border-brand-blue/30 text-brand-blue text-sm font-medium hover:bg-brand-blue/25 disabled:opacity-50 transition-colors"
          >
            {loading ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
            Run Backtest
          </button>
        </CardBody>
      </Card>

      {/* Results */}
      {m && backtestResult && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
          {/* Metrics */}
          <Card>
            <CardHeader title="Backtest Results" subtitle={`${days}d · ${selectedUnderlyings.join(', ')}`} />
            <CardBody>
              <div className="grid grid-cols-2 gap-3">
                {[
                  { label: 'Total P&L', value: inr(m.total_pnl), color: m.total_pnl >= 0 ? 'text-brand-green' : 'text-brand-red' },
                  { label: 'Return%', value: pct(m.return_pct), color: m.return_pct >= 0 ? 'text-brand-green' : 'text-brand-red' },
                  { label: 'Sharpe Ratio', value: m.sharpe_like.toFixed(2), color: m.sharpe_like > 1 ? 'text-brand-green' : 'text-gray-300' },
                  { label: 'Max Drawdown', value: pct(m.max_drawdown * 100), color: m.max_drawdown > 0.1 ? 'text-brand-red' : 'text-brand-yellow' },
                  { label: 'Win Rate', value: pct(m.win_rate * 100), color: m.win_rate > 0.55 ? 'text-brand-green' : 'text-gray-300' },
                  { label: 'Profit Factor', value: m.profit_factor.toFixed(2), color: m.profit_factor > 1.5 ? 'text-brand-green' : 'text-gray-300' },
                  { label: 'Trade Count', value: String(m.trade_count), color: 'text-gray-200' },
                  { label: 'End Equity', value: inr(m.ending_equity), color: 'text-brand-blue' },
                ].map(({ label, value, color }) => (
                  <div key={label} className="bg-surface-elevated rounded-lg p-3">
                    <div className="text-[10px] text-gray-500 uppercase tracking-wider">{label}</div>
                    <div className={clsx('text-lg font-bold font-mono mt-1', color)}>{value}</div>
                  </div>
                ))}
              </div>
            </CardBody>
          </Card>

          {/* Equity curve mini chart */}
          <Card>
            <CardHeader title="Equity Curve" subtitle="Start vs End" />
            <CardBody className="h-64 !p-2">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={equityCurveData} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                  <defs>
                    <linearGradient id="btGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%"  stopColor={m.total_pnl >= 0 ? '#3fb950' : '#f85149'} stopOpacity={0.3} />
                      <stop offset="95%" stopColor={m.total_pnl >= 0 ? '#3fb950' : '#f85149'} stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="#30363d" vertical={false} />
                  <XAxis dataKey="day" tick={{ fill: '#6e7681', fontSize: 10 }} axisLine={false} tickLine={false} />
                  <YAxis
                    tickFormatter={(v: number) => `₹${(v / 100000).toFixed(0)}L`}
                    tick={{ fill: '#6e7681', fontSize: 10 }}
                    axisLine={false}
                    tickLine={false}
                    width={52}
                  />
                  <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v: number) => [inr(v), 'Equity']} />
                  <Area
                    type="monotone"
                    dataKey="equity"
                    stroke={m.total_pnl >= 0 ? '#3fb950' : '#f85149'}
                    strokeWidth={2}
                    fill="url(#btGrad)"
                    dot
                  />
                </AreaChart>
              </ResponsiveContainer>
            </CardBody>
          </Card>

          {/* Trade log */}
          {backtestResult.reports && backtestResult.reports.length > 0 && (
            <div className="lg:col-span-2">
              <Card>
                <button
                  className="w-full flex items-center justify-between px-4 py-3 hover:bg-surface-elevated/30 transition-colors"
                  onClick={() => setTradeLogExpanded(v => !v)}
                >
                  <span className="text-sm font-semibold text-gray-200">Trade Log ({backtestResult.reports.length} records)</span>
                  <span className="text-xs text-gray-500">{tradeLogExpanded ? 'Collapse' : 'Expand'}</span>
                </button>
                {tradeLogExpanded && (
                  <Table
                    columns={[
                      { key: 'symbol', label: 'Symbol' },
                      { key: 'side', label: 'Side', render: (v) => <span className={String(v) === 'BUY' ? 'text-brand-green' : 'text-brand-red'}>{String(v)}</span> },
                      { key: 'strategy_name', label: 'Strategy' },
                      { key: 'pnl', label: 'P&L', align: 'right', render: (v) => (
                        <span className={Number(v) >= 0 ? 'text-brand-green' : 'text-brand-red'}>{inr(Number(v), 2)}</span>
                      )},
                    ]}
                    rows={backtestResult.reports as Record<string, unknown>[]}
                    keyFn={(r, i) => String(i)}
                    emptyMessage="No trade records"
                    compact
                  />
                )}
              </Card>
            </div>
          )}
        </div>
      )}

    </div>
  )
}
