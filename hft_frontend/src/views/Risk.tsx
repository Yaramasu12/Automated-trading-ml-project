import { useEffect, useRef, useState, useCallback } from 'react'
import { Shield, AlertTriangle, RefreshCw, Loader2, CheckCircle, XCircle } from 'lucide-react'
import { clsx } from 'clsx'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Table } from '../components/shared/Table'
import { Tag } from '../components/shared/Badge'
import { useStore } from '../store'
import { getHealth, getCompliance, getEventRisk, getRiskEvents } from '../api'
import { fmtDateTime, pct } from '../utils'

export function Risk() {
  const runtimeState = useStore((s) => s.runtimeState)
  const monitoring   = useStore((s) => s.monitoring)

  const [health, setHealth]       = useState<Record<string, unknown> | null>(null)
  const [compliance, setCompliance] = useState<Record<string, unknown> | null>(null)
  const [eventRisk, setEventRisk]   = useState<Record<string, unknown> | null>(null)
  const [riskEvents, setRiskEvents] = useState<Record<string, unknown>[]>([])
  const [loading, setLoading]       = useState(false)

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [h, c, e, re] = await Promise.allSettled([
        getHealth(),
        getCompliance(),
        getEventRisk(),
        getRiskEvents(50),
      ])
      if (h.status === 'fulfilled')  setHealth(h.value as unknown as Record<string, unknown>)
      if (c.status === 'fulfilled')  setCompliance(c.value)
      if (e.status === 'fulfilled')  setEventRisk(e.value)
      if (re.status === 'fulfilled') {
        const v = re.value as { events?: Record<string, unknown>[] }
        setRiskEvents(v.events ?? [])
      }
    } catch { /* ignore */ }
    finally { setLoading(false) }
  }, [])

  useEffect(() => {
    load()
    pollRef.current = setInterval(load, 30_000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [load])

  const riskLimits = (health as { risk_limits?: Record<string, number> } | null)?.risk_limits

  const limitBars = riskLimits ? [
    { label: 'Drawdown Limit', value: (monitoring?.rejection_rate ?? 0) * 100, max: riskLimits.max_drawdown * 100, unit: '%' },
    { label: 'Daily Loss Limit', value: 0, max: riskLimits.max_daily_loss * 100, unit: '%' },
    { label: 'Position Limit', value: 0, max: riskLimits.max_position_pct * 100, unit: '%' },
    { label: 'Margin Utilization', value: 0, max: riskLimits.max_margin_utilization * 100, unit: '%' },
  ] : []

  return (
    <div className="space-y-5">

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Shield size={16} className="text-brand-red" />
          <div>
            <h1 className="text-lg font-bold text-gray-100">Risk Management</h1>
            <p className="text-xs text-gray-500 mt-0.5">Active risk limits, compliance, event guards</p>
          </div>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-surface-elevated border border-surface-border text-xs text-gray-400 hover:text-gray-200 transition-colors"
        >
          {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
          Refresh
        </button>
      </div>

      {/* System status */}
      {monitoring && (
        <div className={clsx(
          'flex items-center gap-3 px-4 py-3 rounded-lg border',
          monitoring.status === 'HEALTHY'  ? 'bg-brand-green/5 border-brand-green/20'
          : monitoring.status === 'DEGRADED' ? 'bg-brand-yellow/5 border-brand-yellow/20'
          : 'bg-brand-red/5 border-brand-red/20',
        )}>
          {monitoring.status === 'HEALTHY'
            ? <CheckCircle size={16} className="text-brand-green" />
            : monitoring.status === 'DEGRADED'
              ? <AlertTriangle size={16} className="text-brand-yellow" />
              : <XCircle size={16} className="text-brand-red" />}
          <span className={clsx(
            'text-sm font-semibold',
            monitoring.status === 'HEALTHY' ? 'text-brand-green'
            : monitoring.status === 'DEGRADED' ? 'text-brand-yellow' : 'text-brand-red',
          )}>
            System {monitoring.status}
          </span>
          {runtimeState?.kill_switch_active && (
            <Tag label="KILL SWITCH ACTIVE" color="red" />
          )}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        {/* Risk Limits */}
        <Card>
          <CardHeader title="Risk Limits" icon={<Shield size={14} />} />
          <CardBody className="space-y-4">
            {limitBars.length > 0 ? limitBars.map((bar) => {
              const pctUsed = bar.max > 0 ? Math.min(100, (bar.value / bar.max) * 100) : 0
              return (
                <div key={bar.label} className="space-y-1.5">
                  <div className="flex items-center justify-between text-xs">
                    <span className="text-gray-400">{bar.label}</span>
                    <span className="font-mono text-gray-300">
                      {bar.value.toFixed(1)}{bar.unit} / {bar.max.toFixed(1)}{bar.unit}
                    </span>
                  </div>
                  <div className="h-1.5 bg-surface-elevated rounded-full overflow-hidden">
                    <div
                      className={clsx(
                        'h-full rounded-full transition-all',
                        pctUsed > 80 ? 'bg-brand-red'
                        : pctUsed > 50 ? 'bg-brand-yellow'
                        : 'bg-brand-green',
                      )}
                      style={{ width: `${pctUsed}%` }}
                    />
                  </div>
                </div>
              )
            }) : (
              <div className="text-xs text-gray-500 text-center py-4">Loading risk limits...</div>
            )}

            {riskLimits && (
              <div className="pt-2 border-t border-surface-border space-y-1.5">
                <div className="flex items-center justify-between text-xs">
                  <span className="text-gray-400">Max Orders/Day</span>
                  <span className="font-mono text-gray-300">{riskLimits.max_orders_per_day}</span>
                </div>
                <div className="flex items-center justify-between text-xs">
                  <span className="text-gray-400">Block Naked Options</span>
                  <Tag label={riskLimits.block_naked_option_selling ? 'BLOCKED' : 'ALLOWED'} color={riskLimits.block_naked_option_selling ? 'red' : 'green'} />
                </div>
              </div>
            )}
          </CardBody>
        </Card>

        {/* Compliance */}
        <Card>
          <CardHeader title="Compliance" icon={<CheckCircle size={14} />} />
          <CardBody className="space-y-3">
            {compliance ? (
              <div className="space-y-2">
                {Object.entries(compliance).slice(0, 8).map(([k, v]) => (
                  <div key={k} className="flex items-center justify-between text-xs py-1.5 border-b border-surface-border/50">
                    <span className="text-gray-400 capitalize">{k.replace(/_/g, ' ')}</span>
                    <span className="font-mono text-gray-200">{String(v)}</span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-gray-500 text-center py-4">Loading compliance data...</div>
            )}
          </CardBody>
        </Card>

        {/* Capital Protection */}
        <Card>
          <CardHeader title="Capital Protection" icon={<Shield size={14} />} />
          <CardBody className="space-y-3">
            {monitoring ? (
              <div className="space-y-2">
                <div className="flex items-center justify-between text-xs py-1.5 border-b border-surface-border/50">
                  <span className="text-gray-400">Total Orders</span>
                  <span className="font-mono text-gray-200">{monitoring.total_orders}</span>
                </div>
                <div className="flex items-center justify-between text-xs py-1.5 border-b border-surface-border/50">
                  <span className="text-gray-400">Filled Orders</span>
                  <span className="font-mono text-brand-green">{monitoring.filled_orders}</span>
                </div>
                <div className="flex items-center justify-between text-xs py-1.5 border-b border-surface-border/50">
                  <span className="text-gray-400">Rejected Orders</span>
                  <span className="font-mono text-brand-red">{monitoring.rejected_orders}</span>
                </div>
                <div className="flex items-center justify-between text-xs py-1.5 border-b border-surface-border/50">
                  <span className="text-gray-400">Rejection Rate</span>
                  <span className={clsx(
                    'font-mono font-bold',
                    monitoring.rejection_rate > 0.1 ? 'text-brand-red'
                    : monitoring.rejection_rate > 0.05 ? 'text-brand-yellow'
                    : 'text-brand-green',
                  )}>
                    {pct(monitoring.rejection_rate * 100)}
                  </span>
                </div>
                <div className="flex items-center justify-between text-xs py-1.5">
                  <span className="text-gray-400">Kill Switch</span>
                  <Tag
                    label={runtimeState?.kill_switch_active ? 'ACTIVE' : 'OFF'}
                    color={runtimeState?.kill_switch_active ? 'red' : 'gray'}
                  />
                </div>
              </div>
            ) : (
              <div className="text-xs text-gray-500 text-center py-4">No monitoring data</div>
            )}
          </CardBody>
        </Card>

        {/* Event Risk */}
        <Card>
          <CardHeader title="Event Risk" icon={<AlertTriangle size={14} />} />
          <CardBody className="space-y-3">
            {eventRisk ? (
              <div className="space-y-2">
                {Object.entries(eventRisk).slice(0, 8).map(([k, v]) => (
                  <div key={k} className="flex items-center justify-between text-xs py-1.5 border-b border-surface-border/50">
                    <span className="text-gray-400 capitalize">{k.replace(/_/g, ' ')}</span>
                    <span className="font-mono text-gray-200">{String(v)}</span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-gray-500 text-center py-4">Loading event risk data...</div>
            )}
          </CardBody>
        </Card>
      </div>

      {/* Risk Rejections Table */}
      <Card>
        <CardHeader
          title="Risk Rejections"
          subtitle={`Last ${riskEvents.length} events`}
          icon={<XCircle size={14} />}
        />
        <Table
          columns={[
            {
              key: 'timestamp', label: 'Time',
              render: (v) => <span className="text-xs text-gray-500">{fmtDateTime(String(v))}</span>,
            },
            {
              key: 'symbol', label: 'Symbol',
              render: (v) => <span className="text-brand-blue font-semibold">{String(v)}</span>,
            },
            { key: 'reason', label: 'Reason' },
            {
              key: 'risk_score', label: 'Risk Score', align: 'right' as const,
              render: (v) => v != null ? (
                <span className={clsx(
                  'font-mono font-bold',
                  Number(v) > 0.8 ? 'text-brand-red' : Number(v) > 0.5 ? 'text-brand-yellow' : 'text-gray-400',
                )}>
                  {Number(v).toFixed(2)}
                </span>
              ) : '—',
            },
          ]}
          rows={riskEvents.slice(0, 50)}
          keyFn={(r) => String(r.id ?? r.timestamp ?? Math.random())}
          emptyMessage="No risk rejections"
          compact
        />
      </Card>

    </div>
  )
}
