import { useState, useCallback } from 'react'
import { Zap, Loader2, ChevronDown, ChevronRight, Eye } from 'lucide-react'
import { clsx } from 'clsx'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Table } from '../components/shared/Table'
import { Tag, regimeBadge } from '../components/shared/Badge'
import { useStore } from '../store'
import { scanSignals, runShadow } from '../api'
import { pct } from '../utils'
import type { Candidate } from '../types'

const UNDERLYINGS = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 'SBIN']
const DAY_OPTIONS = [7, 14, 30, 60]
const STRATEGIES = [
  'ShortStraddleStrategy', 'IronCondorStrategy', 'CoveredCallStrategy',
  'BullCallSpreadStrategy', 'ProtectivePutStrategy', 'MomentumBreakoutStrategy',
]

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
      render: (_: unknown, r: Candidate) => <span className="text-xs text-gray-400">{r.strategy_name}</span>,
    },
    {
      key: 'side', label: 'Side',
      render: (_: unknown, r: Candidate) => r.signal ? (
        <span className={r.signal.side === 'BUY' ? 'text-brand-green' : 'text-brand-red'}>{r.signal.side}</span>
      ) : <span className="text-gray-600">—</span>,
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
      ) : <span className="text-gray-600">—</span>,
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
        <span className="text-xs text-gray-500 truncate">{r.risk_decision?.reason ?? r.reason}</span>
      ),
    },
  ]

  return (
    <div className="space-y-5">

      {/* Header */}
      <div className="flex items-center gap-2">
        <Zap size={16} className="text-brand-yellow" />
        <div>
          <h1 className="text-lg font-bold text-gray-100">Signal Scanner</h1>
          <p className="text-xs text-gray-500 mt-0.5">Multi-strategy signal detection across the universe</p>
        </div>
      </div>

      {/* Form */}
      <Card>
        <CardHeader title="Scan Parameters" icon={<Zap size={14} />} />
        <CardBody className="space-y-4">
          {/* Underlying chips */}
          <div>
            <label className="block text-xs text-gray-400 mb-2 font-semibold uppercase tracking-wider">Underlyings</label>
            <div className="flex flex-wrap gap-1.5">
              {UNDERLYINGS.map((u) => (
                <button
                  key={u}
                  onClick={() => toggleUnderlying(u)}
                  className={clsx(
                    'px-2.5 py-1 rounded text-xs font-mono border transition-colors',
                    selectedUnderlyings.includes(u)
                      ? 'bg-brand-blue/15 border-brand-blue/30 text-brand-blue'
                      : 'bg-surface-elevated border-surface-border text-gray-400 hover:text-gray-200',
                  )}
                >
                  {u}
                </button>
              ))}
            </div>
          </div>

          {/* Days */}
          <div>
            <label className="block text-xs text-gray-400 mb-2 font-semibold uppercase tracking-wider">Lookback</label>
            <div className="flex gap-1.5">
              {DAY_OPTIONS.map((d) => (
                <button
                  key={d}
                  onClick={() => setDays(d)}
                  className={clsx(
                    'px-3 py-1 rounded text-xs font-mono border transition-colors',
                    days === d
                      ? 'bg-brand-cyan/15 border-brand-cyan/30 text-brand-cyan'
                      : 'bg-surface-elevated border-surface-border text-gray-400 hover:text-gray-200',
                  )}
                >
                  {d}d
                </button>
              ))}
            </div>
          </div>

          {/* Strategy filter */}
          <div>
            <label className="block text-xs text-gray-400 mb-2 font-semibold uppercase tracking-wider">
              Strategies <span className="text-gray-600 normal-case">(optional)</span>
            </label>
            <div className="flex flex-wrap gap-1.5">
              {STRATEGIES.map((s) => (
                <button
                  key={s}
                  onClick={() => toggleStrategy(s)}
                  className={clsx(
                    'px-2 py-1 rounded text-[11px] font-mono border transition-colors',
                    selectedStrategies.includes(s)
                      ? 'bg-brand-purple/15 border-brand-purple/30 text-brand-purple'
                      : 'bg-surface-elevated border-surface-border text-gray-500 hover:text-gray-300',
                  )}
                >
                  {s.replace('Strategy', '')}
                </button>
              ))}
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
              className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-yellow/15 border border-brand-yellow/30 text-brand-yellow text-sm font-medium hover:bg-brand-yellow/25 disabled:opacity-50 transition-colors"
            >
              {loading ? <Loader2 size={14} className="animate-spin" /> : <Eye size={14} />}
              Shadow Run
            </button>
          </div>
        </CardBody>
      </Card>

      {/* Results */}
      {signalResult && (
        <div className="space-y-4">
          {/* Summary row */}
          <div className="flex items-center gap-3 flex-wrap">
            <span className="text-xs text-gray-400">Mode: <span className="text-gray-200 font-mono">{signalResult.mode}</span></span>
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
                  <span className="text-sm font-semibold text-gray-100">{scan.underlying}</span>
                  {regimeBadge(scan.regime)}
                  <span className="text-xs text-gray-500 font-mono">
                    Vol: {(scan.volatility_forecast.annualized_volatility * 100).toFixed(1)}%
                  </span>
                  <span className="text-xs text-gray-600">{scan.candidates.length} candidates</span>
                </div>
                {expandedIdx === idx
                  ? <ChevronDown size={14} className="text-gray-500 flex-shrink-0" />
                  : <ChevronRight size={14} className="text-gray-500 flex-shrink-0" />}
              </button>
              {expandedIdx === idx && (
                <div className="border-t border-surface-border">
                  <div className="px-4 py-2 bg-surface-elevated/30">
                    <span className="text-xs text-gray-500">
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
