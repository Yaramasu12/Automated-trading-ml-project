import { useEffect, useRef, useState } from 'react'
import { Cpu, Loader2, RefreshCw, Wifi, WifiOff } from 'lucide-react'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Badge } from '../components/shared/Badge'
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
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-gray-100">Account & Infrastructure</h1>
          <p className="text-xs text-gray-500 mt-0.5">Angel One connectivity, live feed, database stats</p>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-surface-elevated border border-surface-border text-xs text-gray-400 hover:text-gray-200"
        >
          {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
          Refresh
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
        {/* Angel One status */}
        <Card>
          <CardHeader title="Angel One Broker" icon={<Cpu size={14} />} />
          <CardBody className="space-y-2.5">
            {[
              { label: 'Broker', value: accountStatus?.broker ?? runtimeState?.broker ?? '—' },
              {
                label: 'API Configured',
                value: <Badge variant={accountStatus?.angel_one_configured ? 'green' : 'red'}>
                  {accountStatus?.angel_one_configured ? 'YES' : 'NOT SET'}
                </Badge>,
              },
              {
                label: 'Read-Only Available',
                value: <Badge variant={accountStatus?.read_only_available ? 'green' : 'gray'}>
                  {accountStatus?.read_only_available ? 'YES' : 'NO'}
                </Badge>,
              },
              {
                label: 'Live Orders',
                value: <Badge variant={accountStatus?.live_orders_possible ? 'green' : 'red'}>
                  {accountStatus?.live_orders_possible ? 'POSSIBLE' : 'BLOCKED'}
                </Badge>,
              },
              {
                label: 'Live Armed',
                value: <Badge variant={accountStatus?.live_armed ? 'orange' : 'gray'}>
                  {accountStatus?.live_armed ? 'ARMED' : 'DISARMED'}
                </Badge>,
              },
              {
                label: 'Kill Switch',
                value: <Badge variant={accountStatus?.kill_switch_active ? 'red' : 'green'} dot>
                  {accountStatus?.kill_switch_active ? 'ACTIVE' : 'OK'}
                </Badge>,
              },
            ].map(({ label, value }) => (
              <div key={label} className="flex items-center justify-between py-1.5 border-b border-surface-border/50">
                <span className="text-sm text-gray-400">{label}</span>
                <span className="text-sm font-mono text-gray-200">{value}</span>
              </div>
            ))}
          </CardBody>
        </Card>

        {/* Monitoring metrics */}
        {monitoring && (
          <Card>
            <CardHeader title="Runtime Metrics" />
            <CardBody className="space-y-2.5">
              {[
                { label: 'Status', value: <Badge variant={monitoring.status === 'HEALTHY' ? 'green' : monitoring.status === 'DEGRADED' ? 'yellow' : 'red'} dot>{monitoring.status}</Badge> },
                { label: 'Uptime', value: <span className="font-mono">{fmtUptime(monitoring.uptime_seconds)}</span> },
                { label: 'Total Orders', value: <span className="font-mono">{monitoring.total_orders}</span> },
                { label: 'Filled', value: <span className="font-mono text-brand-green">{monitoring.filled_orders}</span> },
                { label: 'Rejected', value: <span className="font-mono text-brand-red">{monitoring.rejected_orders}</span> },
                { label: 'Avg Latency', value: <span className="font-mono">{monitoring.average_latency_ms.toFixed(1)}ms</span> },
                { label: 'Max Latency', value: <span className="font-mono">{monitoring.max_latency_ms.toFixed(1)}ms</span> },
                { label: 'Stale Data', value: <Badge variant={monitoring.stale_market_data ? 'red' : 'green'}>{monitoring.stale_market_data ? 'YES' : 'NO'}</Badge> },
              ].map(({ label, value }) => (
                <div key={label} className="flex items-center justify-between py-1.5 border-b border-surface-border/50">
                  <span className="text-sm text-gray-400">{label}</span>
                  <span className="text-sm text-gray-200">{value}</span>
                </div>
              ))}
            </CardBody>
          </Card>
        )}
      </div>

      {/* Live feed control */}
      <Card>
        <CardHeader
          title="Live Market Feed"
          subtitle="Angel One WebSocket (SmartWebSocketV2)"
          icon={feedSnapshot?.running ? <Wifi size={14} className="text-brand-green" /> : <WifiOff size={14} className="text-gray-500" />}
        />
        <CardBody className="space-y-4">
          <div className="flex flex-wrap items-end gap-4">
            <div className="flex-1 min-w-48">
              <label className="block text-xs text-gray-500 mb-1">Symbols to subscribe</label>
              <input
                value={feedSymbols}
                onChange={(e) => setFeedSymbols(e.target.value)}
                placeholder="Blank = all cash/index instruments"
                className="w-full bg-surface-elevated border border-surface-border rounded-md px-3 py-1.5 text-sm text-gray-200 font-mono focus:outline-none focus:border-brand-blue"
              />
            </div>
            <button
              onClick={handleFeedStart}
              disabled={feedLoading || feedSnapshot?.running}
              className="flex items-center gap-2 px-4 py-1.5 rounded-md bg-brand-green/20 border border-brand-green/40 text-brand-green text-sm font-medium hover:bg-brand-green/30 disabled:opacity-50 transition-colors"
            >
              {feedLoading ? <Loader2 size={14} className="animate-spin" /> : <Wifi size={14} />}
              Start Feed
            </button>
            <button
              onClick={handleFeedStop}
              disabled={feedLoading || !feedSnapshot?.running}
              className="flex items-center gap-2 px-4 py-1.5 rounded-md bg-brand-red/20 border border-brand-red/40 text-brand-red text-sm font-medium hover:bg-brand-red/30 disabled:opacity-50 transition-colors"
            >
              {feedLoading ? <Loader2 size={14} className="animate-spin" /> : <WifiOff size={14} />}
              Stop Feed
            </button>
          </div>

          {feedSnapshot && (
            <div className="flex items-center gap-6 text-sm bg-surface-elevated rounded-md px-4 py-3">
              <div>
                <span className="text-gray-500 text-xs">State</span>
                <div className={clsx('font-bold font-mono', feedSnapshot.running ? 'text-brand-green' : 'text-gray-500')}>
                  {feedSnapshot.running ? 'RUNNING' : 'STOPPED'}
                </div>
              </div>
              <div>
                <span className="text-gray-500 text-xs">Tick Count</span>
                <div className="font-bold font-mono text-gray-200">{feedSnapshot.tick_count.toLocaleString()}</div>
              </div>
              <div className="flex-1">
                <span className="text-gray-500 text-xs">Subscribed Symbols</span>
                <div className="flex flex-wrap gap-1 mt-1">
                  {feedSnapshot.subscribed_symbols.length === 0 ? (
                    <span className="text-gray-600 text-xs">none</span>
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
          <CardHeader title="Database" subtitle={dbSummary.db_path} />
          <CardBody>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {[
                { label: 'Total Trades', value: dbSummary.total_trades, accent: 'text-brand-blue' },
                { label: 'Live Trades', value: dbSummary.live_trades, accent: 'text-brand-green' },
                { label: 'Portfolio Snapshots', value: dbSummary.portfolio_snapshots, accent: 'text-brand-cyan' },
                { label: 'Risk Blocks', value: dbSummary.risk_blocks, accent: 'text-brand-red' },
              ].map(({ label, value, accent }) => (
                <div key={label} className="bg-surface-elevated rounded-md p-3 text-center">
                  <div className={clsx('text-2xl font-bold font-mono', accent)}>{value}</div>
                  <div className="text-xs text-gray-500 mt-1">{label}</div>
                </div>
              ))}
            </div>
          </CardBody>
        </Card>
      )}
    </div>
  )
}
