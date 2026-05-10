import { useCallback, useEffect, useMemo, useState } from 'react'
import { clsx } from 'clsx'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { AlertTriangle, Loader2, RefreshCw, Target } from 'lucide-react'
import { getDailyPnl, getGoalGovernorStatus, getTargetProgress } from '../api'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { KeyValueGrid, PageHeader, ProgressBar, StatTile } from '../components/shared/Command'
import { Tag } from '../components/shared/Badge'
import { useStore } from '../store'
import { fmtDate, inr, pct } from '../utils'
import type { DailyPnl, TargetProgress } from '../types'

const TOOLTIP_STYLE = {
  backgroundColor: '#161b22',
  border: '1px solid #30363d',
  borderRadius: 6,
  fontSize: 12,
  color: '#e6edf3',
}

function recommendationColor(value?: string): 'green' | 'red' | 'yellow' {
  if (value === 'reduce_risk') return 'red'
  if (value === 'increase_research') return 'yellow'
  return 'green'
}

export function GoalGovernor() {
  const status = useStore((s) => s.goalGovernorStatus)
  const setStatus = useStore((s) => s.setGoalGovernorStatus)
  const setTargetProgress = useStore((s) => s.setTargetProgress)
  const storedTarget = useStore((s) => s.targetProgress)
  const setDailyPnlStore = useStore((s) => s.setDailyPnl)
  const dailyPnlStore = useStore((s) => s.dailyPnl)
  const setError = useStore((s) => s.setError)

  const [target, setTarget] = useState<TargetProgress | null>(storedTarget)
  const [dailyPnl, setDailyPnl] = useState<DailyPnl[]>(dailyPnlStore)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [goalRes, targetRes, pnlRes] = await Promise.allSettled([
        getGoalGovernorStatus(),
        getTargetProgress({}),
        getDailyPnl(90),
      ])
      if (goalRes.status === 'fulfilled') setStatus(goalRes.value)
      if (targetRes.status === 'fulfilled') {
        setTarget(targetRes.value)
        setTargetProgress(targetRes.value)
      }
      if (pnlRes.status === 'fulfilled') {
        setDailyPnl(pnlRes.value.history)
        setDailyPnlStore(pnlRes.value.history)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load Goal Governor')
    } finally {
      setLoading(false)
    }
  }, [setDailyPnlStore, setError, setStatus, setTargetProgress])

  useEffect(() => { load() }, [load])

  const yearlyTarget = status?.yearly_target ?? target?.annual_target ?? 50_000_000
  const realizedPnl = status?.realized_pnl ?? target?.realized_pnl ?? dailyPnl.reduce((sum, d) => sum + d.realized_pnl, 0)
  const currentEquity = status?.current_equity ?? target?.current_equity ?? 0
  const targetPct = yearlyTarget > 0 ? Math.max(0, Math.min(100, (realizedPnl / yearlyTarget) * 100)) : 0
  const probability = status?.target_probability ?? 0
  const requiredRunRate = status?.required_daily_run_rate ?? target?.required_run_rate ?? 0
  const actualRunRate = status?.actual_daily_run_rate ?? (dailyPnl.length ? realizedPnl / dailyPnl.length : 0)

  const chartData = useMemo(() => {
    let cumulative = 0
    return dailyPnl.map((day) => {
      cumulative += day.realized_pnl
      return {
        date: fmtDate(day.trade_date),
        realized: day.realized_pnl,
        cumulative,
        equity: day.ending_equity,
      }
    })
  }, [dailyPnl])

  return (
    <div className="space-y-5">
      <PageHeader
        title="Goal Governor"
        subtitle="Target progress, run-rate pressure, Monte Carlo probability, and hard risk boundary"
        icon={<Target size={17} className="text-brand-green" />}
        action={
          <button
            onClick={load}
            disabled={loading}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-surface-elevated border border-surface-border text-xs text-gray-400 hover:text-gray-200 disabled:opacity-50"
          >
            {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
            Refresh
          </button>
        }
      />

      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
        <StatTile label="Annual Target" value={inr(yearlyTarget)} accent="green" sub="target constraint" />
        <StatTile label="Realized P&L" value={inr(realizedPnl)} accent={realizedPnl >= 0 ? 'green' : 'red'} sub={currentEquity ? `${inr(currentEquity)} equity` : 'from ledger'} />
        <StatTile label="Target Probability" value={pct(probability * 100, 1)} accent={probability > 0.45 ? 'green' : probability > 0.2 ? 'yellow' : 'red'} sub="Monte Carlo estimate" />
        <StatTile label="Recommendation" value={status?.recommendation ?? 'normal'} accent={recommendationColor(status?.recommendation)} sub="cannot raise risk limits" />
      </div>

      <Card className="border-brand-green/20">
        <CardHeader title="Target Reality Layer" subtitle="Goal pressure can change research priority, not hard risk limits" icon={<Target size={14} />} />
        <CardBody className="space-y-4">
          <ProgressBar
            label="Annual target progress"
            value={targetPct}
            accent={targetPct > 50 ? 'green' : targetPct > 20 ? 'yellow' : 'blue'}
            right={`${targetPct.toFixed(2)}%`}
          />
          <div className="grid grid-cols-1 lg:grid-cols-[1fr_.8fr] gap-5">
            <div className="h-72 min-w-0">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData}>
                  <defs>
                    <linearGradient id="goalPnl" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#3fb950" stopOpacity={0.45} />
                      <stop offset="95%" stopColor="#3fb950" stopOpacity={0.02} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid stroke="#30363d" strokeDasharray="3 3" vertical={false} />
                  <XAxis dataKey="date" tick={{ fill: '#8b949e', fontSize: 11 }} axisLine={false} tickLine={false} />
                  <YAxis tick={{ fill: '#8b949e', fontSize: 11 }} axisLine={false} tickLine={false} tickFormatter={(v) => `${Number(v) / 1000}k`} />
                  <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v: number) => [inr(v), 'Cumulative P&L']} />
                  <Area type="monotone" dataKey="cumulative" stroke="#3fb950" fill="url(#goalPnl)" strokeWidth={2} />
                </AreaChart>
              </ResponsiveContainer>
            </div>
            <div className="space-y-4">
              <KeyValueGrid
                columns="grid-cols-2"
                items={[
                  { label: 'Required/day', value: inr(requiredRunRate), accent: 'orange' },
                  { label: 'Actual/day', value: inr(actualRunRate), accent: actualRunRate >= requiredRunRate ? 'green' : 'yellow' },
                  { label: 'Days elapsed', value: status?.days_elapsed ?? target?.elapsed_days ?? 0, accent: 'blue' },
                  { label: 'Drawdown budget', value: status?.drawdown_budget_remaining != null ? pct(status.drawdown_budget_remaining * 100, 1) : '--', accent: 'red' },
                ]}
              />
              <div className={clsx(
                'rounded-lg border p-3 flex items-start gap-2',
                status?.recommendation === 'reduce_risk' ? 'bg-brand-red/5 border-brand-red/20'
                : status?.recommendation === 'increase_research' ? 'bg-brand-yellow/5 border-brand-yellow/20'
                : 'bg-brand-green/5 border-brand-green/20',
              )}>
                <AlertTriangle size={14} className={clsx(
                  'mt-0.5',
                  status?.recommendation === 'reduce_risk' ? 'text-brand-red'
                  : status?.recommendation === 'increase_research' ? 'text-brand-yellow'
                  : 'text-brand-green',
                )} />
                <div className="text-xs text-gray-400">
                  <span className="font-semibold text-gray-200">Risk boundary:</span> target pressure never auto-raises drawdown,
                  allocation, broker, compliance, manual approval, or kill-switch limits.
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                <Tag label={status?.enabled ? 'governor enabled' : 'governor disabled'} color={status?.enabled ? 'green' : 'gray'} />
                <Tag label={status?.on_track ? 'on track' : 'off track'} color={status?.on_track ? 'green' : 'yellow'} />
                <Tag label="risk limits locked" color="red" />
              </div>
            </div>
          </div>
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Daily P&L Tape" subtitle={`${dailyPnl.length} sessions`} />
        <CardBody className="grid grid-cols-1 md:grid-cols-3 gap-3">
          {dailyPnl.slice(-6).map((day) => (
            <div key={day.trade_date} className="rounded-lg bg-surface-elevated border border-surface-border p-3">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-gray-400">{fmtDate(day.trade_date)}</span>
                <Tag label={`${day.total_trades} trades`} color="blue" />
              </div>
              <div className={clsx('mt-2 text-lg font-bold font-mono', day.realized_pnl >= 0 ? 'text-brand-green' : 'text-brand-red')}>
                {inr(day.realized_pnl)}
              </div>
              <div className="mt-1 text-xs text-gray-500">{day.winning_trades} winners</div>
            </div>
          ))}
          {dailyPnl.length === 0 && <div className="md:col-span-3 text-xs text-gray-500 text-center py-8">No daily P&L history yet</div>}
        </CardBody>
      </Card>
    </div>
  )
}
