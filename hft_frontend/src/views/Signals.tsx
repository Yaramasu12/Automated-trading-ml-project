import { useState, useCallback, useEffect } from 'react'
import { Zap, Loader2, ChevronDown, ChevronRight, AlertTriangle, PlayCircle } from 'lucide-react'
import { clsx } from 'clsx'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { PageHeader } from '../components/shared/PageHeader'
import { Table } from '../components/shared/Table'
import { Tag, regimeBadge } from '../components/shared/Badge'
import { useStore } from '../store'
import { scanSignals, runShadow, getStrategyCatalog } from '../api'
import { pct, fmtDate } from '../utils'
import type { Candidate } from '../types'

interface CatalogStrategy {
  name?: string
  strategy_name?: string
  family?: string
}

const UNDERLYINGS = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 'SBIN']
const DAY_OPTIONS = [7, 14, 30, 60]

export function Signals() {
  const signalResult    = useStore((s) => s.signalResult)
  const setSignalResult = useStore((s) => s.setSignalResult)
  const loading         = useStore((s) => s.loading.signals)
  const setLoading      = useStore((s) => s.setLoading)
  const setError        = useStore((s) => s.setError)

  const [selectedUnderlyings, setSelectedUnderlyings] = useState<string[]>(['NIFTY', 'BANKNIFTY'])
  const [days, setDays]                               = useState(14)
  const [selectedStrategies, setSelectedStrategies]   = useState<string[]>([])
  const [expandedIdx, setExpandedIdx]                 = useState<number | null>(null)
  const [strategies, setStrategies]                   = useState<CatalogStrategy[]>([])

  useEffect(() => {
    // Section 19 audit (2026-09-16): this chip list used to be a hand-typed
    // array of strategy class names (e.g. "ShortStraddleStrategy") that
    // shared zero overlap with the real catalog's snake_case names (e.g.
    // "iron_condor") — every filter selection silently matched nothing,
    // since inception. Source it from the real catalog instead.
    getStrategyCatalog()
      .then((res) => setStrategies((res.strategies as CatalogStrategy[]) ?? []))
      .catch(() => setStrategies([]))
  }, [])

  function toggleUnderlying(u: string) {
    setSelectedUnderlyings(prev =>
      prev.includes(u) ? prev.filter(x => x !== u) : [...prev, u]
    )
  }

  function toggleStrategy(s: string) {
    setSelectedStrategies(prev =>
      prev.includes(s) ? prev.filter(x => x !== s) : [...prev, s]
    )
  }

  const runScan = useCallback(async (shadow = false) => {
    if (selectedUnderlyings.length === 0) return
    if (shadow && !window.confirm(
      'Shadow Run submits real PAPER orders against an isolated demo ledger over a fixed ' +
      'historical window (2026-01-01 onward), not "the last ' + days + ' days" like Scan Signals. ' +
      'It does not touch your live paper portfolio or equity curve. Continue?'
    )) {
      return
    }
    setLoading('signals', true)
    setError(null)
    try {
      const payload = {
        underlyings: selectedUnderlyings,
        days,
        strategy_names: selectedStrategies.length > 0 ? selectedStrategies : undefined,
      }
      const res = shadow ? await runShadow(payload) : await scanSignals(payload)
      setSignalResult(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Scan failed')
    } finally {
      setLoading('signals', false)
    }
  }, [selectedUnderlyings, days, selectedStrategies, setLoading, setError, setSignalResult])

  const candidateColumns = [
    {
      key: 'instrument', label: 'Instrument',
      render: (_: unknown, r: Candidate) => <span className="text-brand-blue font-mono">{r.instrument.symbol}</span>,
    },
    {
      key: 'strategy_name', label: 'Strategy',
      render: (_: unknown, r: Candidate) => <span className="text-xs text-ink-muted">{r.strategy_name}</span>,
    },
    {
      key: 'side', label: 'Side',
      render: (_: unknown, r: Candidate) => r.signal ? (
        <span className={r.signal.side === 'BUY' ? 'text-brand-green' : 'text-brand-red'}>{r.signal.side}</span>
      ) : <span className="text-ink-faint">—</span>,
    },
    {
      key: 'confidence', label: 'Confidence', align: 'right' as const,
      render: (_: unknown, r: Candidate) => r.signal ? (
        <span className={clsx(
          'font-mono font-semibold',
          r.signal.confidence > 0.7 ? 'text-brand-green' : r.signal.confidence > 0.4 ? 'text-brand-yellow' : 'text-brand-red',
        )}>
          {pct(r.signal.confidence * 100)}
        </span>
      ) : <span className="text-ink-faint">—</span>,
    },
    {
      key: 'risk_decision', label: 'Risk Decision',
      render: (_: unknown, r: Candidate) => r.risk_decision ? (
        r.risk_decision.approved
          ? <Tag label="Approved" color="green" />
          : <Tag label="Rejected" color="red" />
      ) : <Tag label="Pending" color="gray" />,
    },
    {
      key: 'reason', label: 'Reason',
      render: (_: unknown, r: Candidate) => (
        <span className="text-xs text-ink-faint truncate">{r.risk_decision?.reason ?? r.reason}</span>
      ),
    },
  ]

  return (
    <div className="space-y-6">

      <PageHeader
        title="Signal Scanner"
        subtitle="Multi-strategy signal detection across the universe"
        icon={<Zap size={18} />}
      />

      {/* Form */}
      <Card>
        <CardHeader title="Scan Parameters" icon={<Zap size={14} />} />
        <CardBody className="space-y-4">
          {/* Underlying chips */}
          <div>
            <label className="block text-xs text-ink-muted mb-2 font-semibold uppercase tracking-wider">Underlyings</label>
            <div className="flex flex-wrap gap-1.5">
              {UNDERLYINGS.map((u) => (
                <button
                  key={u}
                  onClick={() => toggleUnderlying(u)}
                  className={clsx(
                    'px-2.5 py-1 rounded text-xs font-mono border transition-colors',
                    selectedUnderlyings.includes(u)
                      ? 'bg-brand-blue/15 border-brand-blue/30 text-brand-blue'
                      : 'bg-surface-elevated border-surface-border text-ink-muted hover:text-ink',
                  )}
                >
                  {u}
                </button>
              ))}
            </div>
          </div>

          {/* Days */}
          <div>
            <label className="block text-xs text-ink-muted mb-2 font-semibold uppercase tracking-wider">Lookback</label>
            <div className="flex gap-1.5">
              {DAY_OPTIONS.map((d) => (
                <button
                  key={d}
                  onClick={() => setDays(d)}
                  className={clsx(
                    'px-3 py-1 rounded text-xs font-mono border transition-colors',
                    days === d
                      ? 'bg-brand-cyan/15 border-brand-cyan/30 text-brand-cyan'
                      : 'bg-surface-elevated border-surface-border text-ink-muted hover:text-ink',
                  )}
                >
                  {d}d
                </button>
              ))}
            </div>
          </div>

          {/* Strategy filter — sourced live from GET /strategies/catalog */}
          <div>
            <label className="block text-xs text-ink-muted mb-2 font-semibold uppercase tracking-wider">
              Strategies <span className="text-ink-faint normal-case">(optional)</span>
            </label>
            <div className="flex flex-wrap gap-1.5">
              {strategies.length === 0 && (
                <span className="text-xs text-ink-faint">Loading catalog…</span>
              )}
              {strategies.map((cat) => {
                const s = cat.name ?? cat.strategy_name ?? ''
                if (!s) return null
                return (
                  <button
                    key={s}
                    onClick={() => toggleStrategy(s)}
                    title={cat.family ? `family: ${cat.family}` : undefined}
                    className={clsx(
                      'px-2 py-1 rounded text-[11px] font-mono border transition-colors',
                      selectedStrategies.includes(s)
                        ? 'bg-brand-purple/15 border-brand-purple/30 text-brand-purple'
                        : 'bg-surface-elevated border-surface-border text-ink-faint hover:text-ink-muted',
                    )}
                  >
                    {s}
                  </button>
                )
              })}
            </div>
          </div>

          {/* Buttons */}
          <div className="flex gap-3 pt-2">
            <button
              onClick={() => runScan(false)}
              disabled={loading || selectedUnderlyings.length === 0}
              className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-green/15 border border-brand-green/30 text-brand-green text-sm font-medium hover:bg-brand-green/25 disabled:opacity-50 transition-colors"
            >
              {loading ? <Loader2 size={14} className="animate-spin" /> : <Zap size={14} />}
              Scan Signals
            </button>
            <button
              onClick={() => runScan(true)}
              disabled={loading || selectedUnderlyings.length === 0}
              title="Submits real PAPER orders against an isolated demo ledger over a fixed historical window — not a passive preview"
              className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-yellow/15 border border-brand-yellow/30 text-brand-yellow text-sm font-medium hover:bg-brand-yellow/25 disabled:opacity-50 transition-colors"
            >
              {loading ? <Loader2 size={14} className="animate-spin" /> : <PlayCircle size={14} />}
              Shadow Run (demo orders)
            </button>
          </div>
        </CardBody>
      </Card>

      {/* Results */}
      {signalResult && (
        <div className="space-y-4">
          {signalResult.is_shadow && (
            <div className="flex items-start gap-2 px-4 py-3 rounded-lg border border-brand-yellow/30 bg-brand-yellow/5 text-xs text-brand-yellow">
              <AlertTriangle size={14} className="mt-0.5 flex-shrink-0" />
              <div>
                <span className="font-semibold">Demo shadow run</span> — {signalResult.filled_orders ?? 0} filled /{' '}
                {signalResult.rejected_orders ?? 0} rejected against an isolated scratch ledger
                {signalResult.shadow_window && (
                  <> over the fixed historical window starting {fmtDate(signalResult.shadow_window.start)}
                    {' '}({signalResult.shadow_window.days}d{signalResult.shadow_window.used_live_history ? ', live history' : ', demo bars'})</>
                )}. Not your live paper portfolio or equity curve.
              </div>
            </div>
          )}
          {/* Summary row */}
          <div className="flex items-center gap-3 flex-wrap">
            <span className="text-xs text-ink-muted">Mode: <span className="text-ink font-mono">{signalResult.mode}</span></span>
            <Tag label={`${signalResult.approved_candidates} Approved`}  color="green" />
            <Tag label={`${signalResult.rejected_candidates} Rejected`}  color="red"   />
            <Tag label={`${signalResult.submitted_orders} Submitted`}    color="blue"  />
          </div>

          {/* Per-underlying accordion */}
          {signalResult.scans.map((scan, idx) => (
            <Card key={scan.underlying}>
              <button
                className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-surface-elevated/30 transition-colors rounded-t-lg"
                onClick={() => setExpandedIdx(expandedIdx === idx ? null : idx)}
              >
                <div className="flex items-center gap-3 flex-wrap">
                  <span className="text-sm font-semibold text-ink">{scan.underlying}</span>
                  {regimeBadge(scan.regime)}
                  <span className="text-xs text-ink-faint font-mono">
                    Vol: {(scan.volatility_forecast.annualized_volatility * 100).toFixed(1)}%
                  </span>
                  <span className="text-xs text-ink-faint">{scan.candidates.length} candidates</span>
                </div>
                {expandedIdx === idx
                  ? <ChevronDown size={14} className="text-ink-faint flex-shrink-0" />
                  : <ChevronRight size={14} className="text-ink-faint flex-shrink-0" />}
              </button>
              {expandedIdx === idx && (
                <div className="border-t border-surface-border">
                  <div className="px-4 py-2 bg-surface-elevated/30">
                    <span className="text-xs text-ink-faint">
                      Strategies: {scan.selected_strategies.join(', ') || 'none selected'}
                    </span>
                  </div>
                  <Table
                    columns={candidateColumns as Parameters<typeof Table>[0]['columns']}
                    rows={scan.candidates as unknown as Record<string, unknown>[]}
                    keyFn={(r) => String((r as unknown as Candidate).instrument?.symbol ?? Math.random())}
                    emptyMessage="No candidates"
                    compact
                  />
                </div>
              )}
            </Card>
          ))}
        </div>
      )}

    </div>
  )
}
