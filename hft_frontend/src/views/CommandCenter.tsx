/**
 * Command Center — REDESIGN_PROMPT.md §10 screen 1.
 *
 * Consolidates Dashboard + Account + Engine into one information-dense screen:
 * live P&L, equity curve, open positions, strategy/runtime status, feed
 * freshness, reconciliation, and an always-reachable KILL SWITCH.
 *
 * DESIGN RULE THAT DRIVES THE EMPTY STATES
 * -----------------------------------------
 * This platform genuinely has near-zero trade history (paper mode, 0 fills so
 * far). A dashboard that renders "—" in twenty tiles is indistinguishable from
 * a dashboard that is BROKEN — and this repo has already burned a session on a
 * UI that looked broken but wasn't (see the ws.ts bridge bug). So every empty
 * region here states WHY it is empty and what would fill it. "No trades yet —
 * paper mode, market closed" is information; "—" is not.
 *
 * Data comes from REST (polled) plus the /ws/dashboard snapshot already fed
 * into the Zustand store, so this screen keeps working if the socket drops.
 */
import { useCallback, useEffect, useState } from 'react'
import {
  Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { clsx } from 'clsx'
import {
  Activity, AlertTriangle, Ban, Brain, Gauge, RefreshCw, ShieldCheck, Wallet,
} from 'lucide-react'

import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { MetricCard } from '../components/shared/MetricCard'
import { PageHeader } from '../components/shared/PageHeader'
import { EmptyState } from '../components/shared/States'
import { Table } from '../components/shared/Table'
import { execModeBadge } from '../components/shared/Badge'
import { useStore } from '../store'
import { inr, pct, fmtDateTime } from '../utils'
import {
  getEquityCurve, getPortfolioPositions, getDBSummary, getRecentTrades,
  getHealth, getFeedSnapshot,
} from '../api'

const POLL_MS = 15_000

type Loaded = {
  equity: { recorded_at: string; equity: number; drawdown: number }[]
  positions: any[]
  portfolio: Record<string, number> | null
  dbSummary: Record<string, unknown> | null
  trades: any[]
  health: any | null
  feed: any | null
}

const EMPTY: Loaded = {
  equity: [], positions: [], portfolio: null, dbSummary: null,
  trades: [], health: null, feed: null,
}

export function CommandCenter() {
  const [data, setData] = useState<Loaded>(EMPTY)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastLoad, setLastLoad] = useState<number | null>(null)
  const wsStatus = useStore((s) => s.wsStatus)

  const load = useCallback(async () => {
    setError(null)
    // Settled, not all — one dead endpoint must not blank the whole screen.
    const results = await Promise.allSettled([
      getEquityCurve(undefined, 400), getPortfolioPositions(), getDBSummary(),
      getRecentTrades(25), getHealth(), getFeedSnapshot(),
    ])
    const val = <T,>(i: number, fallback: T): T =>
      results[i].status === 'fulfilled' ? ((results[i] as PromiseFulfilledResult<any>).value ?? fallback) : fallback

    const failures = results.filter((r) => r.status === 'rejected').length
    if (failures === results.length) setError('Backend unreachable — every request failed.')
    else if (failures > 0) setError(`${failures} of ${results.length} panels failed to load.`)

    const posResp = val<any>(1, {})
    setData({
      equity: val<any>(0, {})?.curve ?? [],
      positions: posResp?.positions ?? [],
      portfolio: posResp?.portfolio ?? null,
      dbSummary: val<any>(2, null),
      trades: val<any>(3, {})?.trades ?? [],
      health: val<any>(4, null),
      feed: val<any>(5, null),
    })
    setLoading(false)
    setLastLoad(Date.now())
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, POLL_MS)
    return () => clearInterval(t)
  }, [load])

  const pf = data.portfolio
  const equity = pf?.equity ?? null
  const startCapital = 1_000_000
  const totalPnl = equity != null ? equity - startCapital : null
  const state = data.health?.state ?? {}
  const killActive = Boolean(state.kill_switch_active)

  const curve = data.equity.map((p) => ({
    t: new Date(p.recorded_at).getTime(),
    equity: p.equity,
    dd: -(p.drawdown ?? 0) * 100,
  }))

  return (
    <div className="space-y-4">
      <PageHeader
        title="Command Center"
        subtitle="Live P&L · positions · runtime health"
        icon={<Gauge size={18} />}
        actions={
          <div className="flex items-center gap-3">
            <ConnBadge status={wsStatus} />
            {lastLoad && (
              <span className="text-[11px] text-ink-faint">
                updated {fmtDateTime(new Date(lastLoad).toISOString())}
              </span>
            )}
            <button
              onClick={load}
              className="flex items-center gap-1.5 rounded border border-surface-border
                         px-2.5 py-1 text-[11px] text-ink-muted
                         hover:bg-surface-elevated hover:text-ink"
            >
              <RefreshCw size={12} className={loading ? 'animate-spin' : ''} /> Refresh
            </button>
          </div>
        }
      />

      {error && (
        <div className="flex items-center gap-2 rounded border border-brand-red/40
                        bg-brand-red/10 px-3 py-2 text-[12px] text-brand-red">
          <AlertTriangle size={14} /> {error}
        </div>
      )}

      {/* KILL SWITCH — §10 requires it always reachable, including mobile width */}
      <div
        className={clsx(
          'flex flex-wrap items-center justify-between gap-3 rounded border px-4 py-3',
          killActive
            ? 'border-brand-red bg-brand-red/10'
            : 'border-surface-border bg-surface-card',
        )}
      >
        <div className="flex items-center gap-3">
          {killActive ? <Ban size={18} className="text-brand-red" />
                      : <ShieldCheck size={18} className="text-brand-green" />}
          <div>
            <div className="text-[13px] font-semibold">
              {killActive ? 'KILL SWITCH ACTIVE — new entries blocked' : 'Trading armed'}
            </div>
            <div className="text-[11px] text-ink-muted">
              {execModeBadge(String(state.execution_mode ?? 'UNKNOWN'))}
              <span className="ml-2">
                broker {String(state.broker ?? '—')} ·{' '}
                {state.angel_one_configured ? 'credentials OK' : 'no credentials'}
              </span>
            </div>
          </div>
        </div>
        <div className="text-[11px] text-ink-faint">
          {data.health?.operational_status ?? '—'}
        </div>
      </div>

      {/* Headline metrics */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <MetricCard label="Equity" value={equity != null ? inr(equity) : '—'}
                    icon={<Wallet size={14} />} accent="blue" />
        <MetricCard label="Total P&L"
                    value={totalPnl != null ? inr(totalPnl) : '—'}
                    color={totalPnl == null ? undefined : totalPnl >= 0 ? 'green' : 'red'} />
        <MetricCard label="Unrealized"
                    value={pf?.unrealized_pnl != null ? inr(pf.unrealized_pnl) : '—'}
                    color={(pf?.unrealized_pnl ?? 0) >= 0 ? 'green' : 'red'} />
        <MetricCard label="Drawdown"
                    value={pf?.drawdown != null ? pct(pf.drawdown) : '—'}
                    color={(pf?.drawdown ?? 0) > 0 ? 'red' : undefined} />
      </div>

      {/* Equity curve */}
      <Card>
        <CardHeader title="Equity curve" subtitle={`${curve.length} snapshots`}
                    icon={<Activity size={14} />} />
        <CardBody>
          {curve.length < 2 ? (
            <EmptyState
              title="Not enough equity history yet"
              hint="Portfolio snapshots accumulate while the runtime is up. This fills in within minutes of startup."
            />
          ) : (
            <ResponsiveContainer width="100%" height={260}>
              <AreaChart data={curve} margin={{ top: 6, right: 8, bottom: 0, left: 8 }}>
                <defs>
                  <linearGradient id="eq" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#58a6ff" stopOpacity={0.35} />
                    <stop offset="100%" stopColor="#58a6ff" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#252c38" strokeDasharray="2 4" vertical={false} />
                <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} scale="time"
                       tickFormatter={(v) => new Date(v).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                       stroke="#6a7480" fontSize={10} />
                <YAxis stroke="#6a7480" fontSize={10} width={70}
                       domain={['auto', 'auto']} tickFormatter={(v) => inr(v)} />
                <Tooltip
                  contentStyle={{
                    background: '#0e131b', border: '1px solid #252c38',
                    borderRadius: 4, fontSize: 11,
                  }}
                  labelFormatter={(v) => new Date(Number(v)).toLocaleString()}
                  formatter={(v: any) => [inr(Number(v)), 'Equity']}
                />
                <Area type="monotone" dataKey="equity" stroke="#58a6ff"
                      strokeWidth={1.5} fill="url(#eq)" isAnimationActive={false} />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </CardBody>
      </Card>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {/* Positions */}
        <Card>
          <CardHeader title="Open positions" subtitle={`${data.positions.length} open`} />
          <CardBody>
            {data.positions.length === 0 ? (
              <EmptyState
                title="No open positions"
                hint={
                  `Paper mode is armed and the short-vol strategy is enabled — it simply has not ` +
                  `found an entry that clears its VRP/Kelly gates yet. Entries are evaluated each ` +
                  `scan cycle during market hours.`
                }
              />
            ) : (
              <Table
                columns={[
                  { key: 'symbol', header: 'Symbol' },
                  { key: 'quantity', header: 'Qty', align: 'right' },
                  { key: 'average_price', header: 'Avg', align: 'right',
                    render: (_v, r: any) => inr(r.average_price) },
                  { key: 'unrealized_pnl', header: 'Unreal.', align: 'right',
                    render: (_v, r: any) => (
                      <span className={r.unrealized_pnl >= 0 ? 'text-brand-green' : 'text-brand-red'}>
                        {inr(r.unrealized_pnl)}
                      </span>
                    ) },
                ]}
                rows={data.positions}
                keyFn={(r: any, i) => String(r.symbol ?? i)}
              />
            )}
          </CardBody>
        </Card>

        {/* Recent trades */}
        <Card>
          <CardHeader title="Recent trades" subtitle={`${data.trades.length} shown`} />
          <CardBody>
            {data.trades.length === 0 ? (
              <EmptyState
                title="No trades recorded"
                hint={
                  `The trades table is empty because nothing has filled yet — not because the ` +
                  `database is disconnected. Backend reports ` +
                  `${String((data.dbSummary as any)?.db_backend ?? 'unknown')} at ` +
                  `${String((data.dbSummary as any)?.db_path ?? 'unknown')}.`
                }
              />
            ) : (
              <Table
                columns={[
                  { key: 'symbol', header: 'Symbol' },
                  { key: 'side', header: 'Side' },
                  { key: 'quantity', header: 'Qty', align: 'right' },
                  { key: 'price', header: 'Price', align: 'right',
                    render: (_v, r: any) => inr(r.price) },
                  { key: 'ts', header: 'Time',
                    render: (_v, r: any) => fmtDateTime(r.ts ?? r.timestamp) },
                ]}
                rows={data.trades}
                keyFn={(r: any, i) => String(r.trade_id ?? r.order_id ?? i)}
              />
            )}
          </CardBody>
        </Card>
      </div>

      {/* AI layer honesty report — /health.ai_capabilities.
          The backend measures each layer's REAL state (e.g. the council's
          actual stub-fallback ratio, not just its configured gateway), but
          nothing rendered it, so a council answering with canned stub votes
          looked identical to a healthy one. CLAUDE.md requires this report
          stay truthful; that only helps if it is visible. */}
      <Card>
        <CardHeader
          title="AI layers"
          subtitle="Advisory only — these inform sizing/conviction, they never block a trade"
          icon={<Brain size={14} />}
          action={
            data.health?.ai_capabilities?.degraded ? (
              <span className="rounded border border-brand-yellow/40 bg-brand-yellow/10
                               px-2 py-0.5 text-[10px] uppercase tracking-wide text-brand-yellow">
                degraded
              </span>
            ) : (
              <span className="rounded border border-brand-green/40 bg-brand-green/10
                               px-2 py-0.5 text-[10px] uppercase tracking-wide text-brand-green">
                all real
              </span>
            )
          }
        />
        <CardBody>
          {!data.health?.ai_capabilities ? (
            <EmptyState title="No capability report" hint="Backend /health did not include ai_capabilities." />
          ) : (
            <div className="space-y-2">
              {Object.entries(data.health.ai_capabilities.layers ?? {}).map(
                ([name, layer]: [string, any]) => {
                  const weak = (data.health.ai_capabilities.degraded_layers ?? []).includes(name)
                  return (
                    <div key={name}
                         className="flex flex-wrap items-baseline justify-between gap-2 rounded
                                    border border-surface-border bg-surface-inset px-3 py-2">
                      <div className="flex items-center gap-2">
                        <span className="text-[12px] font-semibold">{name.replace(/_/g, ' ')}</span>
                        <span className={clsx(
                          'rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide',
                          weak ? 'bg-brand-yellow/15 text-brand-yellow'
                               : 'bg-brand-green/15 text-brand-green',
                        )}>{String(layer.status)}</span>
                      </div>
                      <span className="text-[10px] text-ink-faint">{String(layer.detail ?? '')}</span>
                      {layer.budget && (
                        <span className="w-full text-[10px] text-ink-faint">
                          council budget: {layer.budget.calls_in_window}/{layer.budget.max_calls} calls
                          per {Math.round(layer.budget.window_seconds)}s
                          {layer.budget.exhausted && (
                            <span className="ml-1 text-brand-yellow">— exhausted, marginal signals skipped</span>
                          )}
                        </span>
                      )}
                    </div>
                  )
                },
              )}
              <p className="pt-1 text-[10px] text-ink-faint">
                {String(data.health.ai_capabilities.note ?? '')}
              </p>
            </div>
          )}
        </CardBody>
      </Card>

      {/* Runtime / feed health */}
      <Card>
        <CardHeader title="Runtime health" icon={<Activity size={14} />} />
        <CardBody>
          <div className="grid grid-cols-2 gap-3 text-[12px] md:grid-cols-4">
            <Stat label="Market" value={String(data.feed?.freshness?.market_status ?? '—')} />
            <Stat label="Feed" value={data.feed?.running ? 'running' : 'stopped'}
                  tone={data.feed?.running ? 'good' : 'warn'} />
            <Stat label="Subscribed" value={String(data.feed?.subscribed_symbols?.length ?? 0)} />
            <Stat label="Ticks" value={String(data.feed?.tick_count ?? 0)} />
            <Stat label="Scheduler"
                  value={data.health?.scheduler?.running ? 'running' : 'stopped'}
                  tone={data.health?.scheduler?.running ? 'good' : 'warn'} />
            <Stat label="Queue" value={String(data.health?.scheduler?.queue_depth ?? 0)} />
            <Stat label="Exit plans" value={String(data.health?.exit_manager?.active_plans ?? 0)} />
            <Stat label="Swallowed errors"
                  value={String(data.health?.swallowed_errors ?? 0)}
                  tone={(data.health?.swallowed_errors ?? 0) > 0 ? 'warn' : 'good'} />
          </div>
          {data.feed?.running && (data.feed?.tick_count ?? 0) === 0 && (
            <div className="mt-3 text-[11px] text-ink-muted">
              Feed is subscribed but has received 0 ticks — expected while the market is
              closed. Angel One sends nothing outside trading hours.
            </div>
          )}
        </CardBody>
      </Card>
    </div>
  )
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: 'good' | 'warn' }) {
  return (
    <div className="rounded border border-surface-border bg-surface-inset px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-ink-faint">{label}</div>
      <div className={clsx(
        'mt-0.5 font-semibold',
        tone === 'good' && 'text-status-good',
        tone === 'warn' && 'text-status-warn',
      )}>{value}</div>
    </div>
  )
}

function ConnBadge({ status }: { status: string }) {
  const tone =
    status === 'connected' ? 'text-brand-green border-brand-green/40'
    : status === 'error' ? 'text-brand-red border-brand-red/40'
    : 'text-brand-yellow border-brand-yellow/40'
  return (
    <span className={clsx('rounded border px-2 py-0.5 text-[10px] uppercase tracking-wide', tone)}>
      ws {status}
    </span>
  )
}
