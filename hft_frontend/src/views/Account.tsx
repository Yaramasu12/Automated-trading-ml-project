import { useEffect, useRef, useState, useCallback } from 'react'
import { Cpu, Loader2, RefreshCw, CheckCircle, XCircle, AlertTriangle, Database, Radio, Square, Play } from 'lucide-react'
import { clsx } from 'clsx'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Tag } from '../components/shared/Badge'
import { useStore } from '../store'
import {
  getAccountStatus,
  getDataStatus,
  refreshInstruments,
  loadCachedInstruments,
  armLive,
  getLiveCanaryReadiness,
  getFeedSnapshot,
  startFeed,
  stopFeed,
} from '../api'
import type { AccountStatus, LiveCanaryReadiness } from '../types'

interface LiveReadinessGate {
  label: string
  satisfied: boolean
  note: string
}

export function Account() {
  const runtimeState = useStore((s) => s.runtimeState)
  const monitoring   = useStore((s) => s.monitoring)
  const wsConnected  = useStore((s) => s.wsConnected)

  const [accountStatus, setAccountStatus] = useState<AccountStatus | null>(null)
  const [dataStatus, setDataStatus]       = useState<Record<string, unknown> | null>(null)
  const [loading, setLoading]             = useState(false)
  const [refreshing, setRefreshing]       = useState(false)
  const [feedLoading, setFeedLoading]     = useState(false)
  const [feedSnapshot, setFeedSnapshot]   = useState<Record<string, unknown> | null>(null)
  const [canaryReadiness, setCanaryReadiness] = useState<LiveCanaryReadiness | null>(null)
  const [feedSymbols, setFeedSymbols]     = useState('NIFTY,BANKNIFTY,RELIANCE,SBIN')
  const [arming, setArming]               = useState(false)
  const [armConfirm, setArmConfirm]       = useState(false)

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [acc, dat, feed, canary] = await Promise.allSettled([
        getAccountStatus(),
        getDataStatus(),
        getFeedSnapshot(),
        getLiveCanaryReadiness(),
      ])
      if (acc.status === 'fulfilled') setAccountStatus(acc.value)
      if (dat.status === 'fulfilled') setDataStatus(dat.value)
      if (feed.status === 'fulfilled') setFeedSnapshot(feed.value as Record<string, unknown>)
      if (canary.status === 'fulfilled') setCanaryReadiness(canary.value)
    } catch { /* ignore */ }
    finally { setLoading(false) }
  }, [])

  useEffect(() => {
    load()
    pollRef.current = setInterval(load, 30_000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [load])

  const handleRefreshInstruments = async () => {
    setRefreshing(true)
    try { await refreshInstruments(); await load() }
    catch { /* ignore */ }
    finally { setRefreshing(false) }
  }

  const handleLoadCache = async () => {
    setRefreshing(true)
    try { await loadCachedInstruments(); await load() }
    catch { /* ignore */ }
    finally { setRefreshing(false) }
  }

  const handleStartFeed = async () => {
    setFeedLoading(true)
    try {
      const symbols = feedSymbols.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean)
      await startFeed(symbols)
      await load()
    } catch { /* ignore */ }
    finally { setFeedLoading(false) }
  }

  const handleStopFeed = async () => {
    setFeedLoading(true)
    try { await stopFeed(); await load() }
    catch { /* ignore */ }
    finally { setFeedLoading(false) }
  }

  const handleArmLive = async (armed: boolean) => {
    setArming(true)
    try { await armLive(armed); await load(); setArmConfirm(false) }
    catch { /* ignore */ }
    finally { setArming(false) }
  }

  // Live Readiness Gates
  const gates: LiveReadinessGate[] = [
    {
      label: 'Angel One Configured',
      satisfied: accountStatus?.angel_one_configured ?? false,
      note: 'API key and secret must be set in environment variables',
    },
    {
      label: 'Read-Only Available',
      satisfied: accountStatus?.read_only_available ?? false,
      note: 'Market data feed is accessible',
    },
    {
      label: 'Live Orders Possible',
      satisfied: accountStatus?.live_orders_possible ?? false,
      note: 'Order placement API is operational',
    },
    {
      label: 'WebSocket Connected',
      satisfied: wsConnected,
      note: 'Real-time dashboard WebSocket must be connected',
    },
    {
      label: 'Engine Healthy',
      satisfied: monitoring?.status === 'HEALTHY',
      note: 'Backend system status must be HEALTHY',
    },
    {
      label: 'Kill Switch OFF',
      satisfied: !(runtimeState?.kill_switch_active ?? true),
      note: 'Kill switch must be deactivated before going live',
    },
    {
      label: 'Instrument Cache Ready',
      satisfied: Boolean(dataStatus?.instrument_cache_exists),
      note: 'Instrument master cache must be loaded',
    },
  ]

  const gatesPassed = gates.filter(g => g.satisfied).length
  const allGatesPassed = gatesPassed === gates.length
  const isLiveMode = runtimeState?.execution_mode?.startsWith('LIVE')
  const feedRunning = Boolean(feedSnapshot?.running)

  return (
    <div className="space-y-5">

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Cpu size={16} className="text-brand-blue" />
          <div>
            <h1 className="text-lg font-bold text-gray-100">Account & Configuration</h1>
            <p className="text-xs text-gray-500 mt-0.5">Angel One broker integration, credentials, live readiness</p>
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

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        {/* Broker Status */}
        <Card>
          <CardHeader title="Broker Status" icon={<Cpu size={14} />} />
          <CardBody className="space-y-3">
            {accountStatus ? (
              <>
                <div className="flex items-center justify-between py-1.5 border-b border-surface-border/50">
                  <span className="text-xs text-gray-400">Broker</span>
                  <span className="text-xs font-mono font-bold text-brand-blue">{accountStatus.broker}</span>
                </div>
                {[
                  { label: 'Configured', value: accountStatus.angel_one_configured },
                  { label: 'Read-Only', value: accountStatus.read_only_available },
                  { label: 'Live Orders', value: accountStatus.live_orders_possible },
                  { label: 'Live Armed', value: accountStatus.live_armed },
                  { label: 'Kill Switch', value: accountStatus.kill_switch_active },
                ].map(({ label, value }) => (
                  <div key={label} className="flex items-center justify-between py-1.5 border-b border-surface-border/50">
                    <span className="text-xs text-gray-400">{label}</span>
                    <div className="flex items-center gap-1.5">
                      {value
                        ? <CheckCircle size={12} className="text-brand-green" />
                        : <XCircle size={12} className="text-brand-red" />}
                      <span className={clsx('text-xs font-mono', value ? 'text-brand-green' : 'text-gray-500')}>
                        {String(value)}
                      </span>
                    </div>
                  </div>
                ))}
              </>
            ) : (
              <div className="text-xs text-gray-500 text-center py-4">Loading broker status...</div>
            )}
          </CardBody>
        </Card>

        {/* Live Readiness */}
        <Card>
          <CardHeader
            title="Live Readiness"
            subtitle={`${gatesPassed} / ${gates.length} gates passed`}
            icon={<CheckCircle size={14} className={allGatesPassed ? 'text-brand-green' : 'text-brand-yellow'} />}
            action={
              <Tag
                label={allGatesPassed ? 'READY' : 'NOT READY'}
                color={allGatesPassed ? 'green' : 'red'}
              />
            }
          />
          <CardBody className="space-y-2">
            {gates.map((gate) => (
              <div key={gate.label} className="flex items-start gap-3 py-1.5">
                {gate.satisfied
                  ? <CheckCircle size={14} className="text-brand-green mt-0.5 flex-shrink-0" />
                  : <XCircle size={14} className="text-brand-red mt-0.5 flex-shrink-0" />}
                <div>
                  <div className={clsx(
                    'text-xs font-medium',
                    gate.satisfied ? 'text-gray-200' : 'text-gray-400',
                  )}>
                    {gate.label}
                  </div>
                  <div className="text-[10px] text-gray-600 mt-0.5">{gate.note}</div>
                </div>
              </div>
            ))}
          </CardBody>
        </Card>

        {/* M6 Live-Canary Readiness */}
        <Card>
          <CardHeader
            title="Live-Canary Readiness"
            subtitle={canaryReadiness ? `${canaryReadiness.paper_window.actual_days} paper day(s)` : 'paper evidence window'}
            icon={<AlertTriangle size={14} className={canaryReadiness?.can_consider_live_canary ? 'text-brand-green' : 'text-brand-yellow'} />}
            action={
              <Tag
                label={canaryReadiness?.status ?? 'UNKNOWN'}
                color={canaryReadiness?.can_consider_live_canary ? 'green' : 'red'}
              />
            }
          />
          <CardBody className="space-y-3">
            {canaryReadiness ? (
              <>
                <div className="grid grid-cols-2 gap-2">
                  {[
                    { label: 'Paper Days', value: canaryReadiness.paper_window.actual_days },
                    { label: 'Trace Evidence', value: canaryReadiness.evidence.trace_count },
                    { label: 'Fills', value: String(canaryReadiness.metrics.fill_count ?? 0) },
                    { label: 'Labels', value: String(canaryReadiness.metrics.label_count ?? 0) },
                  ].map((item) => (
                    <div key={item.label} className="rounded-md bg-surface-elevated border border-surface-border p-2">
                      <div className="text-[10px] text-gray-500">{item.label}</div>
                      <div className="text-sm font-mono font-bold text-gray-100 mt-1">{item.value}</div>
                    </div>
                  ))}
                </div>
                <div className="space-y-1.5">
                  {canaryReadiness.checks.slice(0, 6).map((check) => (
                    <div key={check.name} className="flex items-center gap-2">
                      {check.passed
                        ? <CheckCircle size={12} className="text-brand-green" />
                        : <XCircle size={12} className="text-brand-red" />}
                      <span className="text-xs text-gray-400">{check.name.replace(/_/g, ' ')}</span>
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <div className="text-xs text-gray-500 text-center py-4">Loading canary readiness...</div>
            )}
          </CardBody>
        </Card>

        {/* Instrument Master */}
        <Card>
          <CardHeader title="Instrument Master" icon={<Database size={14} />} />
          <CardBody className="space-y-3">
            {dataStatus ? (
              <>
                <div className="grid grid-cols-2 gap-2">
                  {[
                    { label: 'Cache Exists', value: String(dataStatus.instrument_cache_exists) },
                    { label: 'Universe Count', value: String(dataStatus.current_universe_count ?? '—') },
                    { label: 'Synthetic', value: String(dataStatus.instrument_master_is_synthetic ?? '—') },
                    { label: 'Angel One API', value: String(dataStatus.angel_one_configured) },
                  ].map(({ label, value }) => (
                    <div key={label} className="bg-surface-elevated rounded p-2">
                      <div className="text-[10px] text-gray-500 uppercase">{label}</div>
                      <div className="text-sm font-bold font-mono text-gray-100 mt-1">{value}</div>
                    </div>
                  ))}
                </div>
                <div className="flex gap-2 pt-2">
                  <button
                    onClick={handleRefreshInstruments}
                    disabled={refreshing}
                    className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-blue/15 border border-brand-blue/30 text-brand-blue text-xs font-medium hover:bg-brand-blue/25 disabled:opacity-50 transition-colors"
                  >
                    {refreshing ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
                    Refresh Instruments
                  </button>
                  <button
                    onClick={handleLoadCache}
                    disabled={refreshing || !dataStatus.instrument_cache_exists}
                    className="flex items-center gap-2 px-4 py-2 rounded-md bg-surface-elevated border border-surface-border text-gray-300 text-xs font-medium hover:text-gray-100 disabled:opacity-50 transition-colors"
                  >
                    {refreshing ? <Loader2 size={12} className="animate-spin" /> : <Database size={12} />}
                    Load Cache
                  </button>
                </div>
              </>
            ) : (
              <div className="text-xs text-gray-500 text-center py-4">Loading data status...</div>
            )}
          </CardBody>
        </Card>

        {/* Paper Live Feed */}
        <Card>
          <CardHeader
            title="Paper Live Feed"
            subtitle="Real Angel ticks, simulated orders only"
            icon={<Radio size={14} className={feedRunning ? 'text-brand-green' : 'text-brand-blue'} />}
            action={<Tag label={feedRunning ? 'RUNNING' : 'STOPPED'} color={feedRunning ? 'green' : 'gray'} />}
          />
          <CardBody className="space-y-3">
            <div className="grid grid-cols-2 gap-2">
              {[
                { label: 'Subscribed', value: String((feedSnapshot?.subscribed_symbols as unknown[] | undefined)?.length ?? 0) },
                { label: 'Ticks', value: String(feedSnapshot?.tick_count ?? 0) },
                { label: 'Mode', value: String(feedSnapshot?.mode ?? 'paper_market_data') },
                { label: 'Live Orders', value: String(feedSnapshot?.live_orders_possible ?? false) },
              ].map(({ label, value }) => (
                <div key={label} className="bg-surface-elevated rounded p-2">
                  <div className="text-[10px] text-gray-500 uppercase">{label}</div>
                  <div className="text-sm font-bold font-mono text-gray-100 mt-1 truncate">{value}</div>
                </div>
              ))}
            </div>
            <input
              value={feedSymbols}
              onChange={(event) => setFeedSymbols(event.target.value)}
              className="w-full bg-surface-elevated border border-surface-border rounded px-3 py-2 text-xs font-mono text-gray-200 focus:outline-none focus:border-brand-blue"
              placeholder="NIFTY,BANKNIFTY,RELIANCE"
            />
            <div className="flex gap-2">
              <button
                onClick={handleStartFeed}
                disabled={feedLoading}
                className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-green/15 border border-brand-green/30 text-brand-green text-xs font-medium hover:bg-brand-green/25 disabled:opacity-50 transition-colors"
              >
                {feedLoading ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
                Start Feed
              </button>
              <button
                onClick={handleStopFeed}
                disabled={feedLoading || !feedRunning}
                className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-red/15 border border-brand-red/30 text-brand-red text-xs font-medium hover:bg-brand-red/25 disabled:opacity-50 transition-colors"
              >
                {feedLoading ? <Loader2 size={12} className="animate-spin" /> : <Square size={12} />}
                Stop Feed
              </button>
            </div>
          </CardBody>
        </Card>

        {/* Credentials Status */}
        <Card>
          <CardHeader title="Credentials" icon={<Cpu size={14} />} />
          <CardBody className="space-y-2">
            <div className="text-xs text-gray-500 mb-3">
              Credentials are read from environment variables and are not displayed for security.
            </div>
            {[
              { label: 'ANGEL_ONE_API_KEY',    configured: accountStatus?.angel_one_configured ?? false },
              { label: 'ANGEL_ONE_CLIENT_ID',  configured: accountStatus?.angel_one_configured ?? false },
              { label: 'ANGEL_ONE_PASSWORD',   configured: accountStatus?.angel_one_configured ?? false },
              { label: 'ANGEL_ONE_TOTP_TOKEN', configured: accountStatus?.live_orders_possible ?? false },
            ].map(({ label, configured }) => (
              <div key={label} className="flex items-center justify-between py-1.5 border-b border-surface-border/50">
                <span className="text-xs font-mono text-gray-400">{label}</span>
                <Tag label={configured ? 'configured' : 'missing'} color={configured ? 'green' : 'red'} />
              </div>
            ))}
          </CardBody>
        </Card>
      </div>

      {/* Live Arming — only show in LIVE mode */}
      {isLiveMode && (
        <Card className="border-brand-red/20">
          <CardHeader
            title="Live Mode Arming"
            icon={<AlertTriangle size={14} className="text-brand-red" />}
            subtitle="Only available in LIVE execution mode"
          />
          <CardBody className="space-y-3">
            <p className="text-xs text-brand-red/80">
              Arming live mode allows the system to submit real orders to Angel One.
              Ensure all gates pass before arming.
            </p>
            <div className="flex items-center gap-3">
              <Tag
                label={runtimeState?.live_armed ? 'ARMED' : 'DISARMED'}
                color={runtimeState?.live_armed ? 'red' : 'gray'}
              />
            </div>
            {!armConfirm ? (
              <button
                onClick={() => setArmConfirm(true)}
                disabled={!allGatesPassed}
                className={clsx(
                  'flex items-center gap-2 px-4 py-2 rounded-md text-xs font-medium border transition-colors',
                  runtimeState?.live_armed
                    ? 'bg-surface-elevated border-surface-border text-gray-400 hover:text-gray-200'
                    : allGatesPassed
                      ? 'bg-brand-red/15 border-brand-red/30 text-brand-red hover:bg-brand-red/25'
                      : 'opacity-50 cursor-not-allowed bg-surface-elevated border-surface-border text-gray-600',
                )}
              >
                <AlertTriangle size={12} />
                {runtimeState?.live_armed ? 'Disarm Live' : 'Arm Live Mode'}
              </button>
            ) : (
              <div className="space-y-2">
                <p className="text-xs text-brand-red font-semibold">
                  Confirm: {runtimeState?.live_armed ? 'Disarm' : 'Arm'} live trading?
                </p>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => handleArmLive(!runtimeState?.live_armed)}
                    disabled={arming}
                    className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-red/20 border border-brand-red/50 text-brand-red text-xs font-bold hover:bg-brand-red/30 transition-colors"
                  >
                    {arming ? <Loader2 size={12} className="animate-spin" /> : <AlertTriangle size={12} />}
                    CONFIRM
                  </button>
                  <button
                    onClick={() => setArmConfirm(false)}
                    className="px-3 py-2 rounded-md bg-surface-elevated border border-surface-border text-gray-400 text-xs hover:text-gray-200 transition-colors"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </CardBody>
        </Card>
      )}

    </div>
  )
}
