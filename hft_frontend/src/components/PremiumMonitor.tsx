import { useCallback, useEffect, useState } from 'react'
import { Gauge, Loader2, RefreshCw } from 'lucide-react'
import { Card, CardBody, CardHeader } from './shared/Card'
import { Badge } from './shared/Badge'
import { getPremiumMonitorStatus } from '../api'
import { num } from '../utils'
import type { PremiumMonitorReading, PremiumMonitorStatus } from '../types'

/**
 * Read-only view of each index's own vol-selling premium (implied vol minus
 * realized vol), refreshed once daily by the scheduler from NSE's free F&O
 * bhavcopy — see audit_results/2026-09-19/per-index-strategy-fit and
 * research/index_premium.py. Mirrors GET /research/premium-status.
 *
 * REPORTING ONLY: this never places or influences a real order. It exists so
 * "is BANKNIFTY's premium rich enough to be worth another look" can be
 * answered by glancing at a page instead of re-running the research scripts.
 */
export function PremiumMonitor() {
  const [data, setData] = useState<PremiumMonitorStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await getPremiumMonitorStatus())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'failed to load premium status')
      setData(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  return (
    <Card>
      <CardHeader
        title="Vol Premium Monitor"
        subtitle="Each index's own implied-vol premium vs. its walk-forward research threshold — reporting only"
        icon={<Gauge className="w-4 h-4 text-brand-blue" />}
        action={
          <button onClick={load} className="text-ink-muted hover:text-ink" title="Refresh">
            {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
          </button>
        }
      />
      <CardBody className="space-y-2">
        {error && <p className="text-xs text-brand-red">{error}</p>}
        {data && data.indices.length === 0 && !error && (
          <p className="text-xs text-ink-faint">
            No readings yet — the daily scheduler populates this at 20:00 IST after NSE
            publishes the day's bhavcopy. Run scripts/backfill_premium_monitor.py for history.
          </p>
        )}
        {data && data.indices.length > 0 && (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-ink-faint text-left">
                    <th className="py-1">Index</th>
                    <th>As of</th>
                    <th className="text-right">ATM IV30</th>
                    <th className="text-right">Realized</th>
                    <th className="text-right">Premium</th>
                    <th className="text-right">60d median</th>
                    <th className="text-right">Threshold</th>
                    <th>Signal</th>
                  </tr>
                </thead>
                <tbody>
                  {data.indices.map((r) => <Row key={r.index} r={r} />)}
                </tbody>
              </table>
            </div>
            <p className="text-[10px] text-ink-faint">{data.note}</p>
          </>
        )}
      </CardBody>
    </Card>
  )
}

function Row({ r }: { r: PremiumMonitorReading }) {
  return (
    <tr className="border-t border-surface-border">
      <td className="py-1 font-semibold text-ink">{r.index}</td>
      <td className="text-ink-faint">{r.as_of}</td>
      <td className="text-right">{num(r.atm_iv30_pct)}%</td>
      <td className="text-right">{r.realized_vol20_pct != null ? `${num(r.realized_vol20_pct)}%` : '—'}</td>
      <td className="text-right">{r.premium_pts != null ? `${num(r.premium_pts)} pts` : '—'}</td>
      <td className="text-right">{r.trailing_median_60d_pts != null ? `${num(r.trailing_median_60d_pts)} pts` : '—'}</td>
      <td className="text-right text-ink-faint">{r.signal_theta_pts != null ? `≥ ${num(r.signal_theta_pts)} pts` : '—'}</td>
      <td><SignalBadge signal={r.signal} /></td>
    </tr>
  )
}

function SignalBadge({ signal }: { signal: string }) {
  if (signal === 'premium rich (trade)') return <Badge variant="green" dot>rich</Badge>
  if (signal === 'premium thin (stand aside)') return <Badge variant="yellow">thin</Badge>
  if (signal === 'no validated strategy') return <Badge variant="gray">unvalidated</Badge>
  return <Badge variant="gray">building history</Badge>
}
