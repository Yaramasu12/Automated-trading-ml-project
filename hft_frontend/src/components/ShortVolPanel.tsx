import { useCallback, useEffect, useState } from 'react'
import { Activity, Loader2, RefreshCw } from 'lucide-react'
import { Card, CardBody, CardHeader } from './shared/Card'
import { Badge } from './shared/Badge'
import { getShortVolPreview } from '../api'
import { num } from '../utils'
import type { ShortVolPreview } from '../types'

const UNDERLYINGS = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX']

/**
 * Read-only view of the volatility-risk-premium short-vol strategy: live spot,
 * India VIX, realized vol, VRP, whether it would enter, and the resolved iron
 * condor legs. Mirrors GET /short-vol/preview — it never places an order.
 */
export function ShortVolPanel() {
  const [underlying, setUnderlying] = useState('NIFTY')
  const [data, setData] = useState<ShortVolPreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async (u: string) => {
    setLoading(true)
    setError(null)
    try {
      setData(await getShortVolPreview(u))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'preview failed')
      setData(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load(underlying) }, [underlying, load])

  return (
    <Card>
      <CardHeader
        title="Short-Vol (VRP) Condor"
        subtitle="Sells the volatility risk premium as a defined-risk iron condor"
        icon={<Activity className="w-4 h-4 text-brand-blue" />}
        action={
          <button
            onClick={() => load(underlying)}
            className="text-ink-muted hover:text-ink"
            title="Refresh"
          >
            {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
          </button>
        }
      />
      <CardBody className="space-y-3">
        <div className="flex flex-wrap gap-1.5">
          {UNDERLYINGS.map((u) => (
            <button
              key={u}
              onClick={() => setUnderlying(u)}
              className={
                'px-2 py-0.5 rounded text-xs border ' +
                (u === underlying
                  ? 'bg-brand-blue/20 border-brand-blue text-brand-blue'
                  : 'border-surface-border text-ink-muted hover:text-ink')
              }
            >
              {u}
            </button>
          ))}
        </div>

        {error && <p className="text-xs text-brand-red">{error}</p>}

        {data && (
          <>
            <div className="grid grid-cols-4 gap-2 text-center">
              <Metric label="Spot" value={num(data.spot)} />
              <Metric label="India VIX" value={num(data.vix)} />
              <Metric label="Realized" value={`${num(data.realized_vol)}%`} />
              <Metric label="VRP" value={num(data.vrp)} />
            </div>

            <div className="grid grid-cols-2 gap-2 text-center">
              <Metric
                label="IV Rank / Percentile"
                value={
                  data.iv_rank == null
                    ? 'n/a'
                    : `${num(data.iv_rank, 0)} / ${num(data.iv_percentile, 0)}`
                }
                sub={
                  data.iv_rank == null
                    ? `needs ${20 - (data.iv_rank_lookback_n ?? 0)} more day(s) of history`
                    : `n=${data.iv_rank_lookback_n} observation(s)`
                }
              />
              <Metric
                label="Vol used for sizing"
                value={data.forecast_vol != null ? `${num(data.forecast_vol)}%` : `${num(data.realized_vol)}%`}
                sub={data.vol_reference === 'trailing_realized' ? 'trailing realized (no forecast)' : (data.vol_reference ?? '')}
              />
            </div>

            <div className="flex items-center gap-2 flex-wrap">
              {data.enter
                ? <Badge variant="green" dot>WOULD ENTER · {data.lots} lot(s)</Badge>
                : <Badge variant="gray">WAITING</Badge>}
              {data.goal_phase && data.goal_scaling_factor != null && data.goal_scaling_factor < 1 && (
                <span title="Sizing reduced by goal-governance risk de-escalation, not by a hard limit">
                  <Badge variant="orange">
                    {data.goal_phase} · size ×{num(data.goal_scaling_factor, 2)}
                  </Badge>
                </span>
              )}
              <span className="text-xs text-ink-faint">{data.reason}</span>
            </div>

            {data.enter && (
              <div className="text-xs text-ink-muted flex flex-wrap gap-4">
                <span>Net credit: <b className="text-brand-green">{num(data.net_credit_pts)} pts</b></span>
                <span>Max loss: <b className="text-brand-red">{num(data.max_loss_pts)} pts</b></span>
                {data.structure && <span>Structure: {data.structure}</span>}
                {data.expiry && <span>Expiry: {data.expiry}{data.dte != null ? ` (${data.dte}d)` : ''}</span>}
              </div>
            )}

            {data.legs.length > 0 && (
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-ink-faint text-left">
                    <th className="py-1">Leg</th><th>Type</th><th>Strike</th>
                    <th className="text-right">Price</th><th className="text-right">Model</th><th className="text-right">Qty</th>
                  </tr>
                </thead>
                <tbody>
                  {data.legs.map((leg) => (
                    <tr key={leg.symbol} className="border-t border-surface-border">
                      <td className="py-1">
                        <Badge variant={leg.side === 'SELL' ? 'yellow' : 'blue'}>
                          {leg.side}{leg.is_wing ? ' · wing' : ''}
                        </Badge>
                      </td>
                      <td>{leg.option_type}</td>
                      <td>{num(leg.strike)}</td>
                      <td className="text-right" title={leg.priced_from === 'market' ? 'Real quoted/last-traded price' : 'No live quote — Black-Scholes theoretical price'}>
                        {num(leg.price)}
                        {leg.priced_from && leg.priced_from !== 'market' && <sup className="text-brand-orange ml-0.5">theo</sup>}
                      </td>
                      <td className="text-right text-ink-faint">
                        {leg.theoretical != null ? num(leg.theoretical) : '—'}
                      </td>
                      <td className="text-right">{leg.quantity}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </CardBody>
    </Card>
  )
}

function Metric({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="bg-surface-inset/50 rounded p-2">
      <div className="text-[10px] uppercase text-ink-faint">{label}</div>
      <div className="text-sm font-semibold text-ink">{value}</div>
      {sub && <div className="text-[10px] text-ink-faint mt-0.5">{sub}</div>}
    </div>
  )
}
