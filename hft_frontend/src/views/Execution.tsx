import { useEffect, useState, useCallback } from 'react'
import {
  Activity, RefreshCw, Wifi, WifiOff,
  ToggleLeft, ToggleRight, CircleDot, Clock, Layers,
  AlertTriangle, CheckCircle2, ArrowUpRight, ArrowDownRight,
} from 'lucide-react'
import { clsx } from 'clsx'
import { Card, CardBody } from '../components/shared/Card'
import { Table } from '../components/shared/Table'
import { PageHeader } from '../components/shared/PageHeader'
import { EmptyState } from '../components/shared/States'
import { fmtDateTime, inr, pct } from '../utils'
import {
  getPortfolioPositions, getRecentTrades, getFeedSnapshot,
  getOmsEvents,
} from '../api'
import type { LivePosition, LivePortfolioMetrics, Trade } from '../types'

// ── helpers ───────────────────────────────────────────────────────────────────

function PnlCell({ value }: { value: number }) {
  const pos = value >= 0
  return (
    <span className={clsx('font-mono font-semibold', pos ? 'text-brand-green' : 'text-brand-red')}>
      {pos ? '+' : ''}{inr(value)}
    </span>
  )
}

function PctCell({ value }: { value: number }) {
  const pos = value >= 0
  return (
    <span className={clsx('font-mono text-xs', pos ? 'text-brand-green' : 'text-brand-red')}>
      {pos ? '+' : ''}{pct(value / 100)}
    </span>
  )
}

function SideBadge({ side }: { side: string }) {
  const isBuy = side.toUpperCase() === 'BUY'
  return (
    <span className={clsx(
      'inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-bold uppercase tracking-wide',
      isBuy ? 'bg-brand-green/15 text-brand-green' : 'bg-brand-red/15 text-brand-red'
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
      live ? 'text-brand-green' : 'text-ink-faint'
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
          ? 'bg-brand-purple/15 border-brand-purple/40 text-brand-purple'
          : 'bg-surface-elevated border-surface-border text-ink-faint hover:text-ink-muted'
      )}
    >
      {active ? <ToggleRight size={14} className="text-brand-purple" /> : <ToggleLeft size={14} />}
      PAPER SIM
    </button>
  )
}

// ── portfolio summary bar ─────────────────────────────────────────────────────

function PortfolioBar({ p }: { p: LivePortfolioMetrics }) {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
      {[
        { label: 'Equity', value: inr(p.equity), color: 'text-ink' },
        { label: 'Cash', value: inr(p.cash), color: 'text-ink-muted' },
        { label: 'Unrealised P&L', value: inr(p.unrealized_pnl), color: p.unrealized_pnl >= 0 ? 'text-brand-green' : 'text-brand-red' },
        { label: 'Realised P&L', value: inr(p.realized_pnl), color: p.realized_pnl >= 0 ? 'text-brand-green' : 'text-brand-red' },
        { label: 'Drawdown', value: pct(p.drawdown / 100), color: p.drawdown > 5 ? 'text-brand-red' : 'text-ink-muted' },
        { label: 'Open Positions', value: String(p.open_positions), color: 'text-brand-blue' },
      ].map(m => (
        <div key={m.label} className="bg-surface-card border border-surface-border rounded-xl p-3 shadow-card">
          <p className="eyebrow mb-1">{m.label}</p>
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
      <EmptyState
        icon={<Layers size={20} />}
        title="No open paper positions yet"
        hint="The agent will populate this once it generates trades."
      />
    )
  }

  const rows = positions.map(p => ({
    symbol:         <span className="font-mono text-brand-blue text-xs">{p.symbol}</span>,
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
    return <EmptyState icon={<Activity size={20} />} title="No paper fills yet" />
  }

  const rows = trades.map(t => ({
    ts:       <span className="text-xs text-ink-faint font-mono">{fmtDateTime(t.timestamp)}</span>,
    symbol:   <span className="font-mono text-brand-blue text-xs">{t.symbol}</span>,
    side:     <SideBadge side={t.side} />,
    quantity: <span className="font-mono text-xs">{t.quantity}</span>,
    price:    <span className="font-mono text-xs">{inr(t.price)}</span>,
    charges:  <span className="font-mono text-xs text-ink-faint">{inr(t.charges ?? 0)}</span>,
    strategy: <span className="text-xs text-ink-muted">{t.strategy_name ?? '—'}</span>,
    mode:     (
      <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-brand-purple/15 text-brand-purple font-semibold">
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
      'flex flex-wrap items-center gap-2 px-4 py-2.5 rounded-lg border text-xs',
      running
        ? 'bg-brand-green/10 border-brand-green/30 text-brand-green'
        : 'bg-surface-elevated border-surface-border text-ink-faint'
    )}>
      {running ? <Wifi size={13} className="animate-pulse" /> : <WifiOff size={13} />}
      <span className="font-semibold">{running ? 'Angel One Live Feed Active' : 'Live Feed Offline'}</span>
      {running ? (
        <>
          <span className="text-ink-faint">·</span>
          <span>{symbols.length} symbols subscribed</span>
          <span className="text-ink-faint">·</span>
          <span>{tickCount.toLocaleString()} ticks received</span>
          <span className="text-ink-faint">·</span>
          <span className="text-ink-muted">Fills use real market prices</span>
        </>
      ) : (
        <span className="text-ink-faint">— Fills will use signal prices until feed starts</span>
      )}
    </div>
  )
}

// ── OMS queue ─────────────────────────────────────────────────────────────────

function OmsPanel({ events }: { events: unknown[] }) {
  if (events.length === 0) {
    return <EmptyState icon={<Layers size={20} />} title="OMS queue is empty" />
  }
  return (
    <div className="space-y-1 max-h-64 overflow-y-auto">
      {events.slice(0, 20).map((e: any, i) => (
        <div key={i} className="flex items-center gap-2 text-xs px-3 py-1.5 bg-surface-inset rounded-lg border border-surface-border">
          <span className={clsx(
            'w-2 h-2 rounded-full flex-shrink-0',
            e.status === 'FILLED' ? 'bg-brand-green' :
            e.status === 'REJECTED' ? 'bg-brand-red' :
            'bg-brand-yellow animate-pulse'
          )} />
          <span className="font-mono text-brand-blue">{e.symbol ?? e.order_id ?? '—'}</span>
          <span className="text-ink-faint">{e.side ?? ''}</span>
          <span className="text-ink-muted">{e.status ?? ''}</span>
          {e.fill_price && <span className="font-mono text-ink-muted">@ {inr(e.fill_price)}</span>}
          <span className="ml-auto text-ink-faint">{e.ts ? fmtDateTime(e.ts) : ''}</span>
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
    <div className="space-y-6">

      <PageHeader
        title="Execution"
        subtitle="Paper-simulated fills at real Angel One prices"
        icon={<Activity size={18} />}
        actions={
          <>
            <PaperSimBadge active={showPaperSim} onToggle={() => setShowPaperSim(v => !v)} />
            {lastRefresh && (
              <span className="hidden md:flex items-center gap-1 text-xs text-ink-faint">
                <Clock size={11} />
                {fmtDateTime(lastRefresh.toISOString())}
              </span>
            )}
            <button
              onClick={refresh}
              className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-surface-card border border-surface-border text-ink-muted hover:text-ink hover:border-surface-border-strong transition-all text-xs"
            >
              <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
              <span className="hidden sm:inline">Refresh</span>
            </button>
          </>
        }
      />

      {/* ── live feed status ── */}
      <FeedStatusBar running={feedRunning} symbols={feedSymbols} tickCount={tickCount} />

      {/* ── paper sim panel ── */}
      {showPaperSim ? (
        <div className="space-y-4">

          {/* how it works banner */}
          <div className="flex items-start gap-3 px-4 py-3 rounded-lg border border-brand-purple/20 bg-brand-purple/5 text-xs text-brand-purple">
            <CheckCircle2 size={14} className="mt-0.5 flex-shrink-0" />
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
            <div className="flex gap-1 px-3 pt-3 border-b border-surface-border">
              {tabs.map(t => (
                <button
                  key={t.id}
                  onClick={() => setTab(t.id)}
                  className={clsx(
                    'px-4 py-2 rounded-t-lg text-xs font-semibold transition-colors flex items-center gap-1.5 -mb-px border border-transparent',
                    tab === t.id
                      ? 'bg-surface-inset border-surface-border border-b-surface-inset text-brand-purple'
                      : 'text-ink-faint hover:text-ink-muted'
                  )}
                >
                  {t.label}
                  {t.count > 0 && (
                    <span className="px-1.5 py-0.5 rounded-full bg-surface-elevated text-[10px]">
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
            <div className="flex items-center gap-2 px-4 py-3 rounded-lg border border-brand-yellow/30 bg-brand-yellow/5 text-xs text-brand-yellow">
              <AlertTriangle size={13} className="flex-shrink-0" />
              Live feed is not running — fills will use last signal price, not real-time Angel One price.
              Start the live feed from the Engine page to enable real-price simulation.
            </div>
          )}

        </div>
      ) : (
        <EmptyState
          className="py-16"
          icon={<ToggleLeft size={22} />}
          title="Paper Simulation is hidden"
          hint="Toggle PAPER SIM above to see live positions and fills."
        />
      )}

    </div>
  )
}
