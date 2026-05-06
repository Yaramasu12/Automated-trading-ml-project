import { useEffect, useState } from 'react'
import { Zap, Play, Loader2, ChevronDown, ChevronRight } from 'lucide-react'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Badge, regimeBadge } from '../components/shared/Badge'
import { useStore } from '../store'
import { getUniverse, scanSignals, runShadow } from '../api'
import { inr, pct, num } from '../utils'
import type { Candidate, SignalScanResult } from '../types'
import { clsx } from 'clsx'

const FALLBACK_UNDERLYINGS = [
  'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX',
  'RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 'SBIN',
  'WIPRO', 'KOTAKBANK', 'AXISBANK', 'MARUTI', 'SUNPHARMA', 'TATAMOTORS',
  'BAJFINANCE', 'HINDUNILVR', 'BHARTIARTL', 'NTPC',
]

function unique(values: string[]) {
  return [...new Set(values.map((value) => value.trim().toUpperCase()).filter(Boolean))]
}

export function Signals() {
  const signalResult = useStore((s) => s.signalResult)
  const setSignalResult = useStore((s) => s.setSignalResult)
  const loading = useStore((s) => s.loading.signals)
  const setLoading = useStore((s) => s.setLoading)
  const setError = useStore((s) => s.setError)

  const [underlyings, setUnderlyings] = useState(FALLBACK_UNDERLYINGS.join(', '))
  const [editedUnderlyings, setEditedUnderlyings] = useState(false)
  const [mode, setMode] = useState<'scan' | 'shadow'>('scan')
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  useEffect(() => {
    if (editedUnderlyings) return
    getUniverse()
      .then((rows) => {
        const derived = unique(
          rows
            .filter((row) => row.segment === 'FUTURES' || row.segment === 'OPTIONS')
            .map((row) => String(row.underlying ?? '')),
        )
        if (derived.length) setUnderlyings(derived.join(', '))
      })
      .catch(() => {})
  }, [editedUnderlyings])

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

      {/* How-to guide */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div className="bg-surface-card border border-surface-border rounded-lg p-4 space-y-2">
          <div className="text-xs font-semibold text-gray-300 mb-2">Scan vs Shadow mode</div>
          <div className="flex items-start gap-2 text-xs">
            <span className="px-2 py-0.5 rounded bg-brand-blue/20 text-brand-blue font-semibold shrink-0">SCAN</span>
            <span className="text-gray-500">Analyzes regime + strategies for each underlying and generates candidates. Does <em>not</em> place any orders. Use this to preview signals before running the agent.</span>
          </div>
          <div className="flex items-start gap-2 text-xs">
            <span className="px-2 py-0.5 rounded bg-indigo-900/60 text-indigo-300 font-semibold shrink-0">SHADOW</span>
            <span className="text-gray-500">Same analysis as Scan, but also submits approved candidates as paper orders. Use to test the full execution pipeline without risking capital.</span>
          </div>
        </div>
        <div className="bg-surface-card border border-surface-border rounded-lg p-4 space-y-2">
          <div className="text-xs font-semibold text-gray-300 mb-2">Reading the results</div>
          <div className="flex items-center gap-2 text-xs">
            <span className="px-2 py-0.5 rounded bg-brand-green/20 text-brand-green font-semibold">APPROVED</span>
            <span className="text-gray-500">Signal passed regime, strategy, and risk checks. In Shadow mode, an order was submitted.</span>
          </div>
          <div className="flex items-center gap-2 text-xs">
            <span className="px-2 py-0.5 rounded bg-brand-red/20 text-brand-red font-semibold">BLOCKED</span>
            <span className="text-gray-500">Rejected by the risk engine. Check the Reason column for the specific limit that triggered. Common: drawdown, position size, daily loss.</span>
          </div>
          <div className="flex items-center gap-2 text-xs">
            <span className="px-2 py-0.5 rounded bg-gray-700 text-gray-400 font-semibold">—</span>
            <span className="text-gray-500">Strategy generated no signal for this underlying given the current regime.</span>
          </div>
        </div>
      </div>

      {/* Controls */}
      <Card>
        <CardBody className="flex flex-wrap items-end gap-4">
          <div className="flex-1 min-w-48">
            <label className="block text-xs text-gray-500 mb-1">Underlyings (comma-separated)</label>
            <input
              value={underlyings}
              onChange={(e) => {
                setEditedUnderlyings(true)
                setUnderlyings(e.target.value)
              }}
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
                            <span title={!c.risk_decision.approved ? c.risk_decision.reason : undefined}>
                              <Badge variant={c.risk_decision.approved ? 'green' : 'red'}>
                                {c.risk_decision.approved ? 'OK' : 'BLOCKED'}
                              </Badge>
                            </span>
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
