import { useCallback, useEffect, useMemo, useState } from 'react'
import { clsx } from 'clsx'
import { Loader2, RefreshCw, Swords, Trophy } from 'lucide-react'
import { evaluateStrategies, getStrategyCatalog, runWalkForward } from '../api'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { KeyValueGrid, PageHeader, ProgressBar, StatTile } from '../components/shared/Command'
import { Table } from '../components/shared/Table'
import { Tag } from '../components/shared/Badge'
import { useStore } from '../store'
import { inr, pct } from '../utils'
import type { StrategyEvaluationResult, StrategyScore, WalkForwardResult } from '../types'

interface CatalogStrategy {
  name?: string
  strategy_name?: string
  family?: string
  description?: string
}

function scoreColor(score: number): 'green' | 'yellow' | 'red' {
  if (score >= 70) return 'green'
  if (score >= 45) return 'yellow'
  return 'red'
}

function scoreText(score: number) {
  if (score >= 70) return 'text-brand-green'
  if (score >= 45) return 'text-brand-yellow'
  return 'text-brand-red'
}

export function Tournament() {
  const result = useStore((s) => s.strategyResult)
  const setResult = useStore((s) => s.setStrategyResult)
  const setError = useStore((s) => s.setError)

  const [catalog, setCatalog] = useState<CatalogStrategy[]>([])
  const [selectedNames, setSelectedNames] = useState<string[]>([])
  const [underlyings, setUnderlyings] = useState('NIFTY,BANKNIFTY,RELIANCE,TCS')
  const [days, setDays] = useState(45)
  const [start, setStart] = useState('2026-01-01')
  const [loadingCatalog, setLoadingCatalog] = useState(false)
  const [running, setRunning] = useState(false)
  const [walkForward, setWalkForward] = useState<WalkForwardResult | null>(null)
  const [wfLoading, setWfLoading] = useState(false)

  const loadCatalog = useCallback(async () => {
    setLoadingCatalog(true)
    try {
      const res = await getStrategyCatalog()
      const strategies = (res.strategies as CatalogStrategy[]) ?? []
      setCatalog(strategies)
      if (selectedNames.length === 0) {
        setSelectedNames(strategies.slice(0, 5).map((s) => s.name ?? s.strategy_name ?? '').filter(Boolean))
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load strategy catalog')
    } finally {
      setLoadingCatalog(false)
    }
  }, [selectedNames.length, setError])

  useEffect(() => { loadCatalog() }, [loadCatalog])

  const runTournament = async () => {
    setRunning(true)
    try {
      const syms = underlyings.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean)
      const payload = {
        start,
        days,
        underlyings: syms,
        strategy_names: selectedNames.length ? selectedNames : undefined,
      }
      setResult(await evaluateStrategies(payload) as StrategyEvaluationResult)
      setWalkForward(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Strategy tournament failed')
    } finally {
      setRunning(false)
    }
  }

  const runChampionWalkForward = async () => {
    const champion = result?.best_strategy ?? result?.leaderboard?.[0]?.strategy_name
    if (!champion) return
    setWfLoading(true)
    try {
      const syms = underlyings.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean)
      setWalkForward(await runWalkForward({
        strategy_name: champion,
        start,
        total_days: Math.max(days, 60),
        train_days: 20,
        test_days: 10,
        underlyings: syms,
      }))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Walk-forward validation failed')
    } finally {
      setWfLoading(false)
    }
  }

  const champion = result?.leaderboard?.[0]
  const byFamily = useMemo(() => catalog.reduce((acc, strategy) => {
    const family = strategy.family ?? 'unknown'
    acc[family] = (acc[family] ?? 0) + 1
    return acc
  }, {} as Record<string, number>), [catalog])

  return (
    <div className="space-y-5">
      <PageHeader
        title="Strategy Tournament"
        subtitle="Champion/challenger validation across rule, neural-enhanced, and policy research lanes"
        icon={<Swords size={17} className="text-brand-orange" />}
        action={
          <button
            onClick={loadCatalog}
            disabled={loadingCatalog}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-surface-elevated border border-surface-border text-xs text-gray-400 hover:text-gray-200 disabled:opacity-50"
          >
            {loadingCatalog ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
            Catalog
          </button>
        }
      />

      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
        <StatTile label="Catalog" value={catalog.length} accent="orange" sub={`${Object.keys(byFamily).length} families`} />
        <StatTile label="Selected" value={selectedNames.length || 'ALL'} accent="blue" sub="challengers" />
        <StatTile label="Champion" value={champion?.strategy_name ?? '--'} accent={champion ? 'green' : 'gray'} sub={champion ? `rank ${champion.rank}` : 'run tournament'} />
        <StatTile label="WF Gate" value={walkForward ? (walkForward.degradation_detected ? 'CHECK' : 'PASS') : '--'} accent={walkForward ? (walkForward.degradation_detected ? 'yellow' : 'green') : 'gray'} sub="purged walk-forward" />
      </div>

      <Card className="border-brand-orange/20">
        <CardHeader title="Tournament Setup" subtitle="Backtest leaderboard with realistic costs and drawdown scoring" icon={<Trophy size={14} />} />
        <CardBody className="space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-[1fr_160px_160px_auto] gap-3">
            <label className="space-y-1">
              <span className="text-xs text-gray-400 uppercase tracking-wider">Underlyings</span>
              <input
                value={underlyings}
                onChange={(e) => setUnderlyings(e.target.value)}
                className="w-full bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded-md px-3 py-2 font-mono focus:outline-none focus:border-brand-orange"
              />
            </label>
            <label className="space-y-1">
              <span className="text-xs text-gray-400 uppercase tracking-wider">Start</span>
              <input
                type="date"
                value={start}
                onChange={(e) => setStart(e.target.value)}
                className="w-full bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded-md px-3 py-2 font-mono focus:outline-none focus:border-brand-orange"
              />
            </label>
            <label className="space-y-1">
              <span className="text-xs text-gray-400 uppercase tracking-wider">Days</span>
              <input
                type="number"
                min={5}
                max={252}
                value={days}
                onChange={(e) => setDays(Number(e.target.value))}
                className="w-full bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded-md px-3 py-2 font-mono focus:outline-none focus:border-brand-orange"
              />
            </label>
            <button
              onClick={runTournament}
              disabled={running}
              className="self-end flex items-center justify-center gap-2 px-4 py-2 rounded-md bg-brand-orange/15 border border-brand-orange/30 text-brand-orange text-xs font-semibold hover:bg-brand-orange/25 disabled:opacity-50"
            >
              {running ? <Loader2 size={12} className="animate-spin" /> : <Swords size={12} />}
              Run
            </button>
          </div>

          <div className="flex flex-wrap gap-2">
            {catalog.slice(0, 18).map((strategy) => {
              const name = strategy.name ?? strategy.strategy_name ?? ''
              const active = selectedNames.includes(name)
              return (
                <button
                  key={name}
                  onClick={() => setSelectedNames((current) => active ? current.filter((item) => item !== name) : [...current, name])}
                  className={clsx(
                    'px-2.5 py-1.5 rounded-md text-xs border transition-colors',
                    active ? 'bg-brand-orange/15 border-brand-orange/30 text-brand-orange' : 'bg-surface-elevated border-surface-border text-gray-400 hover:text-gray-200',
                  )}
                >
                  {name}
                </button>
              )
            })}
          </div>
        </CardBody>
      </Card>

      <div className="grid grid-cols-1 xl:grid-cols-[1.2fr_.8fr] gap-5">
        <Card>
          <CardHeader title="Leaderboard" subtitle={`${result?.leaderboard?.length ?? 0} ranked strategies`} />
          <Table<StrategyScore>
            columns={[
              { key: 'rank', label: '#', align: 'right' },
              { key: 'strategy_name', label: 'Strategy', render: (v, row) => <span className={row.rank === 1 ? 'text-brand-green font-semibold' : 'text-brand-blue'}>{String(v)}</span> },
              { key: 'family', label: 'Family' },
              { key: 'score', label: 'Score', align: 'right', render: (v) => <span className={scoreText(Number(v))}>{Number(v).toFixed(1)}</span> },
              { key: 'trade_count', label: 'Trades', align: 'right' },
              { key: 'metrics', label: 'P&L', align: 'right', render: (_, row) => <span className={row.metrics.total_pnl >= 0 ? 'text-brand-green' : 'text-brand-red'}>{inr(row.metrics.total_pnl)}</span> },
              { key: 'metrics', label: 'PF', align: 'right', render: (_, row) => row.metrics.profit_factor.toFixed(2) },
              { key: 'metrics', label: 'DD', align: 'right', render: (_, row) => pct(row.metrics.max_drawdown * 100, 1) },
            ]}
            rows={result?.leaderboard ?? []}
            keyFn={(row) => row.strategy_name}
            emptyMessage="Run a tournament to rank strategies"
            compact
          />
        </Card>

        <Card>
          <CardHeader
            title="Champion Gate"
            subtitle={champion?.strategy_name ?? 'No champion yet'}
            action={
              <button
                onClick={runChampionWalkForward}
                disabled={wfLoading || !champion}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-surface-elevated border border-surface-border text-xs text-gray-400 hover:text-gray-200 disabled:opacity-50"
              >
                {wfLoading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
                Walk-forward
              </button>
            }
          />
          <CardBody className="space-y-4">
            {champion ? (
              <>
                <KeyValueGrid
                  columns="grid-cols-2"
                  items={[
                    { label: 'Return', value: pct(champion.metrics.return_pct, 2), accent: champion.metrics.return_pct >= 0 ? 'green' : 'red' },
                    { label: 'Sharpe-like', value: champion.metrics.sharpe_like.toFixed(2), accent: 'blue' },
                    { label: 'Win rate', value: pct(champion.metrics.win_rate * 100, 1), accent: 'green' },
                    { label: 'Profit factor', value: champion.metrics.profit_factor.toFixed(2), accent: champion.metrics.profit_factor > 1 ? 'green' : 'yellow' },
                  ]}
                />
                <ProgressBar label="Promotion score" value={champion.score} accent={scoreColor(champion.score)} right={champion.score.toFixed(1)} />
                <div className="flex flex-wrap gap-2">
                  <Tag label="shadow first" color="yellow" />
                  <Tag label="paper required" color="cyan" />
                  <Tag label="manual live approval" color="red" />
                </div>
              </>
            ) : (
              <div className="text-xs text-gray-500 text-center py-8">No champion selected</div>
            )}

            {walkForward && (
              <div className="pt-4 border-t border-surface-border space-y-3">
                <KeyValueGrid
                  columns="grid-cols-2"
                  items={[
                    { label: 'Windows', value: walkForward.window_count, accent: 'blue' },
                    { label: 'Mean test return', value: pct(walkForward.mean_test_return, 2), accent: walkForward.mean_test_return >= 0 ? 'green' : 'red' },
                    { label: 'Mean test Sharpe', value: walkForward.mean_test_sharpe.toFixed(2), accent: 'cyan' },
                    { label: 'Decay', value: walkForward.degradation_detected ? 'YES' : 'NO', accent: walkForward.degradation_detected ? 'red' : 'green' },
                  ]}
                />
              </div>
            )}
          </CardBody>
        </Card>
      </div>
    </div>
  )
}
