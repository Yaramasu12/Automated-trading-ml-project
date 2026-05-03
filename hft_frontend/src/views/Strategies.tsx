import { useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { BarChart2, Loader2, Play, Trophy } from 'lucide-react'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Badge } from '../components/shared/Badge'
import { Table } from '../components/shared/Table'
import { useStore } from '../store'
import { evaluateStrategies, getStrategyCatalog } from '../api'
import { inr, pct, num } from '../utils'
import type { StrategyScore } from '../types'
import { useEffect } from 'react'

const TOOLTIP_STYLE = {
  backgroundColor: '#161b22',
  border: '1px solid #30363d',
  borderRadius: 6,
  fontSize: 12,
  color: '#e6edf3',
}

const DEFAULT_UNDERLYINGS = 'NIFTY, BANKNIFTY'

export function Strategies() {
  const strategyResult = useStore((s) => s.strategyResult)
  const setStrategyResult = useStore((s) => s.setStrategyResult)
  const loadingEval = useStore((s) => s.loading.strategies)
  const setLoading = useStore((s) => s.setLoading)
  const setError = useStore((s) => s.setError)

  const [underlyings, setUnderlyings] = useState(DEFAULT_UNDERLYINGS)
  const [days, setDays] = useState('30')
  const [capital, setCapital] = useState('2000000')
  const [catalogCount, setCatalogCount] = useState<number | null>(null)

  useEffect(() => {
    getStrategyCatalog()
      .then((r) => setCatalogCount(r.count))
      .catch(() => {})
  }, [])

  async function evaluate() {
    setLoading('strategies', true)
    setError(null)
    try {
      const result = await evaluateStrategies({
        underlyings: underlyings.split(',').map((s) => s.trim()).filter(Boolean),
        days: parseInt(days, 10),
        starting_capital: parseFloat(capital),
      })
      setStrategyResult(result)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading('strategies', false)
    }
  }

  const leaderboard = strategyResult?.leaderboard ?? []
  const chartData = leaderboard
    .slice(0, 12)
    .map((s) => ({ name: s.strategy_name.replace(/_/g, ' '), score: parseFloat(s.score.toFixed(2)), pnl: s.metrics.total_pnl }))

  const columns = [
    {
      key: 'rank', header: '#', align: 'center' as const,
      render: (r: StrategyScore) => (
        <span className={r.rank === 1 ? 'text-brand-yellow font-bold' : 'text-gray-500'}>{r.rank}</span>
      ),
    },
    {
      key: 'name', header: 'Strategy',
      render: (r: StrategyScore) => (
        <div>
          <div className="text-gray-200 font-medium">{r.strategy_name.replace(/_/g, ' ')}</div>
          <div className="text-[10px] text-gray-500">{r.family}</div>
        </div>
      ),
    },
    {
      key: 'score', header: 'Score', align: 'right' as const,
      render: (r: StrategyScore) => (
        <span className="font-mono font-bold text-brand-cyan">{r.score.toFixed(2)}</span>
      ),
    },
    {
      key: 'return', header: 'Return', align: 'right' as const,
      render: (r: StrategyScore) => (
        <span className={r.metrics.return_pct >= 0 ? 'text-brand-green font-mono' : 'text-brand-red font-mono'}>
          {pct(r.metrics.return_pct)}
        </span>
      ),
    },
    {
      key: 'pnl', header: 'P&L', align: 'right' as const,
      render: (r: StrategyScore) => (
        <span className={r.metrics.total_pnl >= 0 ? 'text-brand-green font-mono' : 'text-brand-red font-mono'}>
          {inr(r.metrics.total_pnl)}
        </span>
      ),
    },
    {
      key: 'dd', header: 'Max DD', align: 'right' as const,
      render: (r: StrategyScore) => (
        <span className="font-mono text-brand-red">{pct(r.metrics.max_drawdown * 100)}</span>
      ),
    },
    {
      key: 'wr', header: 'Win %', align: 'right' as const,
      render: (r: StrategyScore) => (
        <span className="font-mono text-gray-300">{pct(r.metrics.win_rate * 100)}</span>
      ),
    },
    {
      key: 'sharpe', header: 'Sharpe', align: 'right' as const,
      render: (r: StrategyScore) => (
        <span className="font-mono text-gray-300">{r.metrics.sharpe_like.toFixed(2)}</span>
      ),
    },
    {
      key: 'trades', header: 'Trades', align: 'right' as const,
      render: (r: StrategyScore) => <span className="font-mono">{r.trade_count}</span>,
    },
  ]

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-gray-100">Strategy Evaluation</h1>
          <p className="text-xs text-gray-500 mt-0.5">
            {catalogCount !== null ? `${catalogCount} strategies in catalog` : 'Loading catalog…'}
          </p>
        </div>
        {strategyResult?.best_strategy && (
          <div className="flex items-center gap-2 px-3 py-1.5 bg-brand-yellow/10 border border-brand-yellow/30 rounded-lg">
            <Trophy size={14} className="text-brand-yellow" />
            <span className="text-xs text-brand-yellow font-medium">{strategyResult.best_strategy}</span>
          </div>
        )}
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
            onClick={evaluate}
            disabled={loadingEval}
            className="flex items-center gap-2 px-4 py-1.5 rounded-md bg-brand-blue text-surface text-sm font-medium hover:bg-brand-blue/80 disabled:opacity-50 transition-colors"
          >
            {loadingEval ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
            Evaluate
          </button>
        </CardBody>
      </Card>

      {leaderboard.length > 0 && (
        <>
          {/* Score chart */}
          <Card>
            <CardHeader title="Strategy Score Comparison" subtitle="Higher is better" />
            <CardBody className="h-56 !p-2">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={chartData} margin={{ top: 4, right: 8, bottom: 24, left: 0 }} layout="vertical">
                  <CartesianGrid strokeDasharray="3 3" stroke="#30363d" horizontal={false} />
                  <XAxis type="number" tick={{ fill: '#6e7681', fontSize: 10 }} axisLine={false} tickLine={false} />
                  <YAxis
                    type="category"
                    dataKey="name"
                    tick={{ fill: '#6e7681', fontSize: 9 }}
                    axisLine={false}
                    tickLine={false}
                    width={100}
                  />
                  <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v: number) => [v.toFixed(2), 'Score']} />
                  <Bar dataKey="score" radius={[0, 4, 4, 0]}>
                    {chartData.map((entry, index) => (
                      <Cell key={index} fill={index === 0 ? '#d29922' : '#58a6ff'} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </CardBody>
          </Card>

          {/* Leaderboard table */}
          <Card>
            <CardHeader
              title="Leaderboard"
              subtitle={`${leaderboard.length} strategies evaluated · ${strategyResult?.days}d window`}
            />
            <Table
              columns={columns}
              data={leaderboard}
              keyFn={(r) => r.strategy_name}
              compact
            />
          </Card>
        </>
      )}

      {!strategyResult && !loadingEval && (
        <div className="flex flex-col items-center justify-center py-20 text-gray-600">
          <BarChart2 size={36} className="mb-3 opacity-40" />
          <p className="text-sm">Run evaluation to rank strategies</p>
        </div>
      )}

      {loadingEval && (
        <div className="flex flex-col items-center justify-center py-20 text-gray-500">
          <Loader2 size={32} className="animate-spin mb-3" />
          <p className="text-sm">Evaluating strategies…</p>
        </div>
      )}
    </div>
  )
}
