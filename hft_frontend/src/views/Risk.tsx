import { useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle, Loader2, RefreshCw, Shield, XCircle } from 'lucide-react'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Badge } from '../components/shared/Badge'
import { useStore } from '../store'
import { getRiskEvents, getSupervisorDecision } from '../api'
import { fmtDateTime, num } from '../utils'
import { clsx } from 'clsx'

interface RiskEvent {
  id: number
  event_type: string
  reason: string
  symbol: string | null
  risk_score: number
  approved: boolean
  timestamp: string
}

export function Risk() {
  const runtimeState = useStore((s) => s.runtimeState)
  const monitoring = useStore((s) => s.monitoring)

  const [riskEvents, setRiskEvents] = useState<RiskEvent[]>([])
  const [supervisor, setSupervisor] = useState<{ action: string; reason: string; max_allocation_multiplier: number } | null>(null)
  const [loadingEvents, setLoadingEvents] = useState(false)
  const [loadingSupervisor, setLoadingSupervisor] = useState(false)

  async function loadEvents() {
    setLoadingEvents(true)
    try {
      const r = await getRiskEvents(50)
      setRiskEvents(r.events as RiskEvent[])
    } finally {
      setLoadingEvents(false)
    }
  }

  async function loadSupervisor() {
    setLoadingSupervisor(true)
    try {
      const r = await getSupervisorDecision({ equity: 2_000_000, realized_pnl: 0, drawdown: 0 })
      setSupervisor(r)
    } finally {
      setLoadingSupervisor(false)
    }
  }

  useEffect(() => {
    loadEvents()
    loadSupervisor()
  }, [])

  const limits = runtimeState ? null : null // populated from health endpoint via WS

  const rejectRate = monitoring?.rejection_rate ?? 0
  const totalOrders = monitoring?.total_orders ?? 0
  const rejectedOrders = Math.round(totalOrders * rejectRate)

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-gray-100">Risk Management</h1>
          <p className="text-xs text-gray-500 mt-0.5">Supervisor decisions, limits, and event log</p>
        </div>
        <button
          onClick={() => { loadEvents(); loadSupervisor() }}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-surface-elevated border border-surface-border text-xs text-gray-400 hover:text-gray-200"
        >
          <RefreshCw size={12} />
          Refresh
        </button>
      </div>

      {/* Monitoring stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="bg-surface-card border border-surface-border rounded-lg p-4">
          <div className="text-xs text-gray-500 mb-1">System Status</div>
          <div className={clsx('text-lg font-bold font-mono', monitoring?.status === 'HEALTHY' ? 'text-brand-green' : 'text-brand-red')}>
            {monitoring?.status ?? '—'}
          </div>
        </div>
        <div className="bg-surface-card border border-surface-border rounded-lg p-4">
          <div className="text-xs text-gray-500 mb-1">Total Orders</div>
          <div className="text-lg font-bold font-mono text-gray-100">{totalOrders}</div>
        </div>
        <div className="bg-surface-card border border-surface-border rounded-lg p-4">
          <div className="text-xs text-gray-500 mb-1">Rejected Orders</div>
          <div className="text-lg font-bold font-mono text-brand-red">{rejectedOrders}</div>
        </div>
        <div className="bg-surface-card border border-surface-border rounded-lg p-4">
          <div className="text-xs text-gray-500 mb-1">Rejection Rate</div>
          <div className={clsx('text-lg font-bold font-mono', rejectRate > 0.3 ? 'text-brand-red' : 'text-brand-green')}>
            {(rejectRate * 100).toFixed(1)}%
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
        {/* Supervisor decision */}
        <Card>
          <CardHeader title="Risk Supervisor" subtitle="Current allocation decision" icon={<Shield size={14} />} />
          <CardBody>
            {loadingSupervisor ? (
              <div className="flex items-center gap-2 text-gray-500">
                <Loader2 size={14} className="animate-spin" />
                <span className="text-sm">Loading…</span>
              </div>
            ) : supervisor ? (
              <div className="space-y-3">
                <div className="flex items-center gap-3">
                  {supervisor.action === 'PROCEED' ? (
                    <CheckCircle size={24} className="text-brand-green flex-shrink-0" />
                  ) : supervisor.action === 'REDUCE' ? (
                    <AlertTriangle size={24} className="text-brand-yellow flex-shrink-0" />
                  ) : (
                    <XCircle size={24} className="text-brand-red flex-shrink-0" />
                  )}
                  <div>
                    <div className="font-bold text-gray-100">{supervisor.action}</div>
                    <div className="text-xs text-gray-400 mt-0.5">{supervisor.reason}</div>
                  </div>
                </div>
                <div className="flex items-center justify-between bg-surface-elevated rounded-md px-3 py-2">
                  <span className="text-xs text-gray-500">Max Allocation Multiplier</span>
                  <span className="font-mono font-bold text-brand-cyan">{supervisor.max_allocation_multiplier.toFixed(2)}×</span>
                </div>
              </div>
            ) : (
              <p className="text-sm text-gray-500">No supervisor data</p>
            )}
          </CardBody>
        </Card>

        {/* Kill switch / mode status */}
        <Card>
          <CardHeader title="System Controls" subtitle="Runtime state" icon={<Shield size={14} />} />
          <CardBody className="space-y-3">
            <div className="flex items-center justify-between py-2 border-b border-surface-border">
              <span className="text-sm text-gray-400">Kill Switch</span>
              <Badge variant={runtimeState?.kill_switch_active ? 'red' : 'green'} dot>
                {runtimeState?.kill_switch_active ? 'ACTIVE' : 'INACTIVE'}
              </Badge>
            </div>
            <div className="flex items-center justify-between py-2 border-b border-surface-border">
              <span className="text-sm text-gray-400">Execution Mode</span>
              <Badge variant={runtimeState?.execution_mode === 'LIVE' ? 'red' : runtimeState?.execution_mode === 'PAPER' ? 'yellow' : 'blue'}>
                {runtimeState?.execution_mode ?? '—'}
              </Badge>
            </div>
            <div className="flex items-center justify-between py-2 border-b border-surface-border">
              <span className="text-sm text-gray-400">Live Armed</span>
              <Badge variant={runtimeState?.live_armed ? 'orange' : 'gray'}>
                {runtimeState?.live_armed ? 'ARMED' : 'DISARMED'}
              </Badge>
            </div>
            <div className="flex items-center justify-between py-2">
              <span className="text-sm text-gray-400">Broker</span>
              <span className="text-sm font-mono text-gray-300">{runtimeState?.broker ?? '—'}</span>
            </div>
          </CardBody>
        </Card>
      </div>

      {/* Risk event log */}
      <Card>
        <CardHeader
          title="Risk Event Log"
          subtitle={`${riskEvents.length} events`}
          action={
            <button onClick={loadEvents} className="text-xs text-gray-500 hover:text-gray-300">
              <RefreshCw size={12} />
            </button>
          }
        />
        {loadingEvents ? (
          <div className="flex justify-center py-8">
            <Loader2 size={20} className="animate-spin text-gray-500" />
          </div>
        ) : riskEvents.length === 0 ? (
          <div className="text-center py-8 text-gray-600 text-sm">No risk events recorded</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-surface-border text-gray-500 uppercase tracking-wider">
                  <th className="px-4 py-2 text-left">Time</th>
                  <th className="px-4 py-2 text-left">Type</th>
                  <th className="px-4 py-2 text-left">Symbol</th>
                  <th className="px-4 py-2 text-right">Risk Score</th>
                  <th className="px-4 py-2 text-left">Decision</th>
                  <th className="px-4 py-2 text-left">Reason</th>
                </tr>
              </thead>
              <tbody>
                {riskEvents.map((ev) => (
                  <tr key={ev.id} className="border-b border-surface-border/40 hover:bg-surface-elevated/40">
                    <td className="px-4 py-2 font-mono text-gray-500">{fmtDateTime(ev.timestamp)}</td>
                    <td className="px-4 py-2 text-gray-300">{ev.event_type}</td>
                    <td className="px-4 py-2 font-mono text-brand-blue">{ev.symbol ?? '—'}</td>
                    <td className="px-4 py-2 text-right font-mono">{num(ev.risk_score, 3)}</td>
                    <td className="px-4 py-2">
                      <Badge variant={ev.approved ? 'green' : 'red'}>
                        {ev.approved ? 'APPROVED' : 'BLOCKED'}
                      </Badge>
                    </td>
                    <td className="px-4 py-2 text-gray-400 max-w-xs truncate">{ev.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
