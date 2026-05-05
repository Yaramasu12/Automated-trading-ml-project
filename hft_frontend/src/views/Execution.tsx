import { useEffect, useRef, useState } from 'react'
import { CheckCircle, GitBranch, Loader2, RefreshCw, ShieldAlert, XCircle } from 'lucide-react'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Badge } from '../components/shared/Badge'
import {
  approveManualApproval,
  getBrokerCapabilities,
  getEventSummary,
  getManualApprovals,
  getOmsEvents,
  getSchedulerStats,
  rejectManualApproval,
  squareOff,
} from '../api'
import type { BrokerCapabilityStatus, EventBusSummary, ManualApprovalStatus } from '../types'
import { fmtDateTime, inr } from '../utils'

interface OmsEvent {
  id: number
  event_id: string
  occurred_at: string
  event_type: string
  order_id: string
  symbol: string | null
  strategy_name: string | null
  rejection_reason: string | null
}

export function Execution() {
  const [scheduler, setScheduler] = useState<Record<string, unknown> | null>(null)
  const [approvals, setApprovals] = useState<ManualApprovalStatus | null>(null)
  const [omsEvents, setOmsEvents] = useState<OmsEvent[]>([])
  const [capabilities, setCapabilities] = useState<BrokerCapabilityStatus | null>(null)
  const [eventBus, setEventBus] = useState<EventBusSummary | null>(null)
  const [loading, setLoading] = useState(false)
  const [busyRequest, setBusyRequest] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  async function refresh() {
    setLoading(true)
    const [schedulerRes, approvalsRes, omsRes, capsRes, busRes] = await Promise.allSettled([
      getSchedulerStats(),
      getManualApprovals(),
      getOmsEvents(60),
      getBrokerCapabilities(),
      getEventSummary(),
    ])
    if (schedulerRes.status === 'fulfilled') setScheduler(schedulerRes.value)
    if (approvalsRes.status === 'fulfilled') setApprovals(approvalsRes.value)
    if (omsRes.status === 'fulfilled') setOmsEvents(omsRes.value.events as OmsEvent[])
    if (capsRes.status === 'fulfilled') setCapabilities(capsRes.value)
    if (busRes.status === 'fulfilled') setEventBus(busRes.value)
    setLoading(false)
  }

  async function approve(requestId: string) {
    setBusyRequest(requestId)
    try {
      await approveManualApproval(requestId, 'Operator approved from execution dashboard')
      await refresh()
    } finally {
      setBusyRequest(null)
    }
  }

  async function reject(requestId: string) {
    setBusyRequest(requestId)
    try {
      await rejectManualApproval(requestId, 'Operator rejected from execution dashboard')
      await refresh()
    } finally {
      setBusyRequest(null)
    }
  }

  async function runSquareOff() {
    setLoading(true)
    try {
      await squareOff({ scope: 'GLOBAL', reason: 'dashboard_square_off' })
      await refresh()
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
    pollRef.current = setInterval(refresh, 6_000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [])

  const queueDepth = Number(scheduler?.queue_depth ?? 0)
  const processed = Number(scheduler?.processed ?? 0)
  const rejected = Number(scheduler?.rejected ?? 0)

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-gray-100">Execution</h1>
          <p className="text-xs text-gray-500 mt-0.5">Queues, approvals, locks, and OMS state</p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={runSquareOff}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-brand-red/15 border border-brand-red/40 text-xs text-brand-red hover:bg-brand-red/25"
          >
            <ShieldAlert size={12} />
            Square Off
          </button>
          <button
            onClick={refresh}
            disabled={loading}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-surface-elevated border border-surface-border text-xs text-gray-400 hover:text-gray-200 disabled:opacity-50"
          >
            {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
            Refresh
          </button>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="Queue" value={queueDepth} accent="text-brand-blue" />
        <Stat label="Processed" value={processed} accent="text-brand-green" />
        <Stat label="Rejected" value={rejected} accent="text-brand-red" />
        <Stat label="Events" value={eventBus?.event_count ?? 0} accent="text-brand-cyan" />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
        <Card>
          <CardHeader
            title="Manual Approvals"
            subtitle={`${approvals?.pending_count ?? 0} pending · threshold ${inr(approvals?.approval_threshold_notional ?? 0)}`}
            icon={<CheckCircle size={14} />}
          />
          {approvals?.pending?.length ? (
            <div className="divide-y divide-surface-border">
              {approvals.pending.map((request) => (
                <div key={request.request_id} className="p-4 flex items-center justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-sm text-brand-blue">{request.symbol}</span>
                      <Badge variant={request.side === 'BUY' ? 'green' : 'red'}>{request.side}</Badge>
                      <span className="text-xs text-gray-500">qty {request.quantity}</span>
                    </div>
                    <div className="text-xs text-gray-500 mt-1 truncate">
                      {request.strategy_name} · expires {fmtDateTime(request.expires_at)}
                    </div>
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={() => approve(request.request_id)}
                      disabled={busyRequest === request.request_id}
                      className="p-2 rounded-md bg-brand-green/15 text-brand-green hover:bg-brand-green/25 disabled:opacity-50"
                      title="Approve"
                    >
                      <CheckCircle size={14} />
                    </button>
                    <button
                      onClick={() => reject(request.request_id)}
                      disabled={busyRequest === request.request_id}
                      className="p-2 rounded-md bg-brand-red/15 text-brand-red hover:bg-brand-red/25 disabled:opacity-50"
                      title="Reject"
                    >
                      <XCircle size={14} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <CardBody>
              <div className="text-sm text-gray-600">No pending approvals</div>
            </CardBody>
          )}
        </Card>

        <Card>
          <CardHeader title="Broker Capabilities" subtitle={capabilities?.active_broker ?? '—'} icon={<GitBranch size={14} />} />
          <CardBody>
            <div className="grid grid-cols-2 gap-3 text-xs">
              {Object.entries(capabilities?.active_capabilities ?? {}).map(([key, value]) => (
                <div key={key} className="flex items-center justify-between rounded-md bg-surface-elevated px-3 py-2">
                  <span className="text-gray-500">{key}</span>
                  <span className="font-mono text-gray-200">{String(value)}</span>
                </div>
              ))}
            </div>
          </CardBody>
        </Card>
      </div>

      <Card>
        <CardHeader title="OMS Events" subtitle={`${omsEvents.length} recent`} />
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-surface-border text-gray-500 uppercase tracking-wider">
                <th className="px-4 py-2 text-left">Time</th>
                <th className="px-4 py-2 text-left">Event</th>
                <th className="px-4 py-2 text-left">Symbol</th>
                <th className="px-4 py-2 text-left">Strategy</th>
                <th className="px-4 py-2 text-left">Reason</th>
              </tr>
            </thead>
            <tbody>
              {omsEvents.map((event) => (
                <tr key={event.id} className="border-b border-surface-border/40 hover:bg-surface-elevated/40">
                  <td className="px-4 py-2 font-mono text-gray-500">{fmtDateTime(event.occurred_at)}</td>
                  <td className="px-4 py-2 text-gray-200">{event.event_type}</td>
                  <td className="px-4 py-2 font-mono text-brand-blue">{event.symbol ?? '—'}</td>
                  <td className="px-4 py-2 text-gray-400">{event.strategy_name ?? '—'}</td>
                  <td className="px-4 py-2 text-gray-500 max-w-xs truncate">{event.rejection_reason ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}

function Stat({ label, value, accent }: { label: string; value: string | number; accent: string }) {
  return (
    <div className="bg-surface-card border border-surface-border rounded-lg p-4">
      <div className="text-xs text-gray-500 mb-1">{label}</div>
      <div className={`text-xl font-bold font-mono ${accent}`}>{value}</div>
    </div>
  )
}
