import { useEffect, useState, useCallback } from 'react'
import {
  Activity, RefreshCw, TrendingUp, TrendingDown, Wifi, WifiOff,
  ToggleLeft, ToggleRight, CircleDot, Clock, Layers, IndianRupee,
  AlertTriangle, CheckCircle2, ArrowUpRight, ArrowDownRight,
} from 'lucide-react'
import { clsx } from 'clsx'
import { Card, CardBody } from '../components/shared/Card'
import { Table } from '../components/shared/Table'
import { fmtDateTime, inr, pct } from '../utils'
import {
  getPortfolioPositions, getRecentTrades, getFeedSnapshot,
  getOmsEvents, getSchedulerStats,
} from '../api'
import type { LivePosition, LivePortfolioMetrics, Trade } from '../types'

// ── helpers ───────────────────────────────────────────────────────────────────

function PnlCell({ value }: { value: number }) {
  const pos = value >= 0
  return (
    <span className={clsx('font-mono font-semibold', pos ? 'text-emerald-400' : 'text-red-400')}>
      {pos ? '+' : ''}{inr(value)}
    </span>
  )
}

function PctCell({ value }: { value: number }) {
  const pos = value >= 0
  return (
    <span className={clsx('font-mono text-xs', pos ? 'text-emerald-400' : 'text-red-400')}>
      {pos ? '+' : ''}{pct(value / 100)}
    </span>
  )
}

function SideBadge({ side }: { side: string }) {
  const isBuy = side.toUpperCase() === 'BUY'
  return (
    <span className={clsx(
      'inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-bold uppercase tracking-wide',
      isBuy ? 'bg-emerald-500/15 text-emerald-400' : 'bg-red-500/15 text-red-400'
    )}>
      {isBuy ? <ArrowUpRight size={10} /> : <ArrowDownRight size={10} />}
      {side.toUpperCase()}
    </span>
  )
}

function LiveDot({ live }: { live: boolean }) {
  return (
    <span className={clsx(
      'inline-flex items-center gap-1 text-xs',
      live ? 'text-emerald-400' : 'text-gray-500'
    )}>
      <CircleDot size={9} className={live ? 'animate-pulse' : ''} />
      {live ? 'Live' : 'Stale'}
    </span>
  )
}

// ── toggle pill ───────────────────────────────────────────────────────────────

function PaperSimBadge({ active, onToggle }: { active: boolean; onToggle: () => void }) {
  return (
    <button
      onClick={onToggle}
      className={clsx(
        'flex items-center gap-2 px-3 py-1.5 rounded-full border text-xs font-semibold transition-all select-none',
        active
          ? 'bg-violet-500/20 border-violet-500/50 text-violet-300 shadow-[0_0_12px_rgba(139,92,246,0.25)]'
          : 'bg-gray-800 border-gray-700 text-gray-500 hover:text-gray-300'
      )}
    >
      {active ? <ToggleRight size={14} className="text-violet-400" /> : <ToggleLeft size={14} />}
      PAPER SIM
    </button>
  )
}

// ── portfolio summary bar ─────────────────────────────────────────────────────

function PortfolioBar({ p }: { p: LivePortfolioMetrics }) {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
      {[
        { label: 'Equity', value: inr(p.equity), sub: null, color: 'text-white' },
        { label: 'Cash', value: inr(p.cash), sub: null, color: 'text-gray-300' },
        { label: 'Unrealised P&L', value: inr(p.unrealized_pnl), sub: null, color: p.unrealized_pnl >= 0 ? 'text-emerald-400' : 'text-red-400' },
        { label: 'Realised P&L', value: inr(p.realized_pnl), sub: null, color: p.realized_pnl >= 0 ? 'text-emerald-400' : 'text-red-400' },
        { label: 'Drawdown', value: pct(p.drawdown / 100), sub: null, color: p.drawdown > 5 ? 'text-red-400' : 'text-gray-300' },
        { label: 'Open Positions', value: String(p.open_positions), sub: null, color: 'text-blue-300' },
      ].map(m => (
        <div key={m.label} className="bg-[#161b22] border border-[#30363d] rounded-lg p-3">
          <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">{m.label}</p>
          <p className={clsx('text-sm font-bold font-mono', m.color)}>{m.value}</p>
        </div>
      ))}
    </div>
  )
}

// ── open positions table ──────────────────────────────────────────────────────

const POS_COLS = [
  { key: 'symbol',        label: 'Symbol' },
  { key: 'side',          label: 'Side' },
  { key: 'quantity',      label: 'Qty', className: 'text-right font-mono' },
  { key: 'average_price', label: 'Entry ₹', className: 'text-right font-mono' },
  { key: 'mark_price',    label: 'Live ₹', className: 'text-right font-mono' },
  { key: 'unrealized_pnl',label: 'Unreal P&L' },
  { key: 'pnl_pct',       label: '%' },
  { key: 'live',          label: 'Price Source' },
]

function PositionsTable({ positions }: { positions: LivePosition[] }) {
  if (positions.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-gray-600">
        <Layers size={32} className="mb-3 opacity-40" />
        <p className="text-sm">No open paper positions yet</p>
        <p className="text-xs mt-1">The agent will populate this once it generates trades</p>
      </div>
    )
  }

  const rows = positions.map(p => ({
    symbol:         <span className="font-mono text-blue-300 text-xs">{p.symbol}</span>,
    side:           <SideBadge side={p.side} />,
    quantity:       <span className="font-mono text-xs">{p.quantity}</span>,
    average_price:  <span className="font-mono text-xs">{inr(p.average_price)}</span>,
    mark_price:     <span className="font-mono text-xs font-semibold">{inr(p.mark_price)}</span>,
    unrealized_pnl: <PnlCell value={p.unrealized_pnl} />,
    pnl_pct:        <PctCell value={p.pnl_pct} />,
    live:           <LiveDot live={p.live} />,
  }))

  return <Table columns={POS_COLS} rows={rows} keyFn={(_, i) => String(i)} />
}

// ── recent fills table ────────────────────────────────────────────────────────

const FILL_COLS = [
  { key: 'ts',       label: 'Time' },
  { key: 'symbol',   label: 'Symbol' },
  { key: 'side',     label: 'Side' },
  { key: 'quantity', label: 'Qty', className: 'text-right font-mono' },
  { key: 'price',    label: 'Fill ₹', className: 'text-right font-mono' },
  { key: 'charges',  label: 'Charges ₹', className: 'text-right font-mono' },
  { key: 'strategy', label: 'Strategy' },
  { key: 'mode',     label: 'Mode' },
]

function FillsTable({ trades }: { trades: Trade[] }) {
  if (trades.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-10 text-gray-600">
        <Activity size={28} className="mb-3 opacity-40" />
        <p className="text-sm">No paper fills yet</p>
      </div>
    )
  }

  const rows = trades.map(t => ({
    ts:       <span className="text-xs text-gray-500 font-mono">{fmtDateTime(t.timestamp)}</span>,
    symbol:   <span className="font-mono text-blue-300 text-xs">{t.symbol}</span>,
    side:     <SideBadge side={t.side} />,
    quantity: <span className="font-mono text-xs">{t.quantity}</span>,
    price:    <span className="font-mono text-xs">{inr(t.price)}</span>,
    charges:  <span className="font-mono text-xs text-gray-500">{inr(t.charges ?? 0)}</span>,
    strategy: <span className="text-xs text-gray-400">{t.strategy_name ?? '—'}</span>,
    mode:     (
      <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-violet-500/15 text-violet-300 font-semibold">
        <CircleDot size={8} />
        PAPER SIM
      </span>
    ),
  }))

  return <Table columns={FILL_COLS} rows={rows} keyFn={(_, i) => String(i)} />
}

// ── feed status bar ───────────────────────────────────────────────────────────

function FeedStatusBar({
  running, symbols, tickCount,
}: { running: boolean; symbols: string[]; tickCount: number }) {
  return (
    <div className={clsx(
      'flex items-center gap-3 px-4 py-2 rounded-lg border text-xs',
      running
        ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300'
        : 'bg-gray-800 border-gray-700 text-gray-500'
    )}>
      {running
        ? <Wifi size={13} className="animate-pulse" />
        : <WifiOff size={13} />}
      <span className="font-semibold">{running ? 'Angel One Live Feed Active' : 'Live Feed Offline'}</span>
      {running && (
        <>
          <span className="text-gray-500">·</span>
          <span>{symbols.length} symbols subscribed</span>
          <span className="text-gray-500">·</span>
          <span>{tickCount.toLocaleString()} ticks received</span>
          <span className="text-gray-500">·</span>
          <span className="text-gray-400">Fills use real market prices</span>
        </>
      )}
      {!running && (
        <span className="text-gray-600">— Fills will use signal prices until feed starts</span>
      )}
    </div>
  )
}

// ── OMS queue ─────────────────────────────────────────────────────────────────

function OmsPanel({ events }: { events: unknown[] }) {
  if (events.length === 0) return null
  return (
    <div className="space-y-1 max-h-48 overflow-y-auto">
      {events.slice(0, 20).map((e: any, i) => (
        <div key={i} className="flex items-center gap-2 text-xs px-3 py-1.5 bg-[#161b22] rounded border border-[#30363d]">
          <span className={clsx(
            'w-2 h-2 rounded-full flex-shrink-0',
            e.status === 'FILLED' ? 'bg-emerald-400' :
            e.status === 'REJECTED' ? 'bg-red-400' :
            'bg-yellow-400 animate-pulse'
          )} />
          <span className="font-mono text-blue-300">{e.symbol ?? e.order_id ?? '—'}</span>
          <span className="text-gray-500">{e.side ?? ''}</span>
          <span className="text-gray-300">{e.status ?? ''}</span>
          {e.fill_price && <span className="font-mono text-gray-400">@ {inr(e.fill_price)}</span>}
          <span className="ml-auto text-gray-600">{e.ts ? fmtDateTime(e.ts) : ''}</span>
        </div>
      ))}
    </div>
  )
}

// ── main view ─────────────────────────────────────────────────────────────────

export function Execution() {
  const [showPaperSim, setShowPaperSim] = useState(true)
  const [positions, setPositions]       = useState<LivePosition[]>([])
  const [portfolio, setPortfolio]       = useState<LivePortfolioMetrics | null>(null)
  const [trades, setTrades]             = useState<Trade[]>([])
  const [feedRunning, setFeedRunning]   = useState(false)
  const [feedSymbols, setFeedSymbols]   = useState<string[]>([])
  const [tickCount, setTickCount]       = useState(0)
  const [omsEvents, setOmsEvents]       = useState<unknown[]>([])
  const [loading, setLoading]           = useState(true)
  const [lastRefresh, setLastRefresh]   = useState<Date | null>(null)
  const [tab, setTab]                   = useState<'positions' | 'fills' | 'oms'>('positions')

  const refresh = useCallback(async () => {
    try {
      const [posRes, tradeRes, feedRes, omsRes] = await Promise.allSettled([
        getPortfolioPositions(),
        getRecentTrades(100),
        getFeedSnapshot(),
        getOmsEvents(50),
      ])

      if (posRes.status === 'fulfilled') {
        const d = posRes.value as any
        setPositions(d.positions ?? [])
        setPortfolio(d.portfolio ?? null)
      }
      if (tradeRes.status === 'fulfilled') {
        const d = tradeRes.value as any
        setTrades(d.trades ?? [])
      }
      if (feedRes.status === 'fulfilled') {
        const d = feedRes.value as any
        setFeedRunning(d.running ?? false)
        setFeedSymbols(d.subscribed_symbols ?? [])
        setTickCount(d.tick_count ?? 0)
      }
      if (omsRes.status === 'fulfilled') {
        const d = omsRes.value as any
        setOmsEvents(d.events ?? [])
      }
      setLastRefresh(new Date())
    } finally {
      setLoading(false)
    }
  }, [])

  // initial load + auto-refresh every 8 seconds
  useEffect(() => { refresh() }, [refresh])
  useEffect(() => {
    const t = setInterval(refresh, 8000)
    return () => clearInterval(t)
  }, [refresh])

  const tabs = [
    { id: 'positions', label: 'Open Positions', count: positions.length },
    { id: 'fills',     label: 'Recent Fills',   count: trades.length },
    { id: 'oms',       label: 'OMS Queue',       count: omsEvents.length },
  ] as const

  return (
    <div className="p-6 space-y-5">

      {/* ── header ── */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-3">
          <Activity size={18} className="text-violet-400" />
          <h1 className="text-lg font-bold text-white">Execution</h1>
          <PaperSimBadge active={showPaperSim} onToggle={() => setShowPaperSim(v => !v)} />
        </div>
        <div className="flex items-center gap-3 text-xs text-gray-500">
          {lastRefresh && (
            <span className="flex items-center gap-1">
              <Clock size={11} />
              {fmtDateTime(lastRefresh.toISOString())}
            </span>
          )}
          <button
            onClick={refresh}
            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded bg-[#21262d] hover:bg-[#30363d] text-gray-400 hover:text-white transition-colors"
          >
            <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
            Refresh
          </button>
        </div>
      </div>

      {/* ── live feed status ── */}
      <FeedStatusBar running={feedRunning} symbols={feedSymbols} tickCount={tickCount} />

      {/* ── paper sim panel ── */}
      {showPaperSim ? (
        <div className="space-y-4">

          {/* how it works banner */}
          <div className="flex items-start gap-3 px-4 py-3 rounded-lg border border-violet-500/20 bg-violet-500/5 text-xs text-violet-300">
            <CheckCircle2 size={14} className="mt-0.5 flex-shrink-0 text-violet-400" />
            <div>
              <span className="font-semibold">Paper Simulation Mode</span>
              {' '}— trades are generated by the AI orchestrator and filled at real Angel One market prices,
              but <span className="font-semibold">no orders are placed in Angel One</span>.
              Use this to validate strategy profitability before going live.
            </div>
          </div>

          {/* portfolio summary */}
          {portfolio && <PortfolioBar p={portfolio} />}

          {/* tabs */}
          <Card>
            {/* tab strip */}
            <div className="flex gap-1 px-3 pt-3 border-b border-[#30363d] pb-0">
              {tabs.map(t => (
                <button
                  key={t.id}
                  onClick={() => setTab(t.id)}
                  className={clsx(
                    'px-4 py-2 rounded-t-md text-xs font-semibold transition-colors flex items-center gap-1.5 -mb-px border border-transparent',
                    tab === t.id
                      ? 'bg-[#0d1117] border-[#30363d] border-b-[#0d1117] text-violet-300'
                      : 'text-gray-500 hover:text-gray-300'
                  )}
                >
                  {t.label}
                  {t.count > 0 && (
                    <span className="px-1.5 py-0.5 rounded-full bg-[#30363d] text-[10px]">
                      {t.count}
                    </span>
                  )}
                </button>
              ))}
            </div>
            <CardBody>
              {tab === 'positions' && <PositionsTable positions={positions} />}
              {tab === 'fills'     && <FillsTable trades={trades} />}
              {tab === 'oms'       && <OmsPanel events={omsEvents} />}
            </CardBody>
          </Card>

          {/* no-live-feed warning */}
          {!feedRunning && (
            <div className="flex items-center gap-2 px-4 py-3 rounded-lg border border-yellow-500/30 bg-yellow-500/5 text-xs text-yellow-300">
              <AlertTriangle size={13} className="flex-shrink-0" />
              Live feed is not running — fills will use last signal price, not real-time Angel One price.
              Start the live feed from the Engine page to enable real-price simulation.
            </div>
          )}

        </div>
      ) : (
        /* collapsed state */
        <div className="flex flex-col items-center justify-center py-16 text-gray-600 gap-3">
          <ToggleLeft size={36} className="opacity-30" />
          <p className="text-sm">Paper Simulation is hidden</p>
          <p className="text-xs">Toggle <span className="text-violet-400 font-semibold">PAPER SIM</span> above to see live positions and fills</p>
        </div>
      )}

    </div>
  )
}
