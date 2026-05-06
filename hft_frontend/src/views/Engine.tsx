import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import {
  Play, Square, Activity, Shield,
  Cpu, Bot, AlertTriangle,
  DollarSign, List, Eye, Layers,
} from 'lucide-react'
import { clsx } from 'clsx'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { useStore } from '../store'
import {
  getAgentStatus, startAgent, stopAgent, setAgentInterval,
  getBatchTicks, getSchedulerStats, getOmsEvents,
  getAgentTrades, getPortfolioPositions, getRiskRejections,
  getGovernanceDashboard, getActiveExitPlans, getOptionChain, getUniverse,
} from '../api'
import { inr, pct } from '../utils'

// ── Universe ──────────────────────────────────────────────────────────────────
const FALLBACK_NSE_INDICES   = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY']
const FALLBACK_BSE_INDICES   = ['SENSEX', 'BANKEX']
const FALLBACK_CORE_EQUITIES = ['RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 'SBIN']
const FALLBACK_EXT_EQUITIES  = ['WIPRO', 'KOTAKBANK', 'AXISBANK', 'MARUTI', 'SUNPHARMA',
                                'TATAMOTORS', 'BAJFINANCE', 'HINDUNILVR', 'BHARTIARTL', 'NTPC']
const FALLBACK_FO_UNDERLYINGS = [...FALLBACK_NSE_INDICES, ...FALLBACK_CORE_EQUITIES, ...FALLBACK_EXT_EQUITIES]
const FALLBACK_TICK_SYMBOLS = [
  ...FALLBACK_NSE_INDICES,
  ...FALLBACK_BSE_INDICES,
  ...FALLBACK_CORE_EQUITIES,
  ...FALLBACK_EXT_EQUITIES,
]
const MAX_VISIBLE_TICKS = 120

const FALLBACK_LOT_SIZE: Record<string, number> = {
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

type UniverseInstrument = {
  symbol: string
  exchange: string
  segment: string
  type: string
  underlying?: string | null
  expiry?: string | null
  strike?: number | null
  option_type?: string | null
  lot_size?: number
}

type OptionLeg = UniverseInstrument & {
  ltp?: number | null
  delta?: number | null
  live?: boolean
}

type OptionChainPayload = {
  underlying: string
  expiry: string
  dte?: number
  spot_price?: number
  rows?: Array<{ strike: number; call?: OptionLeg | null; put?: OptionLeg | null }>
  call_count?: number
  put_count?: number
}

function uniq(values: string[]) {
  return [...new Set(values.map((value) => value.trim().toUpperCase()).filter(Boolean))]
}

function baseKey(symbol: string) {
  return symbol.replace(/-EQ$/, '')
}

const INTERVALS = [
  { label: '30s', s: 30 }, { label: '1m', s: 60 },
  { label: '5m',  s: 300 }, { label: '15m', s: 900 },
]

const TABS = [
  { id: 'overview', label: 'Overview',  icon: <Cpu size={13} /> },
  { id: 'monitor',  label: 'Monitor',   icon: <DollarSign size={13} />, hint: 'Positions & Trades' },
  { id: 'market',   label: 'Market',    icon: <Activity size={13} />,   hint: 'Live Prices & Options' },
  { id: 'debug',    label: 'Debug',     icon: <Shield size={13} />,     hint: 'Governance, Risk & Activity' },
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
  const refPrice = BASE_PRICE[symbol] ?? BASE_PRICE[baseKey(symbol)]
  const change = price && refPrice ? ((price - refPrice) / refPrice * 100) : null
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

function legPrice(leg?: OptionLeg | null) {
  if (leg?.ltp == null) return '—'
  return `₹${leg.ltp.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function legDelta(leg?: OptionLeg | null) {
  if (leg?.delta == null) return '—'
  return leg.delta.toFixed(3)
}

function OptionsTable({ chain }: { chain?: OptionChainPayload }) {
  const rows = chain?.rows ?? []
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
          {rows.map((row) => {
            const spot = chain?.spot_price ?? 0
            const step = rows.length > 1 ? Math.abs(rows[1].strike - rows[0].strike) || 1 : 1
            const isAtm = spot > 0 && Math.abs(row.strike - spot) <= step / 2
            return (
              <tr key={row.strike} className={clsx(
                'border-b border-surface-border/40',
                isAtm ? 'bg-amber-950/30' : '',
              )}>
                <td className={clsx('text-right py-0.5 pr-2', row.call?.live ? 'text-emerald-400' : 'text-gray-500')}>
                  {legPrice(row.call)}
                </td>
                <td className="text-right py-0.5 pr-2 text-gray-500">{legDelta(row.call)}</td>
                <td className={clsx('text-center py-0.5 px-2 font-bold', isAtm ? 'text-amber-400' : 'text-gray-300')}>
                  {row.strike.toLocaleString('en-IN')}{isAtm ? ' ★' : ''}
                </td>
                <td className="text-left py-0.5 pl-2 text-gray-500">{legDelta(row.put)}</td>
                <td className={clsx('text-left py-0.5 pl-2', row.put?.live ? 'text-red-400' : 'text-gray-500')}>
                  {legPrice(row.put)}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td colSpan={5} className="py-4 text-center text-gray-600">No option-chain rows available</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}

// ── Section divider for merged tabs ─────────────────────────────────────────
function SectionDivider({ title }: { title: string }) {
  return (
    <div className="flex items-center gap-3 pt-2">
      <div className="h-px flex-1 bg-surface-border" />
      <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-widest shrink-0">{title}</span>
      <div className="h-px flex-1 bg-surface-border" />
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────
export function Engine() {
  const livePortfolio = useStore((s) => s.livePortfolio)
  const [activeTab, setActiveTab] = useState('overview')
  const [universe, setUniverse] = useState<UniverseInstrument[]>([])
  const [priceFilter, setPriceFilter] = useState('')
  const [selectedOptionUnderlying, setSelectedOptionUnderlying] = useState('')
  const [optionChains, setOptionChains] = useState<Record<string, OptionChainPayload>>({})
  const [optionLoading, setOptionLoading] = useState(false)
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

  const cashInstruments = useMemo(
    () => universe.filter((instrument) => instrument.segment === 'CASH'),
    [universe],
  )
  const nseIndices = useMemo(
    () => cashInstruments.filter((instrument) => instrument.exchange === 'NSE' && instrument.type === 'INDEX').map((instrument) => instrument.symbol),
    [cashInstruments],
  )
  const bseIndices = useMemo(
    () => cashInstruments.filter((instrument) => instrument.exchange === 'BSE' && instrument.type === 'INDEX').map((instrument) => instrument.symbol),
    [cashInstruments],
  )
  const equitySymbols = useMemo(
    () => cashInstruments.filter((instrument) => instrument.type === 'EQUITY').map((instrument) => instrument.symbol),
    [cashInstruments],
  )
  const tickSymbols = useMemo(
    () => uniq(cashInstruments.length ? cashInstruments.map((instrument) => instrument.symbol) : FALLBACK_TICK_SYMBOLS),
    [cashInstruments],
  )
  const foUnderlyings = useMemo(
    () => {
      const derived = uniq(
        universe
          .filter((instrument) => instrument.segment === 'FUTURES' || instrument.segment === 'OPTIONS')
          .map((instrument) => instrument.underlying ?? ''),
      )
      return derived.length ? derived : FALLBACK_FO_UNDERLYINGS
    },
    [universe],
  )
  const lotSizeByUnderlying = useMemo(() => {
    const lots: Record<string, number> = { ...FALLBACK_LOT_SIZE }
    for (const instrument of universe) {
      if (instrument.underlying && instrument.lot_size) lots[instrument.underlying] = instrument.lot_size
    }
    return lots
  }, [universe])
  const selectedOption = selectedOptionUnderlying || foUnderlyings[0] || ''
  const activeOptionChain = selectedOption ? optionChains[selectedOption] : undefined
  const activeOptionSymbols = useMemo(() => {
    const rows = activeOptionChain?.rows ?? []
    return uniq(rows.flatMap((row) => [row.call?.symbol ?? '', row.put?.symbol ?? '']))
  }, [activeOptionChain])
  const tickRequestSymbols = useMemo(
    () => uniq([...tickSymbols, ...activeOptionSymbols]),
    [tickSymbols, activeOptionSymbols],
  )
  const visiblePriceSymbols = useMemo(() => {
    const filter = priceFilter.trim().toUpperCase()
    const filtered = filter
      ? tickSymbols.filter((symbol) => symbol.includes(filter) || baseKey(symbol).includes(filter))
      : tickSymbols
    return filtered.slice(0, MAX_VISIBLE_TICKS)
  }, [priceFilter, tickSymbols])
  const displayNseIndices = nseIndices.length ? nseIndices : FALLBACK_NSE_INDICES
  const displayBseIndices = bseIndices.length ? bseIndices : FALLBACK_BSE_INDICES

  const fetchUniverse = useCallback(async () => {
    try {
      const rows = await getUniverse()
      const parsed = rows.map((row) => ({
        symbol: String(row.symbol ?? '').toUpperCase(),
        exchange: String(row.exchange ?? ''),
        segment: String(row.segment ?? ''),
        type: String(row.type ?? ''),
        underlying: row.underlying ? String(row.underlying).toUpperCase() : null,
        expiry: row.expiry ? String(row.expiry) : null,
        strike: row.strike == null ? null : Number(row.strike),
        option_type: row.option_type ? String(row.option_type) : null,
        lot_size: row.lot_size == null ? undefined : Number(row.lot_size),
      })).filter((row) => row.symbol)
      setUniverse(parsed)
      const firstFo = parsed.find((row) => row.segment === 'OPTIONS' && row.underlying)?.underlying
      if (firstFo) setSelectedOptionUnderlying((current) => current || firstFo)
    } catch (_) {
      setUniverse([])
    }
  }, [])

  const fetchCore = useCallback(async () => {
    const [ag, sched] = await Promise.allSettled([getAgentStatus(), getSchedulerStats()])
    if (ag.status === 'fulfilled') setAgent(ag.value)
    if (sched.status === 'fulfilled') setScheduler(sched.value as any)
  }, [])

  const fetchAllSections = useCallback(async () => {
    const [posRes, tradeRes, rejRes, govRes, epRes, omsRes] = await Promise.allSettled([
      getPortfolioPositions(),
      getAgentTrades(200),
      getRiskRejections(200),
      getGovernanceDashboard(),
      getActiveExitPlans(),
      getOmsEvents(50),
    ])
    if (posRes.status === 'fulfilled') setPositions(posRes.value)
    if (tradeRes.status === 'fulfilled') setTrades(((tradeRes.value as any).trades) ?? [])
    if (rejRes.status === 'fulfilled') setRejections(((rejRes.value as any).rejections) ?? [])
    if (govRes.status === 'fulfilled') setGovernance(govRes.value)
    if (epRes.status === 'fulfilled') setExitPlans(((epRes.value as any).plans) ?? [])
    if (omsRes.status === 'fulfilled') setOms(((omsRes.value as any).events) ?? [])
  }, [])

  const fetchTicks = useCallback(async () => {
    try {
      if (tickRequestSymbols.length === 0) return
      const res = await getBatchTicks(tickRequestSymbols)
      const newTicks: Record<string, any> = {}
      for (const [sym, tick] of Object.entries(res.ticks ?? {})) {
        if ((tick as any)?.available) newTicks[sym] = tick
      }
      tickRef.current = { ...tickRef.current, ...newTicks }
      setTicks({ ...tickRef.current })
    } catch (_) { /* batch endpoint not yet available — silent */ }
  }, [tickRequestSymbols])

  const fetchOptionChain = useCallback(async () => {
    if (!selectedOption) return
    setOptionLoading(true)
    try {
      const spot = ticks[selectedOption]?.last_price ?? ticks[`${selectedOption}-EQ`]?.last_price
      const chain = await getOptionChain(selectedOption, undefined, spot) as OptionChainPayload
      setOptionChains((prev) => ({ ...prev, [selectedOption]: chain }))
    } finally {
      setOptionLoading(false)
    }
  }, [selectedOption, ticks])

  useEffect(() => {
    fetchUniverse()
  }, [fetchUniverse])

  useEffect(() => {
    fetchCore()
    fetchAllSections()
    fetchTicks()
    const coreIv     = setInterval(fetchCore, 5000)
    const sectionsIv = setInterval(fetchAllSections, 8000)
    const ticksIv    = setInterval(fetchTicks, 5000)
    return () => {
      clearInterval(coreIv)
      clearInterval(sectionsIv)
      clearInterval(ticksIv)
    }
  }, [fetchCore, fetchAllSections, fetchTicks])

  useEffect(() => {
    if (activeTab !== 'market') return
    fetchOptionChain()
    const chainIv = setInterval(fetchOptionChain, 8000)
    return () => clearInterval(chainIv)
  }, [activeTab, fetchOptionChain])

  const handleStart = async () => {
    setLoading(true)
    await startAgent(intervalSec)
    await fetchCore()
    setLoading(false)
  }
  const handleStop = async () => {
    setLoading(true)
    await stopAgent()
    await fetchCore()
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
              {(universe.length || tickSymbols.length).toLocaleString('en-IN')} instruments · {foUnderlyings.length.toLocaleString('en-IN')} F&O underlyings · Paper mode
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
          { label: 'Live Feed',   ok: Object.keys(ticks).length > 0 },
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
            title={tab.hint}
            className={clsx(
              'flex items-center gap-1 px-3 py-1.5 text-xs font-medium whitespace-nowrap border-b-2 -mb-px transition-colors',
              activeTab === tab.id
                ? 'border-indigo-500 text-indigo-300'
                : 'border-transparent text-gray-500 hover:text-gray-300',
            )}>
            {tab.icon} {tab.label}
          </button>
        ))}
        <div className="ml-auto flex items-center text-[10px] text-gray-600 pr-1 whitespace-nowrap">
          {TABS.find(t => t.id === activeTab)?.hint}
        </div>
      </div>

      {/* ════════════════════════════════════════════════════════════════
          TAB: OVERVIEW
      ═══════════════════════════════════════════════════════════════ */}
      {activeTab === 'overview' && (
        <div className="space-y-4">
          {/* Getting started guide — only shown before first scan */}
          {!isRunning && scanCount === 0 && (
            <div className="bg-indigo-950/30 border border-indigo-800/50 rounded-lg p-4">
              <h3 className="text-sm font-semibold text-indigo-300 mb-3 flex items-center gap-2">
                <Play size={13} /> Getting Started
              </h3>
              <ol className="space-y-2">
                {[
                  { label: 'Server running — API and database connected', done: true },
                  { label: 'Click Start Agent above to begin autonomous market scanning', done: isRunning },
                  { label: 'Agent scans all underlyings for regime + strategy signals', done: scanCount > 0 },
                  { label: 'Check Monitor tab — see open positions and trade log', done: totalEnq > 0 },
                  { label: 'Go to Dashboard — view live P&L, equity curve, and daily performance', done: false },
                ].map((step, i) => (
                  <li key={i} className="flex items-start gap-2 text-xs">
                    <span className={clsx(
                      'w-4 h-4 rounded-full flex items-center justify-center text-[9px] shrink-0 mt-0.5 font-bold',
                      step.done ? 'bg-emerald-700 text-emerald-100' : 'bg-gray-700 text-gray-400',
                    )}>
                      {step.done ? '✓' : i + 1}
                    </span>
                    <span className={step.done ? 'text-gray-300' : 'text-gray-500'}>{step.label}</span>
                  </li>
                ))}
              </ol>
              <p className="text-[10px] text-indigo-500/70 mt-3">
                Tip: Use the Signals view to manually run a one-off scan at any time without the agent.
              </p>
            </div>
          )}

          {/* KPI row */}
          <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-2">
            <StatBox label="Status"     value={isRunning ? 'RUNNING' : 'STOPPED'} color={isRunning ? 'text-emerald-400' : 'text-red-400'} />
            <StatBox label="Scans"      value={scanCount} />
            <StatBox label="Enqueued"   value={totalEnq} />
            <StatBox label="Universe"   value={(universe.length || tickSymbols.length).toLocaleString('en-IN')} sub="instruments" />
            <StatBox label="Live Ticks" value={Object.keys(ticks).length} sub={`/ ${tickSymbols.length.toLocaleString('en-IN')}`} color="text-emerald-400" />
            <StatBox label="Exit Plans" value={exitPlans.length} />
            <StatBox label="Rejections" value={rejections.length} sub="session" color="text-amber-400" />
            <StatBox label="Interval"   value={`${intervalSec}s`} />
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
                {actLog.length === 0 && <span className="text-gray-600">No activity yet — start the agent to see logs here</span>}
              </div>
            </CardBody>
          </Card>
        </div>
      )}

      {/* ════════════════════════════════════════════════════════════════
          TAB: MONITOR — Positions + Exit Plans + Trade Log
      ═══════════════════════════════════════════════════════════════ */}
      {activeTab === 'monitor' && (
        <div className="space-y-4">
          {/* Portfolio summary */}
          {(positions?.portfolio || livePortfolio?.portfolio) && (() => {
            const p = livePortfolio?.portfolio ?? positions?.portfolio
            const drawdownPct = livePortfolio ? p.drawdown : (p.drawdown_pct ?? p.drawdown ?? 0)
            return (
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                <StatBox label="Equity"         value={inr(p.equity)} />
                <StatBox label="Drawdown"       value={`${Number(drawdownPct).toFixed(2)}%`}
                  color={Number(drawdownPct) > 5 ? 'text-red-400' : 'text-gray-200'} />
                <StatBox label="Unrealized P&L" value={inr(p.unrealized_pnl)}
                  color={p.unrealized_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'} />
                <StatBox label="Realized P&L"   value={inr(p.realized_pnl)}
                  color={p.realized_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'} />
              </div>
            )
          })()}

          {/* Open Positions */}
          {(() => {
            const posList = livePortfolio?.positions ?? positions?.positions ?? []
            const count = livePortfolio?.count ?? positions?.count ?? 0
            return (
              <Card>
                <CardHeader title={`Open Positions (${count})`} />
                <CardBody>
                  {posList.length === 0 ? (
                    <div className="text-center text-gray-600 py-6 text-sm">
                      No open positions — positions appear here once the agent executes trades
                    </div>
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
                          {posList.map((pos: any) => (
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
            )
          })()}

          {/* Active exit plans */}
          <Card>
            <CardHeader title={`Active Exit Plans (${exitPlans.length}) — Stop Loss / Target / Trailing`} />
            <CardBody>
              {exitPlans.length === 0 ? (
                <div className="text-center text-gray-600 py-4 text-sm">No active exit plans</div>
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

          <SectionDivider title="Trade Log" />

          {/* Trade Log */}
          <Card>
            <CardHeader title={`Agent Trade Log (${trades.length} fills)`} />
            <CardBody>
              {trades.length === 0 ? (
                <div className="text-center text-gray-600 py-6 text-sm">
                  No trades yet — agent executes paper trades during market hours (9:15–15:20 IST)
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
        </div>
      )}

      {/* ════════════════════════════════════════════════════════════════
          TAB: MARKET — Live Prices + Options Chain
      ═══════════════════════════════════════════════════════════════ */}
      {activeTab === 'market' && (
        <div className="space-y-4">
          {/* Live prices */}
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="text-xs text-gray-500">
              {tickSymbols.length.toLocaleString('en-IN')} cash/index symbols · {Object.keys(ticks).length.toLocaleString('en-IN')} live ticks
            </div>
            <input
              value={priceFilter}
              onChange={(e) => setPriceFilter(e.target.value)}
              placeholder="Search symbol"
              className="w-56 max-w-full bg-surface-elevated border border-surface-border rounded-md px-3 py-1.5 text-xs text-gray-200 font-mono focus:outline-none focus:border-brand-blue"
            />
          </div>
          <div>
            <div className="text-xs font-semibold text-gray-400 mb-2">NSE Indices</div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              {displayNseIndices.map(sym => <TickBox key={sym} symbol={sym} tick={ticks[sym]} />)}
            </div>
          </div>
          <div>
            <div className="text-xs font-semibold text-gray-400 mb-2">BSE Indices</div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              {displayBseIndices.map(sym => <TickBox key={sym} symbol={sym} tick={ticks[sym]} />)}
            </div>
          </div>
          <div>
            <div className="text-xs font-semibold text-gray-400 mb-2">
              Equities {visiblePriceSymbols.length < equitySymbols.length ? `(${visiblePriceSymbols.length.toLocaleString('en-IN')} of ${equitySymbols.length.toLocaleString('en-IN')})` : ''}
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
              {visiblePriceSymbols
                .filter((sym) => !displayNseIndices.includes(sym) && !displayBseIndices.includes(sym))
                .map(sym => <TickBox key={sym} symbol={sym} tick={ticks[sym]} />)}
            </div>
          </div>
          <p className="text-[10px] text-gray-600 text-center">
            Live ticks via Angel One SmartWebSocketV2 · prices stream from 9:15 AM IST · F&O tokens loaded at pre-market (9:00 AM)
          </p>

          <SectionDivider title="Options Chain" />

          {/* Options chain */}
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <label className="block text-xs text-gray-500 mb-1">Underlying</label>
              <select
                value={selectedOption}
                onChange={(e) => setSelectedOptionUnderlying(e.target.value)}
                className="bg-surface-elevated border border-surface-border rounded-md px-3 py-1.5 text-sm text-gray-200 font-mono focus:outline-none focus:border-brand-blue"
              >
                {foUnderlyings.map((sym) => (
                  <option key={sym} value={sym}>{sym}</option>
                ))}
              </select>
            </div>
            <button
              onClick={fetchOptionChain}
              disabled={optionLoading || !selectedOption}
              className="px-3 py-1.5 rounded-md border border-surface-border bg-surface-elevated text-xs text-gray-300 hover:text-gray-100 disabled:opacity-50"
            >
              {optionLoading ? 'Loading…' : 'Refresh Chain'}
            </button>
          </div>
          <Card>
            <CardHeader
              title={`${selectedOption || '—'} Options Chain`}
              subtitle={[
                activeOptionChain?.expiry ? `Expiry ${activeOptionChain.expiry}` : null,
                activeOptionChain?.dte ? `${activeOptionChain.dte} DTE` : null,
                activeOptionChain?.spot_price ? `Spot ₹${activeOptionChain.spot_price.toFixed(2)}` : null,
                selectedOption ? `Lot ${lotSizeByUnderlying[selectedOption] ?? '—'}` : null,
              ].filter(Boolean).join(' · ')}
            />
            <CardBody>
              {optionLoading && !activeOptionChain ? (
                <div className="py-8 text-center text-sm text-gray-600">Loading option chain…</div>
              ) : (
                <OptionsTable chain={activeOptionChain} />
              )}
            </CardBody>
          </Card>
        </div>
      )}

      {/* ════════════════════════════════════════════════════════════════
          TAB: DEBUG — Governance + Risk Log + Activity
      ═══════════════════════════════════════════════════════════════ */}
      {activeTab === 'debug' && (
        <div className="space-y-4">
          {/* Governance */}
          {governance ? (
            <>
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
            <div className="text-center text-gray-600 py-6">Loading governance data…</div>
          )}

          <SectionDivider title="Risk Rejection Log" />

          {/* Risk rejections */}
          <Card>
            <CardHeader title={`Risk Engine Rejection Log (${rejections.length})`} />
            <CardBody>
              {rejections.length === 0 ? (
                <div className="text-center text-gray-600 py-6 text-sm">
                  No risk rejections this session — approved candidates were either executed or had no signal
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

          <SectionDivider title="System Activity" />

          {/* Scheduler stats */}
          {scheduler && (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              <StatBox label="Orders Queued" value={scheduler.queue_depth ?? 0} />
              <StatBox label="Processed"     value={scheduler.processed ?? 0} color="text-emerald-400" />
              <StatBox label="Rejected"      value={scheduler.rejected ?? 0} color="text-amber-400" />
              <StatBox label="Kill Switch"   value={scheduler.kill_switch_active ? 'ON' : 'OFF'}
                color={scheduler.kill_switch_active ? 'text-red-400' : 'text-gray-400'} />
            </div>
          )}

          {/* OMS events */}
          <Card>
            <CardHeader title="OMS Event Stream (last 50)" />
            <CardBody>
              <div className="space-y-0.5 max-h-60 overflow-y-auto font-mono text-[10px]">
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
              <div className="space-y-0.5 max-h-72 overflow-y-auto font-mono text-[11px]">
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
