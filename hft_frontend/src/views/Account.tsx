import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Cpu, Loader2, RefreshCw, Wifi, WifiOff, Database, Activity } from 'lucide-react'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Badge } from '../components/shared/Badge'
import { PageHeader } from '../components/shared/PageHeader'
import { useStore } from '../store'
import {
  getAccountStatus,
  getFeedSnapshot,
  getMonitoringMetrics,
  startFeed,
  stopFeed,
} from '../api'
import { fmtUptime } from '../utils'
import type { AccountStatus } from '../types'
import { clsx } from 'clsx'

interface FeedSnapshot {
  running: boolean
  subscribed_symbols: string[]
  tick_count: number
}

interface MonitoringFull {
  status: string
  started_at: string
  uptime_seconds: number
  execution_mode: string
  live_armed: boolean
  kill_switch_active: boolean
  stale_market_data: boolean
  total_orders: number
  filled_orders: number
  rejected_orders: number
  rejection_rate: number
  average_latency_ms: number
  max_latency_ms: number
  event_count: number
}

/** Compact key/value row used in the status panels. */
function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 py-1.5 border-b border-surface-border/60 last:border-0">
      <span className="text-sm text-ink-muted">{label}</span>
      <span className="text-sm font-mono text-ink">{children}</span>
    </div>
  )
}

export function Account() {
  const runtimeState = useStore((s) => s.runtimeState)
  const dbSummary = useStore((s) => s.dbSummary)

  const [accountStatus, setAccountStatus] = useState<AccountStatus | null>(null)
  const [feedSnapshot, setFeedSnapshot] = useState<FeedSnapshot | null>(null)
  const [monitoring, setMonitoring] = useState<MonitoringFull | null>(null)
  const [loading, setLoading] = useState(false)
  const [feedSymbols, setFeedSymbols] = useState('')
  const [feedLoading, setFeedLoading] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  async function load() {
    setLoading(true)
    const [status, feed, mon] = await Promise.allSettled([
      getAccountStatus(),
      getFeedSnapshot(),
      getMonitoringMetrics(),
    ])
    if (status.status === 'fulfilled') setAccountStatus(status.value)
    if (feed.status === 'fulfilled') setFeedSnapshot(feed.value as FeedSnapshot)
    if (mon.status === 'fulfilled') setMonitoring(mon.value as unknown as MonitoringFull)
    setLoading(false)
  }

  useEffect(() => {
    load()
    pollRef.current = setInterval(load, 10_000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [])

  async function handleFeedStart() {
    setFeedLoading(true)
    try {
      const symbols = feedSymbols.split(',').map((s) => s.trim()).filter(Boolean)
      await startFeed(symbols)
      await load()
    } finally {
      setFeedLoading(false)
    }
  }

  async function handleFeedStop() {
    setFeedLoading(true)
    try {
      await stopFeed()
      await load()
    } finally {
      setFeedLoading(false)
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Account & Infrastructure"
        subtitle="Angel One connectivity, live feed, database stats"
        icon={<Cpu size={18} />}
        actions={
          <button
            onClick={load}
            disabled={loading}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-surface-card border border-surface-border text-xs text-ink-muted hover:text-ink hover:border-surface-border-strong transition-all disabled:opacity-50"
          >
            {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
            <span className="hidden sm:inline">Refresh</span>
          </button>
        }
      />

      <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
        {/* Angel One status */}
        <Card>
          <CardHeader title="Angel One Broker" icon={<Cpu size={14} />} />
          <CardBody className="space-y-1">
            <Row label="Broker">{accountStatus?.broker ?? runtimeState?.broker ?? '—'}</Row>
            <Row label="API Configured">
              <Badge variant={accountStatus?.angel_one_configured ? 'green' : 'red'}>
                {accountStatus?.angel_one_configured ? 'YES' : 'NOT SET'}
              </Badge>
            </Row>
            <Row label="Read-Only Available">
              <Badge variant={accountStatus?.read_only_available ? 'green' : 'gray'}>
                {accountStatus?.read_only_available ? 'YES' : 'NO'}
              </Badge>
            </Row>
            <Row label="Live Orders">
              <Badge variant={accountStatus?.live_orders_possible ? 'green' : 'red'}>
                {accountStatus?.live_orders_possible ? 'POSSIBLE' : 'BLOCKED'}
              </Badge>
            </Row>
            <Row label="Live Armed">
              <Badge variant={accountStatus?.live_armed ? 'orange' : 'gray'}>
                {accountStatus?.live_armed ? 'ARMED' : 'DISARMED'}
              </Badge>
            </Row>
            <Row label="Kill Switch">
              <Badge variant={accountStatus?.kill_switch_active ? 'red' : 'green'} dot>
                {accountStatus?.kill_switch_active ? 'ACTIVE' : 'OK'}
              </Badge>
            </Row>
          </CardBody>
        </Card>

        {/* Monitoring metrics */}
        {monitoring && (
          <Card>
            <CardHeader title="Runtime Metrics" icon={<Activity size={14} />} />
            <CardBody className="space-y-1">
              <Row label="Status">
                <Badge variant={monitoring.status === 'HEALTHY' ? 'green' : monitoring.status === 'DEGRADED' ? 'yellow' : 'red'} dot>{monitoring.status}</Badge>
              </Row>
              <Row label="Uptime">{fmtUptime(monitoring.uptime_seconds)}</Row>
              <Row label="Total Orders">{monitoring.total_orders}</Row>
              <Row label="Filled"><span className="text-brand-green">{monitoring.filled_orders}</span></Row>
              <Row label="Rejected"><span className="text-brand-red">{monitoring.rejected_orders}</span></Row>
              <Row label="Avg Latency">{monitoring.average_latency_ms.toFixed(1)}ms</Row>
              <Row label="Max Latency">{monitoring.max_latency_ms.toFixed(1)}ms</Row>
              <Row label="Stale Data">
                <Badge variant={monitoring.stale_market_data ? 'red' : 'green'}>{monitoring.stale_market_data ? 'YES' : 'NO'}</Badge>
              </Row>
            </CardBody>
          </Card>
        )}
      </div>

      {/* Live feed control */}
      <Card>
        <CardHeader
          title="Live Market Feed"
          subtitle="Angel One WebSocket (SmartWebSocketV2)"
          icon={feedSnapshot?.running ? <Wifi size={14} className="text-brand-green" /> : <WifiOff size={14} className="text-ink-faint" />}
        />
        <CardBody className="space-y-4">
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex-1 min-w-48">
              <label className="block eyebrow mb-1.5">Symbols to subscribe</label>
              <input
                value={feedSymbols}
                onChange={(e) => setFeedSymbols(e.target.value)}
                placeholder="Blank = all cash/index instruments"
                className="w-full bg-surface-inset border border-surface-border rounded-lg px-3 py-2 text-sm text-ink font-mono placeholder:text-ink-faint focus:outline-none focus:border-brand-blue focus:ring-1 focus:ring-brand-blue/40"
              />
            </div>
            <button
              onClick={handleFeedStart}
              disabled={feedLoading || feedSnapshot?.running}
              className="flex items-center gap-2 px-4 py-2 rounded-lg bg-brand-green/15 border border-brand-green/35 text-brand-green text-sm font-semibold hover:bg-brand-green/25 disabled:opacity-40 transition-colors"
            >
              {feedLoading ? <Loader2 size={14} className="animate-spin" /> : <Wifi size={14} />}
              Start Feed
            </button>
            <button
              onClick={handleFeedStop}
              disabled={feedLoading || !feedSnapshot?.running}
              className="flex items-center gap-2 px-4 py-2 rounded-lg bg-brand-red/15 border border-brand-red/35 text-brand-red text-sm font-semibold hover:bg-brand-red/25 disabled:opacity-40 transition-colors"
            >
              {feedLoading ? <Loader2 size={14} className="animate-spin" /> : <WifiOff size={14} />}
              Stop Feed
            </button>
          </div>

          {feedSnapshot && (
            <div className="flex flex-wrap items-start gap-6 text-sm bg-surface-inset rounded-lg px-4 py-3 border border-surface-border">
              <div>
                <span className="eyebrow">State</span>
                <div className={clsx('font-bold font-mono mt-0.5', feedSnapshot.running ? 'text-brand-green' : 'text-ink-faint')}>
                  {feedSnapshot.running ? 'RUNNING' : 'STOPPED'}
                </div>
              </div>
              <div>
                <span className="eyebrow">Tick Count</span>
                <div className="font-bold font-mono text-ink mt-0.5">{feedSnapshot.tick_count.toLocaleString()}</div>
              </div>
              <div className="flex-1 min-w-48">
                <span className="eyebrow">Subscribed Symbols</span>
                <div className="flex flex-wrap gap-1 mt-1.5">
                  {feedSnapshot.subscribed_symbols.length === 0 ? (
                    <span className="text-ink-faint text-xs">none</span>
                  ) : (
                    feedSnapshot.subscribed_symbols.map((s) => (
                      <Badge key={s} variant="blue">{s}</Badge>
                    ))
                  )}
                </div>
              </div>
            </div>
          )}
        </CardBody>
      </Card>

      {/* Database summary */}
      {dbSummary && (
        <Card>
          <CardHeader title="Database" subtitle={dbSummary.db_path} icon={<Database size={14} />} />
          <CardBody>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {[
                { label: 'Total Trades', value: dbSummary.total_trades, accent: 'text-brand-blue' },
                { label: 'Live Trades', value: dbSummary.live_trades, accent: 'text-brand-green' },
                { label: 'Portfolio Snapshots', value: dbSummary.portfolio_snapshots, accent: 'text-brand-cyan' },
                { label: 'Risk Blocks', value: dbSummary.risk_blocks, accent: 'text-brand-red' },
              ].map(({ label, value, accent }) => (
                <div key={label} className="bg-surface-inset border border-surface-border rounded-lg p-3.5 text-center">
                  <div className={clsx('text-2xl font-bold font-mono', accent)}>{value}</div>
                  <div className="text-xs text-ink-faint mt-1">{label}</div>
                </div>
              ))}
            </div>
          </CardBody>
        </Card>
      )}
    </div>
  )
}
