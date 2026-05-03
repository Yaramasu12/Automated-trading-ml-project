import { useEffect, useState } from 'react'
import { Brain, Loader2, Newspaper, Play, RefreshCw } from 'lucide-react'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Badge, regimeBadge } from '../components/shared/Badge'
import { analyzeNews, getCurrentRegime, getNewsEvents, getPerformanceSummary } from '../api'
import { fmtDateTime, num, pct } from '../utils'

interface NewsEvent {
  event_id: string
  headline: string
  source: string
  event_type: string
  recommended_action: string
  global_risk_score: number
  importance_score: number
  sentiment_score: number
  received_at: string
}

interface StrategyQuality {
  strategy_id: string
  profit_factor: number
  sharpe: number
  max_drawdown: number
  win_rate: number
  score: number
  scaling_eligible: boolean
}

export function Intelligence() {
  const [headline, setHeadline] = useState('Breaking RBI policy shock may increase banking volatility')
  const [summary, setSummary] = useState('RBI event risk can affect BANKNIFTY, FINNIFTY, and rate-sensitive sectors.')
  const [newsEvents, setNewsEvents] = useState<NewsEvent[]>([])
  const [newsFeatures, setNewsFeatures] = useState<Record<string, unknown> | null>(null)
  const [regime, setRegime] = useState<Record<string, unknown> | null>(null)
  const [performance, setPerformance] = useState<Record<string, unknown> | null>(null)
  const [loading, setLoading] = useState(false)

  async function refresh() {
    setLoading(true)
    try {
      const [news, current, perf] = await Promise.all([
        getNewsEvents(30),
        getCurrentRegime('NIFTY'),
        getPerformanceSummary(30),
      ])
      setNewsEvents(news.events as NewsEvent[])
      setNewsFeatures(news.features)
      setRegime(current)
      setPerformance(perf)
    } finally {
      setLoading(false)
    }
  }

  async function submitNews() {
    setLoading(true)
    try {
      await analyzeNews({ headline, summary, country: 'IN', source: 'dashboard' })
      await refresh()
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
  }, [])

  const qualityScores = ((performance?.strategy_quality_scores as StrategyQuality[] | undefined) ?? []).slice(0, 8)
  const adjustedRegime = String(regime?.adjusted_regime ?? regime?.regime ?? '—')

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-gray-100">Intelligence</h1>
          <p className="text-xs text-gray-500 mt-0.5">Regime, event risk, and strategy quality</p>
        </div>
        <button
          onClick={refresh}
          disabled={loading}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-surface-elevated border border-surface-border text-xs text-gray-400 hover:text-gray-200 disabled:opacity-50"
        >
          {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
          Refresh
        </button>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="Regime" value={adjustedRegime} />
        <Stat label="News Risk" value={num(Number(newsFeatures?.global_risk_score ?? 0), 3)} />
        <Stat label="Active Events" value={Number(newsFeatures?.active_event_count ?? 0)} />
        <Stat label="Action" value={String(newsFeatures?.recommended_action ?? 'MONITOR')} />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
        <Card>
          <CardHeader title="News Intake" subtitle="Normalized event-risk scoring" icon={<Newspaper size={14} />} />
          <CardBody className="space-y-3">
            <input
              value={headline}
              onChange={(e) => setHeadline(e.target.value)}
              className="w-full bg-surface-elevated border border-surface-border rounded-md px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-brand-blue"
            />
            <textarea
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              rows={3}
              className="w-full bg-surface-elevated border border-surface-border rounded-md px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-brand-blue resize-none"
            />
            <button
              onClick={submitNews}
              disabled={loading}
              className="flex items-center gap-2 px-4 py-1.5 rounded-md bg-brand-blue text-surface text-sm font-medium hover:bg-brand-blue/80 disabled:opacity-50"
            >
              {loading ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
              Analyze
            </button>
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Current Regime" subtitle={String(regime?.symbol ?? 'NIFTY')} icon={<Brain size={14} />} />
          <CardBody className="space-y-4">
            <div className="flex items-center justify-between">
              <span className="text-sm text-gray-400">Adjusted Regime</span>
              {regimeBadge(adjustedRegime)}
            </div>
            <div className="grid grid-cols-2 gap-3">
              {Object.entries((regime?.probabilities as Record<string, number> | undefined) ?? {}).map(([key, value]) => (
                <div key={key} className="rounded-md bg-surface-elevated px-3 py-2">
                  <div className="text-xs text-gray-500">{key}</div>
                  <div className="font-mono text-gray-100">{pct(value * 100)}</div>
                </div>
              ))}
            </div>
          </CardBody>
        </Card>
      </div>

      <Card>
        <CardHeader title="Strategy Quality" subtitle={`${qualityScores.length} ranked strategies`} />
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-surface-border text-gray-500 uppercase tracking-wider">
                <th className="px-4 py-2 text-left">Strategy</th>
                <th className="px-4 py-2 text-right">Score</th>
                <th className="px-4 py-2 text-right">PF</th>
                <th className="px-4 py-2 text-right">Sharpe</th>
                <th className="px-4 py-2 text-right">Drawdown</th>
                <th className="px-4 py-2 text-left">Scaling</th>
              </tr>
            </thead>
            <tbody>
              {qualityScores.map((row) => (
                <tr key={row.strategy_id} className="border-b border-surface-border/40 hover:bg-surface-elevated/40">
                  <td className="px-4 py-2 font-medium text-gray-200">{row.strategy_id}</td>
                  <td className="px-4 py-2 text-right font-mono">{num(row.score, 3)}</td>
                  <td className="px-4 py-2 text-right font-mono">{num(row.profit_factor, 2)}</td>
                  <td className="px-4 py-2 text-right font-mono">{num(row.sharpe, 2)}</td>
                  <td className="px-4 py-2 text-right font-mono">{pct(row.max_drawdown * 100)}</td>
                  <td className="px-4 py-2">
                    <Badge variant={row.scaling_eligible ? 'green' : 'gray'}>
                      {row.scaling_eligible ? 'ELIGIBLE' : 'HOLD'}
                    </Badge>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card>
        <CardHeader title="News Events" subtitle={`${newsEvents.length} recent`} />
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-surface-border text-gray-500 uppercase tracking-wider">
                <th className="px-4 py-2 text-left">Time</th>
                <th className="px-4 py-2 text-left">Headline</th>
                <th className="px-4 py-2 text-left">Type</th>
                <th className="px-4 py-2 text-right">Risk</th>
                <th className="px-4 py-2 text-left">Action</th>
              </tr>
            </thead>
            <tbody>
              {newsEvents.map((event) => (
                <tr key={event.event_id} className="border-b border-surface-border/40 hover:bg-surface-elevated/40">
                  <td className="px-4 py-2 font-mono text-gray-500">{fmtDateTime(event.received_at)}</td>
                  <td className="px-4 py-2 text-gray-300 max-w-md truncate">{event.headline}</td>
                  <td className="px-4 py-2 text-gray-500">{event.event_type}</td>
                  <td className="px-4 py-2 text-right font-mono">{num(event.global_risk_score, 3)}</td>
                  <td className="px-4 py-2">
                    <Badge variant={event.recommended_action === 'BLOCK_ENTRIES' ? 'red' : event.recommended_action === 'MONITOR' ? 'gray' : 'yellow'}>
                      {event.recommended_action}
                    </Badge>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="bg-surface-card border border-surface-border rounded-lg p-4 min-w-0">
      <div className="text-xs text-gray-500 mb-1">{label}</div>
      <div className="text-lg font-bold font-mono text-gray-100 truncate">{value}</div>
    </div>
  )
}
