import { useState, useCallback } from 'react'
import { BarChart2, Loader2, TrendingUp, Award } from 'lucide-react'
import { clsx } from 'clsx'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Table } from '../components/shared/Table'
import { Tag } from '../components/shared/Badge'
import { ShortVolPanel } from '../components/ShortVolPanel'
import { useStore } from '../store'
import { evaluateStrategies, runWalkForward } from '../api'
import { pct, num } from '../utils'
import type { StrategyScore } from '../types'

const UNDERLYINGS = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'RELIANCE', 'TCS', 'INFY', 'HDFCBANK']
const DAY_OPTIONS = [7, 14, 30, 60]
const STRATEGY_FAMILIES = ['All', 'Options', 'Momentum', 'Arbitrage']

export function Strategies() {
  const strategyResult    = useStore((s) => s.strategyResult)
  const setStrategyResult = useStore((s) => s.setStrategyResult)
  const walkForwardResult = useStore((s) => s.walkForwardResult)
  const setWalkForwardResult = useStore((s) => s.setWalkForwardResult)
  const loading           = useStore((s) => s.loading.strategies)
  const setLoading        = useStore((s) => s.setLoading)
  const setError          = useStore((s) => s.setError)

  const [selectedUnderlyings, setSelectedUnderlyings] = useState<string[]>(['NIFTY', 'BANKNIFTY'])
  const [days, setDays]                 = useState(14)
  const [familyFilter, setFamilyFilter] = useState('All')
  const [walkForward, setWalkForward]   = useState(false)

  function toggleUnderlying(u: string) {
    setSelectedUnderlyings(prev =>
      prev.includes(u) ? prev.filter(x => x !== u) : [...prev, u]
    )
  }

  const runEval = useCallback(async () => {
    if (selectedUnderlyings.length === 0) return
    setLoading('strategies', true)
    setError(null)
    try {
      const payload = {
        days,
        underlyings: selectedUnderlyings,
        family: familyFilter !== 'All' ? familyFilter : undefined,
      }
      const res = await evaluateStrategies(payload)
      setStrategyResult(res)
      if (walkForward && res.best_strategy) {
        setLoading('walkForward', true)
        try {
          const wfRes = await runWalkForward({ strategy_name: res.best_strategy, days, underlyings: selectedUnderlyings })
          setWalkForwardResult(wfRes)
        } catch { /* ignore */ }
        finally { setLoading('walkForward', false) }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Evaluation failed')
    } finally {
      setLoading('strategies', false)
    }
  }, [selectedUnderlyings, days, familyFilter, walkForward, setLoading, setError, setStrategyResult, setWalkForwardResult])

  const filtered = strategyResult?.leaderboard.filter(
    s => familyFilter === 'All' || s.family.toLowerCase().includes(familyFilter.toLowerCase())
  ) ?? []

  const leaderboardColumns = [
    { key: 'rank', label: '#', align: 'center' as const, render: (v: unknown, r: StrategyScore) => (
      <span className={clsx('font-bold font-mono', r.rank === 1 ? 'text-brand-yellow' : 'text-gray-400')}>
        {String(v)}
      </span>
    )},
    { key: 'strategy_name', label: 'Strategy', render: (v: unknown, r: StrategyScore) => (
      <span className={r.rank === 1 ? 'text-brand-yellow font-semibold' : 'text-gray-200'}>{String(v)}</span>
    )},
    { key: 'family', label: 'Family', render: (v: unknown) => (
      <Tag label={String(v)} color="purple" />
    )},
    { key: 'sharpe', label: 'Sharpe', align: 'right' as const, render: (_: unknown, r: StrategyScore) => (
      <span className={clsx('font-mono', r.metrics.sharpe_like > 1 ? 'text-brand-green' : 'text-gray-300')}>
        {r.metrics.sharpe_like.toFixed(2)}
      </span>
    )},
    { key: 'win_rate', label: 'Win Rate', align: 'right' as const, render: (_: unknown, r: StrategyScore) => (
      <span className={clsx('font-mono', r.metrics.win_rate > 0.6 ? 'text-brand-green' : 'text-gray-300')}>
        {pct(r.metrics.win_rate * 100)}
      </span>
    )},
    { key: 'return', label: 'Return%', align: 'right' as const, render: (_: unknown, r: StrategyScore) => (
      <span className={clsx('font-mono', r.metrics.return_pct > 0 ? 'text-brand-green' : 'text-brand-red')}>
        {pct(r.metrics.return_pct)}
      </span>
    )},
    { key: 'profit_factor', label: 'Profit Factor', align: 'right' as const, render: (_: unknown, r: StrategyScore) => (
      <span className={clsx('font-mono', r.metrics.profit_factor > 1.5 ? 'text-brand-green' : 'text-gray-300')}>
        {r.metrics.profit_factor.toFixed(2)}
      </span>
    )},
    { key: 'drawdown', label: 'Max DD', align: 'right' as const, render: (_: unknown, r: StrategyScore) => (
      <span className={clsx('font-mono', r.metrics.max_drawdown > 0.1 ? 'text-brand-red' : 'text-gray-300')}>
        {pct(r.metrics.max_drawdown * 100)}
      </span>
    )},
    { key: 'trade_count', label: 'Trades', align: 'right' as const, render: (_: unknown, r: StrategyScore) => (
      <span className="font-mono text-gray-400">{r.metrics.trade_count}</span>
    )},
  ]

  return (
    <div className="space-y-5">

      {/* Header */}
      <div className="flex items-center gap-2">
        <BarChart2 size={16} className="text-brand-blue" />
        <div>
          <h1 className="text-lg font-bold text-gray-100">Strategy Evaluation</h1>
          <p className="text-xs text-gray-500 mt-0.5">Leaderboard across instruments and lookback periods</p>
        </div>
      </div>

      {/* Live short-vol (VRP) strategy status */}
      <ShortVolPanel />

      {/* Form */}
      <Card>
        <CardHeader title="Evaluation Parameters" icon={<BarChart2 size={14} />} />
        <CardBody className="space-y-4">
          {/* Underlyings */}
          <div>
            <label className="block text-xs text-gray-400 mb-2 font-semibold uppercase tracking-wider">Underlyings</label>
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

          {/* Days */}
          <div>
            <label className="block text-xs text-gray-400 mb-2 font-semibold uppercase tracking-wider">Lookback</label>
            <div className="flex gap-1.5">
              {DAY_OPTIONS.map((d) => (
                <button
                  key={d}
                  onClick={() => setDays(d)}
                  className={clsx(
                    'px-3 py-1 rounded text-xs font-mono border transition-colors',
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

          {/* Strategy family filter */}
          <div>
            <label className="block text-xs text-gray-400 mb-2 font-semibold uppercase tracking-wider">Family Filter</label>
            <div className="flex gap-1.5">
              {STRATEGY_FAMILIES.map((f) => (
                <button
                  key={f}
                  onClick={() => setFamilyFilter(f)}
                  className={clsx(
                    'px-3 py-1 rounded text-xs font-mono border transition-colors',
                    familyFilter === f
                      ? 'bg-brand-purple/15 border-brand-purple/30 text-brand-purple'
                      : 'bg-surface-elevated border-surface-border text-gray-400 hover:text-gray-200',
                  )}
                >
                  {f}
                </button>
              ))}
            </div>
          </div>

          {/* Walk-Forward toggle */}
          <div className="flex items-center gap-3">
            <button
              onClick={() => setWalkForward(v => !v)}
              className={clsx(
                'w-10 h-5 rounded-full border transition-all relative',
                walkForward ? 'bg-brand-blue border-brand-blue' : 'bg-surface-elevated border-surface-border',
              )}
            >
              <span className={clsx(
                'absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all',
                walkForward ? 'left-[22px]' : 'left-0.5',
              )} />
            </button>
            <span className="text-xs text-gray-400">Walk-Forward Analysis</span>
          </div>

          <button
            onClick={runEval}
            disabled={loading || selectedUnderlyings.length === 0}
            className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-blue/15 border border-brand-blue/30 text-brand-blue text-sm font-medium hover:bg-brand-blue/25 disabled:opacity-50 transition-colors"
          >
            {loading ? <Loader2 size={14} className="animate-spin" /> : <TrendingUp size={14} />}
            Evaluate Strategies
          </button>
        </CardBody>
      </Card>

      {/* Results */}
      {strategyResult && (
        <div className="space-y-4">
          {/* Best strategy badge */}
          {strategyResult.best_strategy && (
            <div className="flex items-center gap-3 p-3 rounded-lg bg-brand-yellow/5 border border-brand-yellow/20">
              <Award size={16} className="text-brand-yellow" />
              <span className="text-sm text-gray-300">Best Strategy:</span>
              <span className="text-sm font-bold text-brand-yellow">{strategyResult.best_strategy}</span>
            </div>
          )}

          <Card>
            <CardHeader
              title="Strategy Leaderboard"
              subtitle={`${filtered.length} strategies · ${days}d lookback`}
              icon={<BarChart2 size={14} />}
            />
            <Table
              columns={leaderboardColumns as Parameters<typeof Table>[0]['columns']}
              rows={filtered as unknown as Record<string, unknown>[]}
              keyFn={(r) => String((r as unknown as StrategyScore).strategy_name)}
              emptyMessage="No strategies evaluated yet"
              compact
            />
          </Card>

          {/* Walk-Forward results */}
          {walkForwardResult && (
            <Card>
              <CardHeader
                title={`Walk-Forward: ${walkForwardResult.strategy_name}`}
                subtitle={`${walkForwardResult.window_count} windows`}
                icon={<TrendingUp size={14} />}
                action={
                  walkForwardResult.degradation_detected ? (
                    <Tag label="Degradation Detected" color="red" />
                  ) : (
                    <Tag label="Stable" color="green" />
                  )
                }
              />
              <CardBody>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
                  <div className="bg-surface-elevated rounded p-2">
                    <div className="text-[10px] text-gray-500 uppercase">Mean Sharpe</div>
                    <div className="text-sm font-bold font-mono text-gray-100 mt-1">
                      {walkForwardResult.mean_test_sharpe.toFixed(2)}
                    </div>
                  </div>
                  <div className="bg-surface-elevated rounded p-2">
                    <div className="text-[10px] text-gray-500 uppercase">Mean Return</div>
                    <div className="text-sm font-bold font-mono text-gray-100 mt-1">
                      {pct(walkForwardResult.mean_test_return)}
                    </div>
                  </div>
                  <div className="bg-surface-elevated rounded p-2">
                    <div className="text-[10px] text-gray-500 uppercase">Windows</div>
                    <div className="text-sm font-bold font-mono text-gray-100 mt-1">{walkForwardResult.window_count}</div>
                  </div>
                  <div className="bg-surface-elevated rounded p-2">
                    <div className="text-[10px] text-gray-500 uppercase">Total Days</div>
                    <div className="text-sm font-bold font-mono text-gray-100 mt-1">{walkForwardResult.total_days}</div>
                  </div>
                </div>
                <Table
                  columns={[
                    { key: 'window_index', label: 'Window', align: 'center' },
                    { key: 'train_start', label: 'Train Start' },
                    { key: 'test_start', label: 'Test Start' },
                    {
                      key: 'train_return', label: 'Train Return', align: 'right',
                      render: (_: unknown, r: Record<string, unknown>) => {
                        const wf = r as { train_metrics: { return_pct: number } }
                        return <span className="font-mono">{pct(wf.train_metrics.return_pct)}</span>
                      },
                    },
                    {
                      key: 'test_return', label: 'Test Return', align: 'right',
                      render: (_: unknown, r: Record<string, unknown>) => {
                        const wf = r as { test_metrics: { return_pct: number } }
                        return <span className={clsx('font-mono', wf.test_metrics.return_pct > 0 ? 'text-brand-green' : 'text-brand-red')}>
                          {pct(wf.test_metrics.return_pct)}
                        </span>
                      },
                    },
                  ]}
                  rows={walkForwardResult.windows as unknown as Record<string, unknown>[]}
                  keyFn={(r) => String((r as { window_index: number }).window_index)}
                  emptyMessage="No window data"
                  compact
                />
              </CardBody>
            </Card>
          )}
        </div>
      )}

    </div>
  )
}
