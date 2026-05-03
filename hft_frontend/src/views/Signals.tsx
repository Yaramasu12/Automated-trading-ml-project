import { useState } from 'react'
import { Zap, Play, Loader2, ChevronDown, ChevronRight } from 'lucide-react'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Badge, regimeBadge } from '../components/shared/Badge'
import { useStore } from '../store'
import { scanSignals, runShadow } from '../api'
import { inr, pct, num } from '../utils'
import type { Candidate, SignalScanResult } from '../types'
import { clsx } from 'clsx'

const DEFAULT_UNDERLYINGS = ['NIFTY', 'BANKNIFTY', 'FINNIFTY']

export function Signals() {
  const signalResult = useStore((s) => s.signalResult)
  const setSignalResult = useStore((s) => s.setSignalResult)
  const loading = useStore((s) => s.loading.signals)
  const setLoading = useStore((s) => s.setLoading)
  const setError = useStore((s) => s.setError)

  const [underlyings, setUnderlyings] = useState(DEFAULT_UNDERLYINGS.join(', '))
  const [mode, setMode] = useState<'scan' | 'shadow'>('scan')
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  async function run() {
    setLoading('signals', true)
    setError(null)
    try {
      const payload = { underlyings: underlyings.split(',').map((s) => s.trim()).filter(Boolean) }
      const result: SignalScanResult = mode === 'scan'
        ? await scanSignals(payload)
        : await runShadow(payload)
      setSignalResult(result)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading('signals', false)
    }
  }

  function toggleExpanded(key: string) {
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }))
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-gray-100">Signal Scanner</h1>
          <p className="text-xs text-gray-500 mt-0.5">Scan market regimes and generate trade candidates</p>
        </div>
      </div>

      {/* Controls */}
      <Card>
        <CardBody className="flex flex-wrap items-end gap-4">
          <div className="flex-1 min-w-48">
            <label className="block text-xs text-gray-500 mb-1">Underlyings (comma-separated)</label>
            <input
              value={underlyings}
              onChange={(e) => setUnderlyings(e.target.value)}
              className="w-full bg-surface-elevated border border-surface-border rounded-md px-3 py-1.5 text-sm text-gray-200 font-mono focus:outline-none focus:border-brand-blue"
            />
          </div>

          <div>
            <label className="block text-xs text-gray-500 mb-1">Mode</label>
            <div className="flex rounded-md border border-surface-border overflow-hidden">
              {(['scan', 'shadow'] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  className={clsx(
                    'px-3 py-1.5 text-xs font-medium capitalize transition-colors',
                    mode === m
                      ? 'bg-brand-blue/20 text-brand-blue'
                      : 'text-gray-400 hover:text-gray-200 bg-surface-elevated',
                  )}
                >
                  {m}
                </button>
              ))}
            </div>
          </div>

          <button
            onClick={run}
            disabled={loading}
            className="flex items-center gap-2 px-4 py-1.5 rounded-md bg-brand-blue text-surface text-sm font-medium hover:bg-brand-blue/80 disabled:opacity-50 transition-colors"
          >
            {loading ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
            Run Scan
          </button>
        </CardBody>
      </Card>

      {/* Summary */}
      {signalResult && (
        <div className="grid grid-cols-3 gap-3">
          <div className="bg-surface-card border border-surface-border rounded-lg p-3 text-center">
            <div className="text-2xl font-bold font-mono text-brand-green">{signalResult.approved_candidates}</div>
            <div className="text-xs text-gray-500 mt-1">Approved</div>
          </div>
          <div className="bg-surface-card border border-surface-border rounded-lg p-3 text-center">
            <div className="text-2xl font-bold font-mono text-brand-red">{signalResult.rejected_candidates}</div>
            <div className="text-xs text-gray-500 mt-1">Rejected</div>
          </div>
          <div className="bg-surface-card border border-surface-border rounded-lg p-3 text-center">
            <div className="text-2xl font-bold font-mono text-brand-blue">{signalResult.submitted_orders}</div>
            <div className="text-xs text-gray-500 mt-1">Orders</div>
          </div>
        </div>
      )}

      {/* Per-underlying scan results */}
      {signalResult?.scans.map((scan) => {
        const key = scan.underlying
        const isOpen = expanded[key] ?? true
        return (
          <Card key={key}>
            <button
              className="w-full"
              onClick={() => toggleExpanded(key)}
            >
              <CardHeader
                title={scan.underlying}
                subtitle={`${scan.candidates.length} candidates · vol ${pct(scan.volatility_forecast.annualized_volatility * 100)}`}
                icon={isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                action={
                  <div className="flex items-center gap-2">
                    {regimeBadge(scan.regime)}
                    <Badge variant="gray">{scan.selected_strategies.length} strategies</Badge>
                  </div>
                }
              />
            </button>

            {isOpen && (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-surface-border text-gray-500 uppercase tracking-wider">
                      <th className="px-4 py-2 text-left">Strategy</th>
                      <th className="px-4 py-2 text-left">Instrument</th>
                      <th className="px-4 py-2 text-left">Side</th>
                      <th className="px-4 py-2 text-right">Confidence</th>
                      <th className="px-4 py-2 text-right">Price</th>
                      <th className="px-4 py-2 text-right">Qty</th>
                      <th className="px-4 py-2 text-left">Risk</th>
                      <th className="px-4 py-2 text-left">Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {scan.candidates.map((c: Candidate, i) => (
                      <tr key={i} className="border-b border-surface-border/40 hover:bg-surface-elevated/40">
                        <td className="px-4 py-2 font-medium text-gray-200">{c.strategy_name}</td>
                        <td className="px-4 py-2 font-mono text-brand-blue">{c.instrument.symbol}</td>
                        <td className="px-4 py-2">
                          {c.signal ? (
                            <Badge variant={c.signal.side === 'BUY' ? 'green' : 'red'}>
                              {c.signal.side}
                            </Badge>
                          ) : '—'}
                        </td>
                        <td className="px-4 py-2 text-right font-mono">
                          {c.signal ? `${(c.signal.confidence * 100).toFixed(0)}%` : '—'}
                        </td>
                        <td className="px-4 py-2 text-right font-mono">
                          {c.signal ? inr(c.signal.price, 2) : '—'}
                        </td>
                        <td className="px-4 py-2 text-right font-mono">{c.quantity}</td>
                        <td className="px-4 py-2">
                          {c.risk_decision ? (
                            <Badge variant={c.risk_decision.approved ? 'green' : 'red'}>
                              {c.risk_decision.approved ? 'OK' : 'BLOCKED'}
                            </Badge>
                          ) : '—'}
                        </td>
                        <td className="px-4 py-2 text-gray-400 max-w-48 truncate">{c.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        )
      })}

      {!signalResult && !loading && (
        <div className="flex flex-col items-center justify-center py-20 text-gray-600">
          <Zap size={36} className="mb-3 opacity-40" />
          <p className="text-sm">Run a scan to see signal candidates</p>
        </div>
      )}

      {loading && (
        <div className="flex flex-col items-center justify-center py-20 text-gray-500">
          <Loader2 size={32} className="animate-spin mb-3" />
          <p className="text-sm">Scanning market regimes…</p>
        </div>
      )}
    </div>
  )
}
