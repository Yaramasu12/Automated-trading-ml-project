import { useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { BookOpen, Loader2, Play } from 'lucide-react'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Badge } from '../components/shared/Badge'
import { useStore } from '../store'
import { runBacktest } from '../api'
import { inr, pct, num } from '../utils'

const TOOLTIP_STYLE = {
  backgroundColor: '#161b22',
  border: '1px solid #30363d',
  borderRadius: 6,
  fontSize: 12,
  color: '#e6edf3',
}

export function Backtest() {
  const backtestResult = useStore((s) => s.backtestResult)
  const setBacktestResult = useStore((s) => s.setBacktestResult)
  const loading = useStore((s) => s.loading.backtest)
  const setLoading = useStore((s) => s.setLoading)
  const setError = useStore((s) => s.setError)

  const [underlyings, setUnderlyings] = useState('NIFTY, BANKNIFTY')
  const [days, setDays] = useState('30')
  const [capital, setCapital] = useState('2000000')
  const [strategy, setStrategy] = useState('')

  async function run() {
    setLoading('backtest', true)
    setError(null)
    try {
      const payload: Record<string, unknown> = {
        underlyings: underlyings.split(',').map((s) => s.trim()).filter(Boolean),
        days: parseInt(days, 10),
        starting_capital: parseFloat(capital),
      }
      if (strategy.trim()) payload.strategy_name = strategy.trim()
      const result = await runBacktest(payload)
      setBacktestResult(result)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading('backtest', false)
    }
  }

  const m = backtestResult?.metrics

  const summaryItems = m
    ? [
        { label: 'Starting Capital', value: inr(m.starting_capital) },
        { label: 'Ending Equity', value: inr(m.ending_equity), accent: m.ending_equity >= m.starting_capital ? 'text-brand-green' : 'text-brand-red' },
        { label: 'Total P&L', value: inr(m.total_pnl), accent: m.total_pnl >= 0 ? 'text-brand-green' : 'text-brand-red' },
        { label: 'Return', value: pct(m.return_pct), accent: m.return_pct >= 0 ? 'text-brand-green' : 'text-brand-red' },
        { label: 'Max Drawdown', value: pct(m.max_drawdown * 100), accent: 'text-brand-red' },
        { label: 'Trades', value: m.trade_count.toString() },
        { label: 'Win Rate', value: pct(m.win_rate * 100) },
        { label: 'Profit Factor', value: num(m.profit_factor, 2), accent: m.profit_factor >= 1 ? 'text-brand-green' : 'text-brand-red' },
        { label: 'Sharpe-like', value: num(m.sharpe_like, 2), accent: m.sharpe_like >= 1 ? 'text-brand-cyan' : 'text-gray-400' },
      ]
    : []

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-lg font-bold text-gray-100">Backtest</h1>
        <p className="text-xs text-gray-500 mt-0.5">Simulate strategy performance on historical data</p>
      </div>

      {/* Controls */}
      <Card>
        <CardBody className="flex flex-wrap items-end gap-4">
          <div className="flex-1 min-w-48">
            <label className="block text-xs text-gray-500 mb-1">Underlyings</label>
            <input
              value={underlyings}
              onChange={(e) => setUnderlyings(e.target.value)}
              className="w-full bg-surface-elevated border border-surface-border rounded-md px-3 py-1.5 text-sm text-gray-200 font-mono focus:outline-none focus:border-brand-blue"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Strategy (optional)</label>
            <input
              value={strategy}
              onChange={(e) => setStrategy(e.target.value)}
              placeholder="e.g. iron_condor"
              className="w-40 bg-surface-elevated border border-surface-border rounded-md px-3 py-1.5 text-sm text-gray-200 font-mono focus:outline-none focus:border-brand-blue placeholder-gray-600"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Days</label>
            <input
              type="number"
              value={days}
              onChange={(e) => setDays(e.target.value)}
              className="w-20 bg-surface-elevated border border-surface-border rounded-md px-3 py-1.5 text-sm text-gray-200 font-mono focus:outline-none focus:border-brand-blue"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Capital (₹)</label>
            <input
              type="number"
              value={capital}
              onChange={(e) => setCapital(e.target.value)}
              className="w-36 bg-surface-elevated border border-surface-border rounded-md px-3 py-1.5 text-sm text-gray-200 font-mono focus:outline-none focus:border-brand-blue"
            />
          </div>
          <button
            onClick={run}
            disabled={loading}
            className="flex items-center gap-2 px-4 py-1.5 rounded-md bg-brand-blue text-surface text-sm font-medium hover:bg-brand-blue/80 disabled:opacity-50 transition-colors"
          >
            {loading ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
            Run Backtest
          </button>
        </CardBody>
      </Card>

      {loading && (
        <div className="flex flex-col items-center justify-center py-20 text-gray-500">
          <Loader2 size={32} className="animate-spin mb-3" />
          <p className="text-sm">Running backtest simulation…</p>
        </div>
      )}

      {backtestResult && !loading && (
        <>
          {/* Config badge row */}
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="blue">{backtestResult.config.days}d</Badge>
            {backtestResult.config.underlyings.map((u) => (
              <Badge key={u} variant="gray">{u}</Badge>
            ))}
            <Badge variant="green">Start: {backtestResult.config.start}</Badge>
          </div>

          {/* Metrics grid */}
          <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-3">
            {summaryItems.map(({ label, value, accent }) => (
              <div key={label} className="bg-surface-card border border-surface-border rounded-lg p-4">
                <div className="text-xs text-gray-500 mb-1 uppercase tracking-wider">{label}</div>
                <div className={`text-lg font-bold font-mono ${accent ?? 'text-gray-100'}`}>{value}</div>
              </div>
            ))}
          </div>

          {/* Reports table */}
          {backtestResult.reports.length > 0 && (
            <Card>
              <CardHeader title="Trade Reports" subtitle={`${backtestResult.reports.length} entries`} />
              <div className="overflow-x-auto max-h-64 overflow-y-auto">
                <pre className="text-xs text-gray-400 p-4 font-mono whitespace-pre-wrap">
                  {JSON.stringify(backtestResult.reports.slice(0, 20), null, 2)}
                </pre>
              </div>
            </Card>
          )}
        </>
      )}

      {!backtestResult && !loading && (
        <div className="flex flex-col items-center justify-center py-20 text-gray-600">
          <BookOpen size={36} className="mb-3 opacity-40" />
          <p className="text-sm">Configure parameters and run a backtest</p>
        </div>
      )}
    </div>
  )
}
