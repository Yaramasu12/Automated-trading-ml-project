/**
 * Research — visualises the data this platform actually HAS.
 *
 * WHY THIS SCREEN EXISTS
 * -----------------------
 * The dashboard was charting live portfolio snapshots: a dead-flat line at
 * the starting capital, because paper trading has executed 0 fills. Meanwhile
 * the genuinely rich datasets were invisible:
 *
 *   ~14 MB of real Angel One daily bars (44 symbols x ~7 yrs)  -> no endpoint
 *   20 validation-gate results in Postgres                     -> no screen
 *   strategy promotion-ladder state                            -> no screen
 *
 * So the UI looked broken while the database looked empty, and neither was
 * true — the interesting data simply had no path to the screen. This screen
 * is that path: price history you can actually inspect, and the validation
 * scorecard that decides what is allowed to trade.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { clsx } from 'clsx'
import { AlertTriangle, CandlestickChart, FlaskConical, RefreshCw, ShieldCheck } from 'lucide-react'

import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { MetricCard } from '../components/shared/MetricCard'
import { PageHeader } from '../components/shared/PageHeader'
import { EmptyState } from '../components/shared/States'
import { inr, pct } from '../utils'
import {
  getBacktestGates, getResearchCandles, getResearchSymbols, getStrategyPromotions,
  type GateRow, type ResearchCandle,
} from '../api'

const LADDER = ['research', 'shadow', 'paper', 'live_canary', 'live_approved']

export function Research() {
  const [symbols, setSymbols] = useState<string[]>([])
  const [symbol, setSymbol] = useState('NIFTY')
  const [candles, setCandles] = useState<ResearchCandle[]>([])
  const [totalBars, setTotalBars] = useState(0)
  const [gates, setGates] = useState<GateRow[]>([])
  const [promotions, setPromotions] = useState<Record<string, unknown>[]>([])
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const loadMeta = useCallback(async () => {
    const r = await Promise.allSettled([
      getResearchSymbols(), getBacktestGates(80), getStrategyPromotions(),
    ])
    if (r[0].status === 'fulfilled') setSymbols(r[0].value.symbols ?? [])
    if (r[1].status === 'fulfilled') setGates(r[1].value.results ?? [])
    if (r[2].status === 'fulfilled') setPromotions(r[2].value.promotions ?? [])
    if (r.every((x) => x.status === 'rejected')) setError('Backend unreachable.')
  }, [])

  const loadCandles = useCallback(async (sym: string) => {
    setBusy(true)
    try {
      const res = await getResearchCandles(sym, 1200)
      setCandles(res.candles ?? [])
      setTotalBars(res.total_available ?? res.count ?? 0)
      setError(null)
    } catch {
      setCandles([])
      setError(`No cached history for ${sym}.`)
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => { loadMeta() }, [loadMeta])
  useEffect(() => { loadCandles(symbol) }, [symbol, loadCandles])

  const series = useMemo(
    () => candles.map((c) => ({ t: c.t, close: c.close, volume: c.volume })),
    [candles],
  )

  const stats = useMemo(() => {
    if (candles.length < 2) return null
    const first = candles[0].close
    const last = candles[candles.length - 1].close
    const closes = candles.map((c) => c.close)
    const rets: number[] = []
    for (let i = 1; i < closes.length; i++) {
      if (closes[i - 1] > 0) rets.push(Math.log(closes[i] / closes[i - 1]))
    }
    const mean = rets.reduce((a, b) => a + b, 0) / (rets.length || 1)
    const sd = Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / Math.max(1, rets.length - 1))
    let peak = -Infinity, mdd = 0
    for (const c of closes) { peak = Math.max(peak, c); if (peak > 0) mdd = Math.max(mdd, (peak - c) / peak) }
    const years = candles.length / 252
    return {
      first, last,
      totalReturn: last / first - 1,
      cagr: years > 0 ? (last / first) ** (1 / years) - 1 : 0,
      vol: sd * Math.sqrt(252),
      maxDD: mdd,
    }
  }, [candles])

  // Gate results grouped by backtest run, newest first.
  const gateRuns = useMemo(() => {
    const by = new Map<string, GateRow[]>()
    for (const g of gates) {
      const k = `${g.strategy_id}::${g.backtest_id}`
      if (!by.has(k)) by.set(k, [])
      by.get(k)!.push(g)
    }
    return [...by.entries()]
      .map(([k, rows]) => ({
        key: k,
        strategy: rows[0].strategy_id,
        runAt: rows[0].run_at,
        rows,
        allPassed: rows.every((r) => r.result === 'pass'),
      }))
      .sort((a, b) => (a.runAt < b.runAt ? 1 : -1))
  }, [gates])

  return (
    <div className="space-y-4">
      <PageHeader
        title="Research"
        subtitle="Market history · validation gates · promotion ladder"
        icon={<FlaskConical size={18} />}
        actions={
          <button
            onClick={() => { loadMeta(); loadCandles(symbol) }}
            className="flex items-center gap-1.5 rounded border border-surface-border px-2.5 py-1
                       text-[11px] text-ink-muted hover:bg-surface-elevated"
          >
            <RefreshCw size={12} className={busy ? 'animate-spin' : ''} /> Refresh
          </button>
        }
      />

      {error && (
        <div className="flex items-center gap-2 rounded border border-brand-yellow/40
                        bg-brand-yellow/10 px-3 py-2 text-[12px] text-brand-yellow">
          <AlertTriangle size={14} /> {error}
        </div>
      )}

      {/* ── Price history ─────────────────────────────────────────────── */}
      <Card>
        <CardHeader
          title="Price history"
          subtitle={`${totalBars.toLocaleString()} daily bars cached · ${symbols.length} symbols available`}
          icon={<CandlestickChart size={14} />}
          action={
            <select
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              className="rounded border border-surface-border bg-surface-inset
                         px-2 py-1 text-[11px] text-ink"
            >
              {symbols.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          }
        />
        <CardBody>
          {stats && (
            <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-5">
              <MetricCard label="Last" value={inr(stats.last)} />
              <MetricCard label="Total return" value={pct(stats.totalReturn)}
                          color={stats.totalReturn >= 0 ? 'green' : 'red'} />
              <MetricCard label="CAGR" value={pct(stats.cagr)}
                          color={stats.cagr >= 0 ? 'green' : 'red'} />
              <MetricCard label="Ann. vol" value={pct(stats.vol)} />
              <MetricCard label="Max DD" value={pct(stats.maxDD)} color="red" />
            </div>
          )}
          {series.length < 2 ? (
            <EmptyState
              title={busy ? 'Loading history…' : 'No cached history'}
              hint="Deep daily history is fetched by the backtest tooling into data/historical/."
            />
          ) : (
            <ResponsiveContainer width="100%" height={300}>
              <AreaChart data={series} margin={{ top: 6, right: 8, bottom: 0, left: 10 }}>
                <defs>
                  <linearGradient id="px" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#39c5cf" stopOpacity={0.35} />
                    <stop offset="100%" stopColor="#39c5cf" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#252c38" strokeDasharray="2 4" vertical={false} />
                <XAxis dataKey="t" stroke="#6a7480" fontSize={10}
                       minTickGap={60} tickFormatter={(v) => String(v).slice(0, 7)} />
                <YAxis stroke="#6a7480" fontSize={10} width={64}
                       domain={['auto', 'auto']} tickFormatter={(v) => Number(v).toFixed(0)} />
                <Tooltip
                  contentStyle={{ background: '#0e131b', border: '1px solid #252c38',
                                  borderRadius: 4, fontSize: 11 }}
                  formatter={(v: any) => [inr(Number(v)), 'Close']}
                />
                <Area type="monotone" dataKey="close" stroke="#39c5cf" strokeWidth={1.4}
                      fill="url(#px)" isAnimationActive={false} />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </CardBody>
      </Card>

      {/* ── Validation gates ──────────────────────────────────────────── */}
      <Card>
        <CardHeader
          title="Validation gates"
          subtitle="What each strategy must clear before it may trade (§5)"
          icon={<ShieldCheck size={14} />}
        />
        <CardBody>
          {gateRuns.length === 0 ? (
            <EmptyState title="No gate results yet"
                        hint="Run a backtest with evaluate_gates=true to populate this." />
          ) : (
            <div className="space-y-3">
              {gateRuns.slice(0, 6).map((run) => (
                <div key={run.key} className="rounded border border-surface-border
                                              bg-surface-inset p-3">
                  <div className="mb-2 flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className="text-[12px] font-semibold">{run.strategy}</span>
                      <span className={clsx(
                        'rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide',
                        run.allPassed
                          ? 'bg-brand-green/15 text-brand-green'
                          : 'bg-brand-red/15 text-brand-red',
                      )}>
                        {run.allPassed ? 'all passed' : 'blocked'}
                      </span>
                    </div>
                    <span className="text-[10px] text-ink-faint">
                      {new Date(run.runAt).toLocaleString()}
                    </span>
                  </div>
                  <div className="grid grid-cols-2 gap-2 md:grid-cols-3 lg:grid-cols-5">
                    {run.rows.map((g) => <GateChip key={g.id} gate={g} />)}
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardBody>
      </Card>

      {/* ── Promotion ladder ──────────────────────────────────────────── */}
      <Card>
        <CardHeader title="Promotion ladder"
                    subtitle="research → shadow → paper → live canary → live approved" />
        <CardBody>
          {promotions.length === 0 ? (
            <EmptyState title="No strategies registered"
                        hint="Strategies appear here once they request promotion." />
          ) : (
            <div className="space-y-3">
              {promotions.map((p: any) => {
                const idx = LADDER.indexOf(String(p.status))
                return (
                  <div key={String(p.strategy_id)} className="rounded border border-surface-border
                                                              bg-surface-inset p-3">
                    <div className="mb-2 flex items-center justify-between">
                      <span className="text-[12px] font-semibold">{String(p.strategy_id)}</span>
                      <span className="text-[11px] text-ink-muted">
                        {String(p.status)} · v{String(p.version ?? 1)}
                      </span>
                    </div>
                    <div className="flex gap-1">
                      {LADDER.map((rung, i) => (
                        <div key={rung} className="flex-1">
                          <div className={clsx(
                            'h-1.5 rounded',
                            i <= idx && idx >= 0 ? 'bg-brand-green' : 'bg-surface-elevated',
                          )} />
                          <div className={clsx(
                            'mt-1 text-center text-[9px]',
                            i === idx ? 'text-brand-green' : 'text-ink-faint',
                          )}>{rung.replace('_', ' ')}</div>
                        </div>
                      ))}
                    </div>
                    {Boolean((p.metadata as any)?.gate_waiver) && (
                      <div className="mt-2 text-[10px] text-brand-yellow">
                        ⚠ promoted via recorded waiver, not passing gates
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </CardBody>
      </Card>
    </div>
  )
}

function GateChip({ gate }: { gate: GateRow }) {
  const tone =
    gate.result === 'pass' ? 'border-brand-green/40 text-brand-green'
    : gate.result === 'warn' ? 'border-brand-yellow/40 text-brand-yellow'
    : gate.result === 'skip' ? 'border-surface-border text-ink-faint'
    : 'border-brand-red/40 text-brand-red'
  return (
    <div className={clsx('rounded border bg-surface-card px-2 py-1.5', tone)}
         title={gate.message ?? ''}>
      <div className="text-[10px] uppercase tracking-wide opacity-80">
        {gate.gate_name.replace(/_/g, ' ')}
      </div>
      <div className="mt-0.5 flex items-baseline justify-between gap-2">
        <span className="text-[12px] font-semibold">{gate.result}</span>
        {gate.metric_value != null && (
          <span className="text-[10px] opacity-90">
            {Number(gate.metric_value).toFixed(3)}
            {gate.threshold_value != null && (
              <span className="opacity-60"> /{Number(gate.threshold_value).toFixed(2)}</span>
            )}
          </span>
        )}
      </div>
    </div>
  )
}
