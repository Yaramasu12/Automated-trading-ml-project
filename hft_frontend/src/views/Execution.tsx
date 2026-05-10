import { useEffect, useRef, useState, useCallback } from 'react'
import {
  GitBranch, Loader2, RefreshCw, CheckCircle, XCircle, AlertTriangle, Activity,
} from 'lucide-react'
import { clsx } from 'clsx'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Table } from '../components/shared/Table'
import { Tag } from '../components/shared/Badge'
import {
  getSchedulerStats, getOmsEvents, getManualApprovals, getBrokerCapabilities,
  approveManualApproval, rejectManualApproval, squareOff, reconcilePositions,
  getEventSummary, getRecentEvents,
} from '../api'
import { fmtDateTime } from '../utils'

export function Execution() {
  const [schedulerStats, setSchedulerStats]     = useState<Record<string, unknown> | null>(null)
  const [omsEvents, setOmsEvents]               = useState<Record<string, unknown>[]>([])
  const [approvals, setApprovals]               = useState<Record<string, unknown>[]>([])
  const [brokerCaps, setBrokerCaps]             = useState<Record<string, unknown> | null>(null)
  const [eventSummary, setEventSummary]         = useState<Record<string, unknown> | null>(null)
  const [recentEvents, setRecentEvents]         = useState<Record<string, unknown>[]>([])
  const [loading, setLoading]                   = useState(false)
  const [squareOffConfirm, setSquareOffConfirm] = useState(false)
  const [squareOffLoading, setSquareOffLoading] = useState(false)
  const [reconcileLoading, setReconcileLoading] = useState(false)
  const [reconcileResult, setReconcileResult]   = useState<string | null>(null)

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [sched, oms, apps, caps, evtSum, evts] = await Promise.allSettled([
        getSchedulerStats(),
        getOmsEvents(30),
        getManualApprovals(),
        getBrokerCapabilities(),
        getEventSummary(),
        getRecentEvents(20),
      ])
      if (sched.status === 'fulfilled') setSchedulerStats(sched.value)
      if (oms.status === 'fulfilled') {
        const v = oms.value as { events?: Record<string, unknown>[] }
        setOmsEvents(v.events ?? [])
      }
      if (apps.status === 'fulfilled') {
        const v = apps.value as unknown as { pending?: Record<string, unknown>[] }
        setApprovals(v.pending ?? [])
      }
      if (caps.status === 'fulfilled') setBrokerCaps(caps.value as unknown as Record<string, unknown>)
      if (evtSum.status === 'fulfilled') setEventSummary(evtSum.value as unknown as Record<string, unknown>)
      if (evts.status === 'fulfilled') {
        const v = evts.value as { events?: Record<string, unknown>[] }
        setRecentEvents(v.events ?? [])
      }
    } catch { /* ignore */ }
    finally { setLoading(false) }
  }, [])

  useEffect(() => {
    load()
    pollRef.current = setInterval(load, 15_000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [load])

  const handleApprove = async (reqId: string) => {
    try { await approveManualApproval(reqId, 'Approved via UI'); await load() } catch { /* ignore */ }
  }
  const handleReject = async (reqId: string) => {
    try { await rejectManualApproval(reqId, 'Rejected via UI'); await load() } catch { /* ignore */ }
  }

  const handleSquareOff = async () => {
    setSquareOffLoading(true)
    try { await squareOff({ reason: 'Manual emergency square-off via UI' }); setSquareOffConfirm(false) }
    catch { /* ignore */ }
    finally { setSquareOffLoading(false) }
  }

  const handleReconcile = async () => {
    setReconcileLoading(true)
    try {
      const res = await reconcilePositions()
      setReconcileResult(String((res as { message?: string }).message ?? 'Reconciliation complete'))
    } catch (e) {
      setReconcileResult(e instanceof Error ? e.message : 'Reconciliation failed')
    } finally { setReconcileLoading(false) }
  }

  return (
    <div className="space-y-5">

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <GitBranch size={16} className="text-brand-blue" />
          <div>
            <h1 className="text-lg font-bold text-gray-100">Execution Plane</h1>
            <p className="text-xs text-gray-500 mt-0.5">Order routing, OMS events, manual approvals</p>
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

      {/* Scheduler Stats */}
      {schedulerStats && (
        <Card>
          <CardHeader title="Scheduler Stats" icon={<Activity size={14} />} />
          <CardBody>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {[
                { label: 'Queue Depth',  value: String(schedulerStats.queued_tasks ?? schedulerStats.queue_depth ?? 0) },
                { label: 'Active Tasks', value: String(schedulerStats.active_tasks ?? 0) },
                { label: 'Avg Latency',  value: `${Number(schedulerStats.avg_latency_ms ?? 0).toFixed(0)}ms` },
                { label: 'Max Latency',  value: `${Number(schedulerStats.max_latency_ms ?? 0).toFixed(0)}ms` },
              ].map((s) => (
                <div key={s.label} className="bg-surface-elevated rounded-lg p-3">
                  <div className="text-[10px] text-gray-500 uppercase tracking-wider">{s.label}</div>
                  <div className="text-lg font-bold font-mono text-gray-100 mt-1">{s.value}</div>
                </div>
              ))}
            </div>
          </CardBody>
        </Card>
      )}

      {/* OMS Events */}
      <Card>
        <CardHeader title="OMS Events" subtitle={`${omsEvents.length} recent events`} icon={<GitBranch size={14} />} />
        <Table
          columns={[
            {
              key: 'order_id', label: 'Order ID',
              render: (v) => <span className="text-xs font-mono text-gray-400">{String(v).slice(0, 14)}</span>,
            },
            {
              key: 'symbol', label: 'Symbol',
              render: (v) => <span className="text-brand-blue font-semibold">{String(v)}</span>,
            },
            { key: 'event_type', label: 'Event Type' },
            {
              key: 'status', label: 'Status',
              render: (v) => (
                <Tag
                  label={String(v)}
                  color={
                    String(v) === 'FILLED'   ? 'green'
                    : String(v) === 'REJECTED' ? 'red'
                    : String(v) === 'PENDING'  ? 'yellow'
                    : 'gray'
                  }
                />
              ),
            },
            {
              key: 'timestamp', label: 'Time',
              render: (v) => <span className="text-xs text-gray-500">{fmtDateTime(String(v))}</span>,
            },
          ]}
          rows={omsEvents.slice(0, 30)}
          keyFn={(r) => String(r.order_id ?? r.event_id ?? Math.random())}
          emptyMessage="No OMS events"
          compact
        />
      </Card>

      {/* Manual Approvals */}
      {approvals.length > 0 && (
        <Card>
          <CardHeader
            title="Manual Approvals"
            subtitle={`${approvals.length} pending`}
            icon={<CheckCircle size={14} />}
            action={<Tag label={`${approvals.length} pending`} color="yellow" />}
          />
          <div className="divide-y divide-surface-border">
            {approvals.map((req) => (
              <div key={String(req.request_id)} className="px-4 py-3 flex items-center justify-between gap-3">
                <div>
                  <div className="text-sm font-medium text-gray-200">
                    {String(req.side)} {String(req.symbol)} × {String(req.quantity)}
                  </div>
                  <div className="text-xs text-gray-500 mt-0.5">
                    {String(req.strategy_name)} · State: {String(req.state)}
                  </div>
                  <div className="text-xs text-gray-600">
                    Expires: {fmtDateTime(String(req.expires_at))}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => handleApprove(String(req.request_id))}
                    className="flex items-center gap-1 px-2.5 py-1.5 rounded text-xs bg-brand-green/15 border border-brand-green/30 text-brand-green hover:bg-brand-green/25 transition-colors"
                  >
                    <CheckCircle size={12} /> Approve
                  </button>
                  <button
                    onClick={() => handleReject(String(req.request_id))}
                    className="flex items-center gap-1 px-2.5 py-1.5 rounded text-xs bg-brand-red/15 border border-brand-red/30 text-brand-red hover:bg-brand-red/25 transition-colors"
                  >
                    <XCircle size={12} /> Reject
                  </button>
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* Event Bus */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <Card>
          <CardHeader title="Event Bus" icon={<Activity size={14} />} />
          <CardBody className="space-y-3">
            {eventSummary ? (
              <>
                <div className="grid grid-cols-2 gap-2">
                  <div className="bg-surface-elevated rounded p-2">
                    <div className="text-[10px] text-gray-500 uppercase">Total Events</div>
                    <div className="text-lg font-bold font-mono text-gray-100 mt-1">
                      {String(eventSummary.event_count ?? 0)}
                    </div>
                  </div>
                  <div className="bg-surface-elevated rounded p-2">
                    <div className="text-[10px] text-gray-500 uppercase">Backend</div>
                    <div className="text-sm font-bold font-mono text-brand-cyan mt-1">
                      {String(eventSummary.backend ?? '—')}
                    </div>
                  </div>
                </div>
                {eventSummary.streams && (
                  <div>
                    <div className="text-xs text-gray-400 mb-2">Stream Counts</div>
                    {Object.entries(eventSummary.streams as Record<string, number>).map(([stream, count]) => (
                      <div key={stream} className="flex items-center justify-between text-xs py-1 border-b border-surface-border/30">
                        <span className="text-gray-400 font-mono">{stream}</span>
                        <span className="font-mono text-gray-200">{count}</span>
                      </div>
                    ))}
                  </div>
                )}
                {eventSummary.latest_event && (
                  <div className="bg-surface-elevated rounded p-2 mt-2">
                    <div className="text-[10px] text-gray-500 uppercase mb-1">Latest Event</div>
                    <div className="text-xs text-gray-300">
                      {String((eventSummary.latest_event as Record<string, unknown>).event_name ?? '—')}
                    </div>
                  </div>
                )}
              </>
            ) : (
              <div className="text-xs text-gray-500 text-center py-4">Loading event bus data...</div>
            )}
          </CardBody>
        </Card>

        {/* Broker Capabilities */}
        <Card>
          <CardHeader title="Broker Capabilities" icon={<GitBranch size={14} />} />
          <CardBody className="space-y-2">
            {brokerCaps ? (
              <>
                <div className="text-xs">
                  <span className="text-gray-400">Active Broker: </span>
                  <span className="text-brand-blue font-mono font-semibold">
                    {String((brokerCaps as { active_broker?: string }).active_broker ?? '—')}
                  </span>
                </div>
                {(brokerCaps as { active_capabilities?: Record<string, unknown> }).active_capabilities && (
                  <div className="space-y-1 pt-2 border-t border-surface-border">
                    <div className="text-xs text-gray-400 uppercase tracking-wider mb-2">Capabilities</div>
                    {Object.entries((brokerCaps as { active_capabilities: Record<string, unknown> }).active_capabilities).map(([k, v]) => (
                      <div key={k} className="flex items-center justify-between text-xs py-1">
                        <span className="text-gray-400 capitalize">{k.replace(/_/g, ' ')}</span>
                        <Tag
                          label={String(v)}
                          color={String(v) === 'true' || v === true ? 'green' : 'gray'}
                        />
                      </div>
                    ))}
                  </div>
                )}
              </>
            ) : (
              <div className="text-xs text-gray-500 text-center py-4">Loading broker data...</div>
            )}
          </CardBody>
        </Card>
      </div>

      {/* Actions Row */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
        {/* Reconciliation */}
        <Card>
          <CardHeader title="Position Reconciliation" icon={<CheckCircle size={14} />} />
          <CardBody className="space-y-3">
            <p className="text-xs text-gray-400">
              Reconcile internal positions against broker records to detect discrepancies.
            </p>
            <button
              onClick={handleReconcile}
              disabled={reconcileLoading}
              className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-blue/15 border border-brand-blue/30 text-brand-blue text-xs font-medium hover:bg-brand-blue/25 disabled:opacity-50 transition-colors"
            >
              {reconcileLoading ? <Loader2 size={12} className="animate-spin" /> : <CheckCircle size={12} />}
              Reconcile Positions
            </button>
            {reconcileResult && (
              <div className="text-xs text-gray-300 bg-surface-elevated rounded p-2">{reconcileResult}</div>
            )}
          </CardBody>
        </Card>

        {/* Emergency Square-Off */}
        <Card className="border-brand-red/20">
          <CardHeader title="Emergency Square-Off" icon={<AlertTriangle size={14} className="text-brand-red" />} />
          <CardBody className="space-y-3">
            <p className="text-xs text-brand-red/80">
              Danger: This will immediately close ALL open positions at market price.
            </p>
            {!squareOffConfirm ? (
              <button
                onClick={() => setSquareOffConfirm(true)}
                className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-red/15 border border-brand-red/30 text-brand-red text-xs font-medium hover:bg-brand-red/25 transition-colors"
              >
                <AlertTriangle size={12} /> Emergency Square-Off
              </button>
            ) : (
              <div className="space-y-2">
                <p className="text-xs text-brand-red font-semibold">Are you absolutely sure? This cannot be undone.</p>
                <div className="flex items-center gap-2">
                  <button
                    onClick={handleSquareOff}
                    disabled={squareOffLoading}
                    className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-red/20 border border-brand-red/50 text-brand-red text-xs font-bold hover:bg-brand-red/30 transition-colors"
                  >
                    {squareOffLoading ? <Loader2 size={12} className="animate-spin" /> : <AlertTriangle size={12} />}
                    CONFIRM SQUARE-OFF
                  </button>
                  <button
                    onClick={() => setSquareOffConfirm(false)}
                    className="px-3 py-2 rounded-md bg-surface-elevated border border-surface-border text-gray-400 text-xs hover:text-gray-200 transition-colors"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </CardBody>
        </Card>
      </div>

    </div>
  )
}
