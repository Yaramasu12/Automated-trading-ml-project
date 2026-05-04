import { useState, useEffect, useCallback, useRef } from 'react'
import {
  Play, Square, Activity, Shield,
  Brain, Cpu, Bot, AlertTriangle,
  Target, DollarSign, BarChart2, List, Eye, Layers,
} from 'lucide-react'
import { clsx } from 'clsx'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { useStore } from '../store'
import {
  getAgentStatus, startAgent, stopAgent, setAgentInterval,
  getLatestTick, getSchedulerStats, getOmsEvents,
  getAgentTrades, getPortfolioPositions, getRiskRejections,
  getGovernanceDashboard, getActiveExitPlans,
} from '../api'
import { inr, pct } from '../utils'

// ── Universe ──────────────────────────────────────────────────────────────────
const NSE_INDICES   = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY']
const BSE_INDICES   = ['SENSEX', 'BANKEX']
const ALL_INDICES   = [...NSE_INDICES, ...BSE_INDICES]
const CORE_EQUITIES = ['RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 'SBIN']
const EXT_EQUITIES  = ['WIPRO', 'KOTAKBANK', 'AXISBANK', 'MARUTI', 'SUNPHARMA',
                        'TATAMOTORS', 'BAJFINANCE', 'HINDUNILVR', 'BHARTIARTL', 'NTPC']
const ALL_EQUITIES  = [...CORE_EQUITIES, ...EXT_EQUITIES]
const FO_UNDERLYINGS = [...NSE_INDICES, ...CORE_EQUITIES, 'WIPRO', 'KOTAKBANK', 'AXISBANK',
                         'MARUTI', 'SUNPHARMA', 'TATAMOTORS', 'BAJFINANCE', 'HINDUNILVR',
                         'BHARTIARTL', 'NTPC']
const ALL_UNDERLYINGS = [...ALL_INDICES, ...ALL_EQUITIES]

const LOT_SIZE: Record<string, number> = {
  NIFTY:50, BANKNIFTY:15, FINNIFTY:40, MIDCPNIFTY:75,
  RELIANCE:250, TCS:175, INFY:400, HDFCBANK:550, ICICIBANK:700, SBIN:750,
  WIPRO:1500, KOTAKBANK:400, AXISBANK:1200, MARUTI:100, SUNPHARMA:700,
  TATAMOTORS:1500, BAJFINANCE:125, HINDUNILVR:300, BHARTIARTL:950, NTPC:3000,
}

const BASE_PRICE: Record<string, number> = {
  NIFTY:24500, BANKNIFTY:52000, FINNIFTY:24200, MIDCPNIFTY:12100,
  SENSEX:80500, BANKEX:55000,
  RELIANCE:2850, TCS:3500, INFY:1500, HDFCBANK:1620, ICICIBANK:1130, SBIN:800,
  WIPRO:460, KOTAKBANK:1800, AXISBANK:1070, MARUTI:11200, SUNPHARMA:1680,
  TATAMOTORS:760, BAJFINANCE:7200, HINDUNILVR:2320, BHARTIARTL:1630, NTPC:375,
}

const INTERVALS = [
  { label: '30s', s: 30 }, { label: '1m', s: 60 },
  { label: '5m',  s: 300 }, { label: '15m', s: 900 },
]

const TABS = [
  { id: 'overview',    label: 'Overview',    icon: <Cpu size={13} /> },
  { id: 'positions',   label: 'Positions',   icon: <DollarSign size={13} /> },
  { id: 'trades',      label: 'Trade Log',   icon: <List size={13} /> },
  { id: 'governance',  label: 'Governance',  icon: <Shield size={13} /> },
  { id: 'prices',      label: 'Live Prices', icon: <Activity size={13} /> },
  { id: 'options',     label: 'Options',     icon: <Layers size={13} /> },
  { id: 'risk',        label: 'Risk Log',    icon: <AlertTriangle size={13} /> },
  { id: 'activity',    label: 'Activity',    icon: <Eye size={13} /> },
]

// ── Tiny components ──────────────────────────────────────────────────────────
function StatBox({ label, value, sub, color = 'text-gray-200' }: {
  label: string; value: string | number; sub?: string; color?: string
}) {
  return (
    <div className="bg-surface-elevated rounded border border-surface-border p-3">
      <div className="text-[10px] text-gray-500 mb-1">{label}</div>
      <div className={clsx('text-base font-mono font-bold', color)}>{value}</div>
      {sub && <div className="text-[10px] text-gray-600 mt-0.5">{sub}</div>}
    </div>
  )
}

function PnlBadge({ value }: { value: number }) {
  const positive = value >= 0
  return (
    <span className={clsx('font-mono text-xs font-semibold', positive ? 'text-emerald-400' : 'text-red-400')}>
      {positive ? '+' : ''}{value.toFixed(2)}%
    </span>
  )
}

function LiveDot({ live }: { live: boolean }) {
  return (
    <span className={clsx('inline-block w-2 h-2 rounded-full mr-1', live ? 'bg-emerald-400 animate-pulse' : 'bg-gray-600')} />
  )
}

function TickBox({ symbol, tick }: { symbol: string; tick: any }) {
  const price  = tick?.last_price ?? tick?.ltp ?? null
  const change = price && BASE_PRICE[symbol] ? ((price - BASE_PRICE[symbol]) / BASE_PRICE[symbol] * 100) : null
  const up     = change !== null ? change >= 0 : null
  return (
    <div className="bg-surface-elevated rounded border border-surface-border p-2">
      <div className="flex justify-between items-start mb-1">
        <span className="text-[10px] font-semibold text-gray-300">{symbol}</span>
        <LiveDot live={price !== null} />
      </div>
      {price !== null ? (
        <>
          <div className={clsx('text-sm font-mono font-bold', up ? 'text-emerald-400' : 'text-red-400')}>
            ₹{price.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </div>
          <div className="flex gap-2 text-[10px] mt-0.5">
            <span className="text-gray-500">O:{tick.open?.toFixed(1) ?? '—'}</span>
            <span className="text-gray-500">H:{tick.high?.toFixed(1) ?? '—'}</span>
            <span className="text-gray-500">L:{tick.low?.toFixed(1) ?? '—'}</span>
          </div>
          {change !== null && (
            <div className={clsx('text-[10px] font-mono mt-0.5', up ? 'text-emerald-400' : 'text-red-400')}>
              {up ? '▲' : '▼'} {Math.abs(change).toFixed(2)}%
            </div>
          )}
        </>
      ) : (
        <div className="text-xs text-gray-600">Awaiting tick…</div>
      )}
    </div>
  )
}

function OptionsTable({ underlying, spot }: { underlying: string; spot: number | null }) {
  const step   = underlying === 'BANKNIFTY' ? 100 : underlying === 'NIFTY' ? 50
               : underlying.endsWith('NIFTY') ? 50
               : LOT_SIZE[underlying] ? 10 : 50
  const base   = spot ?? BASE_PRICE[underlying] ?? 1000
  const atm    = Math.round(base / step) * step
  const strikes = Array.from({ length: 9 }, (_, i) => atm + (i - 4) * step)
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[10px] font-mono">
        <thead>
          <tr className="text-gray-500 border-b border-surface-border">
            <th className="text-right py-1 pr-2">CE</th>
            <th className="text-right py-1 pr-2">CE Δ</th>
            <th className="text-center py-1 px-2 font-bold text-gray-300">STRIKE</th>
            <th className="text-left py-1 pl-2">PE Δ</th>
            <th className="text-left py-1 pl-2">PE</th>
          </tr>
        </thead>
        <tbody>
          {strikes.map(strike => {
            const isAtm = Math.abs(strike - atm) < step / 2
            return (
              <tr key={strike} className={clsx(
                'border-b border-surface-border/40',
                isAtm ? 'bg-amber-950/30' : '',
              )}>
                <td className="text-right py-0.5 pr-2 text-emerald-400">—</td>
                <td className="text-right py-0.5 pr-2 text-gray-500">—</td>
                <td className={clsx('text-center py-0.5 px-2 font-bold', isAtm ? 'text-amber-400' : 'text-gray-300')}>
                  {strike.toLocaleString('en-IN')}{isAtm ? ' ★' : ''}
                </td>
                <td className="text-left py-0.5 pl-2 text-gray-500">—</td>
                <td className="text-left py-0.5 pl-2 text-red-400">—</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <p className="text-[9px] text-gray-600 mt-1">
        Live premiums stream after 9:15 AM IST. Lot size: {LOT_SIZE[underlying] ?? '—'}.
      </p>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────
export function Engine() {
  useStore()  // keep store subscription for future state
  const [activeTab, setActiveTab] = useState('overview')
  const [agent, setAgent]         = useState<any>(null)
  const [ticks, setTicks]         = useState<Record<string, any>>({})
  const [positions, setPositions] = useState<any>(null)
  const [trades, setTrades]       = useState<any[]>([])
  const [rejections, setRejections] = useState<any[]>([])
  const [governance, setGovernance] = useState<any>(null)
  const [exitPlans, setExitPlans]   = useState<any[]>([])
  const [oms, setOms]               = useState<any[]>([])
  const [scheduler, setScheduler]   = useState<any>(null)
  const [loading, setLoading]       = useState(false)
  const [intervalSec, setIntervalSec] = useState(300)
  const tickRef = useRef<Record<string, any>>({})

  const fetchAll = useCallback(async () => {
    const [ag, sched] = await Promise.allSettled([
      getAgentStatus(),
      getSchedulerStats(),
    ])
    if (ag.status === 'fulfilled') setAgent(ag.value)
    if (sched.status === 'fulfilled') setScheduler((sched.value as any))

    // Fetch ticks for all underlyings
    const tickResults = await Promise.allSettled(
      ALL_UNDERLYINGS.map(sym => getLatestTick(sym).then(t => ({ sym, t })))
    )
    const newTicks: Record<string, any> = {}
    for (const r of tickResults) {
      if (r.status === 'fulfilled' && (r.value as any).t?.available) {
        newTicks[(r.value as any).sym] = (r.value as any).t
      }
    }
    tickRef.current = { ...tickRef.current, ...newTicks }
    setTicks({ ...tickRef.current })
  }, [])

  const fetchTabData = useCallback(async (tab: string) => {
    try {
      if (tab === 'positions') {
        const d = await getPortfolioPositions()
        setPositions(d)
      } else if (tab === 'trades') {
        const d = await getAgentTrades(200)
        setTrades(((d as any).trades) ?? [])
      } else if (tab === 'risk') {
        const d = await getRiskRejections(200)
        setRejections(((d as any).rejections) ?? [])
      } else if (tab === 'governance') {
        const d = await getGovernanceDashboard()
        setGovernance(d)
      } else if (tab === 'activity') {
        const [ep, omsD] = await Promise.allSettled([
          getActiveExitPlans(),
          getOmsEvents(50),
        ])
        if (ep.status === 'fulfilled') setExitPlans(((ep.value as any).plans) ?? [])
        if (omsD.status === 'fulfilled') setOms(((omsD.value as any).events) ?? [])
      }
    } catch (_) { /* silent */ }
  }, [])

  useEffect(() => {
    fetchAll()
    const iv = setInterval(fetchAll, 3000)
    return () => clearInterval(iv)
  }, [fetchAll])

  useEffect(() => {
    fetchTabData(activeTab)
    const iv = setInterval(() => fetchTabData(activeTab), 5000)
    return () => clearInterval(iv)
  }, [activeTab, fetchTabData])

  const handleStart = async () => {
    setLoading(true)
    await startAgent(intervalSec)
    await fetchAll()
    setLoading(false)
  }
  const handleStop = async () => {
    setLoading(true)
    await stopAgent()
    await fetchAll()
    setLoading(false)
  }
  const handleInterval = async (s: number) => {
    setIntervalSec(s)
    if (agent?.running) await setAgentInterval(s)
  }

  const isRunning  = agent?.running ?? false
  const lastCycle  = agent?.last_cycle ?? null
  const actLog     = agent?.activity_log ?? []
  const mktStatus  = agent?.market_status ?? '—'
  const istTime    = agent?.ist_time ?? '—'
  const scanCount  = agent?.scan_count ?? 0
  const totalEnq   = agent?.enqueued_total ?? 0

  return (
    <div className="space-y-4 p-4">
      {/* ── Header bar ──────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-3">
          <Bot size={22} className="text-indigo-400" />
          <div>
            <h1 className="text-lg font-bold text-gray-100">Autonomous Trading Engine</h1>
            <p className="text-[11px] text-gray-500">
              {ALL_UNDERLYINGS.length} instruments · {FO_UNDERLYINGS.length} F&O underlyings · Paper mode
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <span className={clsx(
            'px-2 py-0.5 rounded text-[10px] font-semibold',
            mktStatus === 'OPEN' ? 'bg-emerald-900/60 text-emerald-400' : 'bg-gray-800 text-gray-400',
          )}>
            {mktStatus} · {istTime} IST
          </span>
          <div className="flex gap-1">
            {INTERVALS.map(iv => (
              <button key={iv.s}
                onClick={() => handleInterval(iv.s)}
                className={clsx('px-2 py-0.5 rounded text-[10px] font-mono border',
                  intervalSec === iv.s
                    ? 'bg-indigo-700 border-indigo-500 text-white'
                    : 'border-surface-border text-gray-400 hover:text-gray-200')}
              >{iv.label}</button>
            ))}
          </div>
          {isRunning ? (
            <button onClick={handleStop} disabled={loading}
              className="flex items-center gap-1 px-3 py-1 rounded bg-red-700 hover:bg-red-600 text-white text-xs font-semibold disabled:opacity-50">
              <Square size={12} /> Stop Agent
            </button>
          ) : (
            <button onClick={handleStart} disabled={loading}
              className="flex items-center gap-1 px-3 py-1 rounded bg-emerald-700 hover:bg-emerald-600 text-white text-xs font-semibold disabled:opacity-50">
              <Play size={12} /> Start Agent
            </button>
          )}
        </div>
      </div>

      {/* ── Pipeline status strip ────────────────────────────────────────── */}
      <div className="flex items-center gap-1 text-[10px] font-mono overflow-x-auto pb-1">
        {[
          { label: 'Live Feed',  ok: Object.keys(ticks).length > 0 },
          { label: 'Features',   ok: scanCount > 0 },
          { label: 'Regime',     ok: scanCount > 0 },
          { label: 'Strategy',   ok: scanCount > 0 },
          { label: 'Risk Gate',  ok: scanCount > 0 },
          { label: 'Execution',  ok: totalEnq > 0 },
          { label: 'Exit Mgr',   ok: isRunning },
          { label: 'ML Feedback',ok: (governance as any)?.ml_models?.feature_store_symbols?.length > 0 },
        ].map((step, i, arr) => (
          <span key={step.label} className="flex items-center gap-1 shrink-0">
            <span className={clsx(
              'px-1.5 py-0.5 rounded',
              step.ok ? 'bg-emerald-900/50 text-emerald-400' : 'bg-gray-800 text-gray-500',
            )}>{step.label}</span>
            {i < arr.length - 1 && <span className="text-gray-700">→</span>}
          </span>
        ))}
      </div>

      {/* ── Tabs ────────────────────────────────────────────────────────── */}
      <div className="flex gap-1 border-b border-surface-border overflow-x-auto pb-0">
        {TABS.map(tab => (
          <button key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={clsx(
              'flex items-center gap-1 px-3 py-1.5 text-xs font-medium whitespace-nowrap border-b-2 -mb-px transition-colors',
              activeTab === tab.id
                ? 'border-indigo-500 text-indigo-300'
                : 'border-transparent text-gray-500 hover:text-gray-300',
            )}>
            {tab.icon} {tab.label}
          </button>
        ))}
      </div>

      {/* ════════════════════════════════════════════════════════════════
          TAB: OVERVIEW
      ═══════════════════════════════════════════════════════════════ */}
      {activeTab === 'overview' && (
        <div className="space-y-4">
          {/* KPI row */}
          <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-2">
            <StatBox label="Status"   value={isRunning ? 'RUNNING' : 'STOPPED'} color={isRunning ? 'text-emerald-400' : 'text-red-400'} />
            <StatBox label="Scans"    value={scanCount} />
            <StatBox label="Enqueued" value={totalEnq} />
            <StatBox label="Universe" value={ALL_UNDERLYINGS.length} sub="underlyings" />
            <StatBox label="Live Ticks" value={Object.keys(ticks).length} sub={`/ ${ALL_UNDERLYINGS.length}`} color="text-emerald-400" />
            <StatBox label="Exit Plans" value={exitPlans.length} />
            <StatBox label="Rejections" value={rejections.length} sub="session" color="text-amber-400" />
            <StatBox label="Interval" value={`${intervalSec}s`} />
          </div>

          {/* Last cycle */}
          {lastCycle && (
            <Card>
              <CardHeader title={`Last Scan Cycle — ${lastCycle.ts}`} />
              <CardBody>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
                  <StatBox label="Market"     value={lastCycle.market_status} />
                  <StatBox label="Candidates" value={lastCycle.total_candidates} />
                  <StatBox label="Approved"   value={lastCycle.approved} color="text-emerald-400" />
                  <StatBox label="Rejected"   value={lastCycle.rejected}  color="text-red-400" />
                  <StatBox label="Enqueued"   value={lastCycle.enqueued}  color="text-indigo-400" />
                  <StatBox label="Skipped"    value={lastCycle.skipped_existing} />
                  <StatBox label="Duration"   value={`${lastCycle.duration_ms}ms`} />
                  <StatBox label="Errors"     value={lastCycle.errors?.length ?? 0} color={lastCycle.errors?.length ? 'text-red-400' : 'text-gray-400'} />
                </div>
                {/* Regimes */}
                {Object.keys(lastCycle.regimes ?? {}).length > 0 && (
                  <div className="mt-3">
                    <div className="text-[10px] text-gray-500 mb-1">Regimes per underlying</div>
                    <div className="flex flex-wrap gap-1">
                      {Object.entries(lastCycle.regimes).map(([sym, regime]) => (
                        <span key={sym} className="px-1.5 py-0.5 rounded text-[10px] bg-gray-800 font-mono">
                          <span className="text-gray-400">{sym}</span>
                          <span className="text-gray-600 mx-1">→</span>
                          <span className={clsx(
                            (regime as string) === 'TRENDING' ? 'text-emerald-400' :
                            (regime as string) === 'HIGH_VOLATILITY' ? 'text-red-400' :
                            (regime as string) === 'BREAKOUT' ? 'text-amber-400' : 'text-blue-400'
                          )}>{regime as string}</span>
                        </span>
                      ))}
                    </div>
                  </div>
                )}
                {/* Signals enqueued this cycle */}
                {(lastCycle.signals?.length ?? 0) > 0 && (
                  <div className="mt-3 space-y-1">
                    <div className="text-[10px] text-gray-500 mb-1">Signals enqueued this cycle</div>
                    {lastCycle.signals.map((sig: any, i: number) => (
                      <div key={i} className="flex items-center gap-2 text-[11px] font-mono bg-gray-800/50 rounded px-2 py-1">
                        <span className={clsx('font-bold', sig.side === 'BUY' ? 'text-emerald-400' : 'text-red-400')}>{sig.side}</span>
                        <span className="text-gray-200">{sig.symbol}</span>
                        <span className="text-gray-500">{sig.strategy}</span>
                        <span className="text-indigo-300">conf:{(sig.confidence * 100).toFixed(0)}%</span>
                        {sig.price > 0 && <span className="text-gray-400">₹{sig.price.toFixed(2)}</span>}
                      </div>
                    ))}
                  </div>
                )}
                {/* Errors */}
                {(lastCycle.errors?.length ?? 0) > 0 && (
                  <div className="mt-2 space-y-0.5">
                    {lastCycle.errors.map((e: string, i: number) => (
                      <div key={i} className="text-[10px] text-red-400 font-mono">{e}</div>
                    ))}
                  </div>
                )}
              </CardBody>
            </Card>
          )}

          {/* Activity log */}
          <Card>
            <CardHeader title="Agent Activity Log (last 20)" />
            <CardBody>
              <div className="space-y-0.5 max-h-56 overflow-y-auto font-mono text-[11px]">
                {actLog.slice(-20).reverse().map((entry: any, i: number) => (
                  <div key={i} className="flex gap-2">
                    <span className="text-gray-600 shrink-0">{entry.ts}</span>
                    <span className={entry.level === 'error' ? 'text-red-400' : 'text-gray-300'}>{entry.msg}</span>
                  </div>
                ))}
                {actLog.length === 0 && <span className="text-gray-600">No activity yet</span>}
              </div>
            </CardBody>
          </Card>
        </div>
      )}

      {/* ════════════════════════════════════════════════════════════════
          TAB: POSITIONS
      ═══════════════════════════════════════════════════════════════ */}
      {activeTab === 'positions' && (
        <div className="space-y-4">
          {/* Portfolio summary */}
          {positions?.portfolio && (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              <StatBox label="Equity"       value={inr(positions.portfolio.equity)} />
              <StatBox label="Drawdown"     value={`${positions.portfolio.drawdown_pct.toFixed(2)}%`}
                color={positions.portfolio.drawdown_pct > 5 ? 'text-red-400' : 'text-gray-200'} />
              <StatBox label="Unrealized P&L" value={inr(positions.portfolio.unrealized_pnl)}
                color={positions.portfolio.unrealized_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'} />
              <StatBox label="Realized P&L" value={inr(positions.portfolio.realized_pnl)}
                color={positions.portfolio.realized_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'} />
            </div>
          )}
          {/* Positions table */}
          <Card>
            <CardHeader title={`Open Positions (${positions?.count ?? 0})`} />
            <CardBody>
              {(positions?.positions?.length ?? 0) === 0 ? (
                <div className="text-center text-gray-600 py-8 text-sm">No open positions</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-gray-500 border-b border-surface-border text-left">
                        <th className="py-1 pr-3">Symbol</th>
                        <th className="py-1 pr-3">Side</th>
                        <th className="py-1 pr-3 text-right">Qty</th>
                        <th className="py-1 pr-3 text-right">Avg Price</th>
                        <th className="py-1 pr-3 text-right">Mark</th>
                        <th className="py-1 pr-3 text-right">Unreal P&L</th>
                        <th className="py-1 pr-3 text-right">P&L %</th>
                        <th className="py-1">Live</th>
                      </tr>
                    </thead>
                    <tbody>
                      {positions.positions.map((pos: any) => (
                        <tr key={pos.symbol} className="border-b border-surface-border/30 hover:bg-gray-800/30">
                          <td className="py-1 pr-3 font-mono font-semibold text-gray-200">{pos.symbol}</td>
                          <td className="py-1 pr-3">
                            <span className={clsx('font-semibold', pos.side === 'BUY' ? 'text-emerald-400' : 'text-red-400')}>
                              {pos.side}
                            </span>
                          </td>
                          <td className="py-1 pr-3 text-right font-mono">{pos.quantity}</td>
                          <td className="py-1 pr-3 text-right font-mono">₹{pos.average_price.toFixed(2)}</td>
                          <td className="py-1 pr-3 text-right font-mono">₹{pos.mark_price.toFixed(2)}</td>
                          <td className={clsx('py-1 pr-3 text-right font-mono', pos.unrealized_pnl >= 0 ? 'text-emerald-400' : 'text-red-400')}>
                            {pos.unrealized_pnl >= 0 ? '+' : ''}₹{pos.unrealized_pnl.toFixed(2)}
                          </td>
                          <td className="py-1 pr-3 text-right"><PnlBadge value={pos.pnl_pct} /></td>
                          <td className="py-1"><LiveDot live={pos.live} /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </CardBody>
          </Card>
          {/* Active exit plans */}
          <Card>
            <CardHeader title={`Active Exit Plans (${exitPlans.length}) — SL / Target / Trailing`} />
            <CardBody>
              {exitPlans.length === 0 ? (
                <div className="text-center text-gray-600 py-6 text-sm">No active exit plans</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-gray-500 border-b border-surface-border text-left">
                        <th className="py-1 pr-3">Symbol</th>
                        <th className="py-1 pr-3">Side</th>
                        <th className="py-1 pr-3 text-right">Entry</th>
                        <th className="py-1 pr-3 text-right">Stop Loss</th>
                        <th className="py-1 pr-3 text-right">Target</th>
                        <th className="py-1 pr-3 text-right">Trailing</th>
                        <th className="py-1">Strategy</th>
                      </tr>
                    </thead>
                    <tbody>
                      {exitPlans.map((plan: any, i: number) => (
                        <tr key={plan.plan_id ?? i} className="border-b border-surface-border/30">
                          <td className="py-1 pr-3 font-mono font-semibold text-gray-200">{plan.symbol}</td>
                          <td className="py-1 pr-3">
                            <span className={clsx('font-semibold', plan.side === 'BUY' ? 'text-emerald-400' : 'text-red-400')}>{plan.side}</span>
                          </td>
                          <td className="py-1 pr-3 text-right font-mono">₹{plan.entry_price?.toFixed(2) ?? '—'}</td>
                          <td className="py-1 pr-3 text-right font-mono text-red-400">₹{plan.stop_loss?.toFixed(2) ?? '—'}</td>
                          <td className="py-1 pr-3 text-right font-mono text-emerald-400">₹{plan.target?.toFixed(2) ?? '—'}</td>
                          <td className="py-1 pr-3 text-right font-mono text-amber-400">₹{plan.trailing_stop?.toFixed(2) ?? '—'}</td>
                          <td className="py-1 text-gray-400 text-[10px]">{plan.strategy_name ?? '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </CardBody>
          </Card>
        </div>
      )}

      {/* ════════════════════════════════════════════════════════════════
          TAB: TRADE LOG
      ═══════════════════════════════════════════════════════════════ */}
      {activeTab === 'trades' && (
        <Card>
          <CardHeader title={`Agent Trade Log (${trades.length} fills)`} />
          <CardBody>
            {trades.length === 0 ? (
              <div className="text-center text-gray-600 py-8 text-sm">
                No trades yet — agent enqueues trades during market hours (9:15–15:20 IST)
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-gray-500 border-b border-surface-border text-left">
                      <th className="py-1 pr-3">Time</th>
                      <th className="py-1 pr-3">Type</th>
                      <th className="py-1 pr-3">Symbol</th>
                      <th className="py-1 pr-3">Side</th>
                      <th className="py-1 pr-3">Strategy</th>
                      <th className="py-1 pr-3">Regime</th>
                      <th className="py-1 pr-3 text-right">Entry ₹</th>
                      <th className="py-1 pr-3 text-right">Exit ₹</th>
                      <th className="py-1 pr-3 text-right">P&L %</th>
                      <th className="py-1 text-right">ML Score</th>
                    </tr>
                  </thead>
                  <tbody>
                    {trades.map((t: any, i: number) => (
                      <tr key={i} className="border-b border-surface-border/30 hover:bg-gray-800/20">
                        <td className="py-1 pr-3 font-mono text-[10px] text-gray-500">{t.ts?.substring(0, 19)}</td>
                        <td className="py-1 pr-3">
                          <span className={clsx('px-1 py-0.5 rounded text-[9px] font-bold',
                            t.type === 'ENTRY' ? 'bg-emerald-900/50 text-emerald-400' : 'bg-blue-900/50 text-blue-400')}>
                            {t.type}
                          </span>
                        </td>
                        <td className="py-1 pr-3 font-mono font-semibold text-gray-200">{t.symbol}</td>
                        <td className="py-1 pr-3">
                          <span className={clsx('font-bold', t.side === 'BUY' ? 'text-emerald-400' : 'text-red-400')}>{t.side}</span>
                        </td>
                        <td className="py-1 pr-3 text-gray-400 text-[10px]">{t.strategy ?? '—'}</td>
                        <td className="py-1 pr-3 text-[10px]">
                          {t.regime ? (
                            <span className={clsx(
                              t.regime === 'TRENDING' ? 'text-emerald-400' :
                              t.regime === 'HIGH_VOLATILITY' ? 'text-red-400' :
                              t.regime === 'BREAKOUT' ? 'text-amber-400' : 'text-blue-400')}>
                              {t.regime}
                            </span>
                          ) : '—'}
                        </td>
                        <td className="py-1 pr-3 text-right font-mono">
                          {t.entry_price != null ? `₹${t.entry_price.toFixed(2)}` : '—'}
                        </td>
                        <td className="py-1 pr-3 text-right font-mono">
                          {t.exit_price != null ? `₹${t.exit_price.toFixed(2)}` : '—'}
                        </td>
                        <td className="py-1 pr-3 text-right">
                          {t.pnl_pct != null ? <PnlBadge value={t.pnl_pct} /> : '—'}
                        </td>
                        <td className="py-1 text-right font-mono text-[10px] text-indigo-300">
                          {t.ml_score != null ? t.ml_score.toFixed(3) : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </CardBody>
        </Card>
      )}

      {/* ════════════════════════════════════════════════════════════════
          TAB: GOVERNANCE
      ═══════════════════════════════════════════════════════════════ */}
      {activeTab === 'governance' && (
        <div className="space-y-4">
          {governance ? (
            <>
              {/* Goal */}
              <Card>
                <CardHeader title="Annual Goal Governance" />
                <CardBody>
                  <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-3">
                    <StatBox label="Phase"      value={governance.goal?.phase ?? '—'}
                      color={governance.goal?.phase === 'ON_TRACK' ? 'text-emerald-400' :
                             governance.goal?.phase === 'LAGGING' ? 'text-amber-400' :
                             governance.goal?.phase === 'HALTED' ? 'text-red-400' : 'text-gray-200'} />
                    <StatBox label="Annual Target" value={`${governance.goal?.annual_target_pct ?? 0}%`} />
                    <StatBox label="Actual Run Rate" value={`${governance.goal?.actual_run_rate?.toFixed(2) ?? 0}%`} />
                    <StatBox label="Req. Run Rate"  value={`${governance.goal?.required_run_rate?.toFixed(2) ?? 0}%`} />
                    <StatBox label="Scaling Factor" value={governance.goal?.scaling_factor ?? '—'}
                      color={governance.goal?.scaling_factor < 1 ? 'text-amber-400' : 'text-gray-200'} />
                    <StatBox label="Gap to Target"  value={`${governance.goal?.gap_pct?.toFixed(2) ?? 0}%`} />
                    <StatBox label="Days Elapsed"   value={governance.goal?.days_elapsed ?? '—'} />
                    <StatBox label="Days Remaining" value={governance.goal?.days_remaining ?? '—'} />
                  </div>
                  {/* Progress bar */}
                  <div className="mb-2">
                    <div className="flex justify-between text-[10px] text-gray-500 mb-1">
                      <span>₹{governance.goal?.current_equity?.toFixed(0)}</span>
                      <span>Target ₹{governance.goal?.target_equity?.toFixed(0)}</span>
                    </div>
                    <div className="h-2 bg-gray-800 rounded overflow-hidden">
                      <div className="h-full bg-emerald-600 rounded"
                        style={{ width: `${Math.min(100, Math.max(0, (governance.goal?.current_equity / governance.goal?.target_equity) * 100))}%` }} />
                    </div>
                  </div>
                  <p className="text-xs text-gray-400">{governance.goal?.message}</p>
                </CardBody>
              </Card>
              {/* ML Models */}
              <Card>
                <CardHeader title="ML Model Health" />
                <CardBody>
                  <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 mb-3">
                    <StatBox label="Regime Classifier" value={governance.ml_models?.regime_classifier_trained ? 'TRAINED' : 'RULE-BASED'}
                      color={governance.ml_models?.regime_classifier_trained ? 'text-emerald-400' : 'text-amber-400'} />
                    <StatBox label="Feature Store Symbols" value={governance.ml_models?.feature_store_symbols?.length ?? 0} />
                    <StatBox label="Risk Rejections (session)" value={governance.risk?.rejection_count_session ?? 0}
                      color="text-amber-400" />
                  </div>
                  {/* Feature drift */}
                  {Object.keys(governance.ml_models?.feature_drift_scores ?? {}).length > 0 && (
                    <div className="mb-3">
                      <div className="text-[10px] text-gray-500 mb-1">Feature Drift Scores ({'>'} 0.25 = retrain needed)</div>
                      <div className="flex flex-wrap gap-1">
                        {Object.entries(governance.ml_models.feature_drift_scores).map(([sym, score]) => (
                          <span key={sym} className={clsx(
                            'px-1.5 py-0.5 rounded text-[10px] font-mono',
                            (score as number) > 0.25 ? 'bg-red-900/50 text-red-400' : 'bg-gray-800 text-gray-400',
                          )}>
                            {sym}: {(score as number).toFixed(3)}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                  {/* MetaModel summary */}
                  {Object.keys(governance.ml_models?.meta_model_summary ?? {}).length > 0 && (
                    <div>
                      <div className="text-[10px] text-gray-500 mb-1">MetaModel Strategy Scores by Regime</div>
                      {Object.entries(governance.ml_models.meta_model_summary).map(([regime, strategies]) => (
                        <div key={regime} className="mb-2">
                          <div className="text-[10px] font-bold text-gray-300 mb-0.5">{regime}</div>
                          <div className="flex flex-wrap gap-1">
                            {Object.entries(strategies as Record<string, number>)
                              .sort((a, b) => b[1] - a[1])
                              .map(([strat, score]) => (
                                <span key={strat} className="px-1.5 py-0.5 rounded text-[10px] font-mono bg-indigo-900/30 text-indigo-300">
                                  {strat}: {score.toFixed(3)}
                                </span>
                              ))}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                  {/* Risk controls */}
                  <div className="mt-3 flex gap-2 flex-wrap">
                    <span className={clsx('px-2 py-0.5 rounded text-[10px]',
                      governance.risk?.kill_switch ? 'bg-red-900 text-red-300' : 'bg-gray-800 text-gray-400')}>
                      Kill Switch: {governance.risk?.kill_switch ? 'ACTIVE' : 'OFF'}
                    </span>
                    <span className={clsx('px-2 py-0.5 rounded text-[10px]',
                      governance.risk?.live_armed ? 'bg-amber-900 text-amber-300' : 'bg-gray-800 text-gray-400')}>
                      Live Armed: {governance.risk?.live_armed ? 'YES' : 'NO'}
                    </span>
                    <span className="px-2 py-0.5 rounded text-[10px] bg-gray-800 text-gray-400">
                      Orders today: {governance.risk?.compliance_orders_today ?? 0}/{governance.risk?.compliance_max_orders ?? 200}
                    </span>
                  </div>
                </CardBody>
              </Card>
            </>
          ) : (
            <div className="text-center text-gray-600 py-8">Loading governance data…</div>
          )}
        </div>
      )}

      {/* ════════════════════════════════════════════════════════════════
          TAB: LIVE PRICES
      ═══════════════════════════════════════════════════════════════ */}
      {activeTab === 'prices' && (
        <div className="space-y-4">
          <div>
            <div className="text-xs font-semibold text-gray-400 mb-2">NSE Indices</div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              {NSE_INDICES.map(sym => <TickBox key={sym} symbol={sym} tick={ticks[sym]} />)}
            </div>
          </div>
          <div>
            <div className="text-xs font-semibold text-gray-400 mb-2">BSE Indices</div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              {BSE_INDICES.map(sym => <TickBox key={sym} symbol={sym} tick={ticks[sym]} />)}
            </div>
          </div>
          <div>
            <div className="text-xs font-semibold text-gray-400 mb-2">Core Equities (F&O)</div>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
              {CORE_EQUITIES.map(sym => <TickBox key={sym} symbol={sym} tick={ticks[sym]} />)}
            </div>
          </div>
          <div>
            <div className="text-xs font-semibold text-gray-400 mb-2">Extended Nifty 50 Equities</div>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
              {EXT_EQUITIES.map(sym => <TickBox key={sym} symbol={sym} tick={ticks[sym]} />)}
            </div>
          </div>
          <p className="text-[10px] text-gray-600 text-center">
            Live ticks via Angel One SmartWebSocketV2. Prices stream at 9:15 AM IST.
            F&O tokens loaded at pre-market (9:00 AM) from Angel One instrument master.
          </p>
        </div>
      )}

      {/* ════════════════════════════════════════════════════════════════
          TAB: OPTIONS
      ═══════════════════════════════════════════════════════════════ */}
      {activeTab === 'options' && (
        <div className="space-y-4">
          <p className="text-[11px] text-amber-400 bg-amber-950/30 border border-amber-900/50 rounded px-3 py-2">
            Options premiums stream after 9:15 AM IST when Angel One WebSocket connects.
            Strikes shown are near-the-money ±4 steps around current spot. ATM row is highlighted ★.
            Real tokens loaded from Angel One instrument master at pre-market 9:00 AM.
          </p>
          {FO_UNDERLYINGS.map(sym => {
            const spot = ticks[sym]?.last_price ?? null
            return (
              <Card key={sym}>
                <CardHeader
                  title={`${sym} Options Chain`}
                  subtitle={spot ? `Spot ₹${spot.toFixed(2)} · Lot ${LOT_SIZE[sym] ?? '—'}` : `Lot ${LOT_SIZE[sym] ?? '—'}`}
                />
                <CardBody>
                  <OptionsTable underlying={sym} spot={spot} />
                </CardBody>
              </Card>
            )
          })}
        </div>
      )}

      {/* ════════════════════════════════════════════════════════════════
          TAB: RISK LOG
      ═══════════════════════════════════════════════════════════════ */}
      {activeTab === 'risk' && (
        <Card>
          <CardHeader title={`Risk Engine Rejection Log (${rejections.length})`} />
          <CardBody>
            {rejections.length === 0 ? (
              <div className="text-center text-gray-600 py-8 text-sm">
                No risk rejections this session — all scanned candidates were either approved or had no signal
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-gray-500 border-b border-surface-border text-left">
                      <th className="py-1 pr-3">Time</th>
                      <th className="py-1 pr-3">Symbol</th>
                      <th className="py-1 pr-3">Strategy</th>
                      <th className="py-1 pr-3">Regime</th>
                      <th className="py-1 pr-3">Rejection Reason</th>
                      <th className="py-1 text-right">Risk Score</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rejections.map((r: any, i: number) => (
                      <tr key={i} className="border-b border-surface-border/30 hover:bg-gray-800/20">
                        <td className="py-1 pr-3 font-mono text-[10px] text-gray-500">{r.ts?.substring(11, 19)}</td>
                        <td className="py-1 pr-3 font-mono font-semibold text-gray-200">{r.symbol || r.underlying}</td>
                        <td className="py-1 pr-3 text-[10px] text-gray-400">{r.strategy}</td>
                        <td className="py-1 pr-3 text-[10px]">
                          <span className={clsx(
                            r.regime === 'TRENDING' ? 'text-emerald-400' :
                            r.regime === 'HIGH_VOLATILITY' ? 'text-red-400' :
                            r.regime === 'BREAKOUT' ? 'text-amber-400' : 'text-blue-400')}>
                            {r.regime}
                          </span>
                        </td>
                        <td className="py-1 pr-3">
                          <span className="px-1.5 py-0.5 rounded text-[10px] bg-red-900/30 text-red-300 font-mono">
                            {r.reason}
                          </span>
                        </td>
                        <td className="py-1 text-right font-mono text-[10px] text-amber-400">
                          {(r.risk_score * 100).toFixed(0)}%
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </CardBody>
        </Card>
      )}

      {/* ════════════════════════════════════════════════════════════════
          TAB: ACTIVITY (OMS events + exit plans + raw log)
      ═══════════════════════════════════════════════════════════════ */}
      {activeTab === 'activity' && (
        <div className="space-y-4">
          {/* Scheduler */}
          {scheduler && (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              <StatBox label="Orders Queued"    value={scheduler.queue_depth ?? 0} />
              <StatBox label="Processed Today"  value={scheduler.processed_today ?? 0} />
              <StatBox label="Filled Today"     value={scheduler.filled_today ?? 0} />
              <StatBox label="Rejected Today"   value={scheduler.rejected_today ?? 0} color="text-amber-400" />
            </div>
          )}
          {/* OMS events */}
          <Card>
            <CardHeader title="OMS Event Stream (last 50)" />
            <CardBody>
              <div className="space-y-0.5 max-h-72 overflow-y-auto font-mono text-[10px]">
                {oms.length === 0 && <span className="text-gray-600">No OMS events yet</span>}
                {oms.map((ev: any, i: number) => (
                  <div key={i} className="flex gap-2 items-start border-b border-surface-border/20 py-0.5">
                    <span className="text-gray-600 shrink-0">{String(ev.timestamp ?? ev.ts ?? '').substring(11, 19)}</span>
                    <span className={clsx('shrink-0 px-1 py-0.5 rounded text-[9px] font-bold',
                      ev.event_type?.includes('fill') ? 'bg-emerald-900/50 text-emerald-400' :
                      ev.event_type?.includes('reject') ? 'bg-red-900/50 text-red-400' :
                      'bg-gray-800 text-gray-400')}>
                      {ev.event_type}
                    </span>
                    <span className="text-gray-300">{ev.symbol ?? ''}</span>
                    {ev.side && <span className={clsx('font-bold', ev.side === 'BUY' ? 'text-emerald-400' : 'text-red-400')}>{ev.side}</span>}
                    {ev.price && <span className="text-gray-400">₹{ev.price}</span>}
                    {ev.quantity && <span className="text-gray-500">×{ev.quantity}</span>}
                  </div>
                ))}
              </div>
            </CardBody>
          </Card>
          {/* Full activity log */}
          <Card>
            <CardHeader title={`Agent Activity Log (all ${actLog.length})`} />
            <CardBody>
              <div className="space-y-0.5 max-h-80 overflow-y-auto font-mono text-[11px]">
                {actLog.slice().reverse().map((entry: any, i: number) => (
                  <div key={i} className="flex gap-2 border-b border-surface-border/20 py-0.5">
                    <span className="text-gray-600 shrink-0">{entry.ts}</span>
                    <span className={entry.level === 'error' ? 'text-red-400' : 'text-gray-300'}>{entry.msg}</span>
                  </div>
                ))}
                {actLog.length === 0 && <span className="text-gray-600">No activity yet</span>}
              </div>
            </CardBody>
          </Card>
        </div>
      )}
    </div>
  )
}
