import { useEffect, useState, useCallback } from 'react'
import { Newspaper, Loader2, Brain, Calendar } from 'lucide-react'
import { clsx } from 'clsx'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Table } from '../components/shared/Table'
import { Tag, regimeBadge } from '../components/shared/Badge'
import { analyzeNews, getCurrentRegime, getNewsEvents, getEconomicCalendar } from '../api'
import { fmtDateTime } from '../utils'

const TABS = ['News', 'Regime', 'Calendar']
const REGIME_SYMBOLS = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'SENSEX']

export function Intelligence() {
  const [tab, setTab] = useState('News')

  // News state
  const [headline, setHeadline]         = useState('')
  const [newsResult, setNewsResult]     = useState<Record<string, unknown> | null>(null)
  const [newsLoading, setNewsLoading]   = useState(false)
  const [newsEvents, setNewsEvents]     = useState<Record<string, unknown>[]>([])
  const [eventsLoading, setEventsLoading] = useState(false)

  // Regime state
  const [regimeSymbol, setRegimeSymbol]     = useState('NIFTY')
  const [regimeResult, setRegimeResult]     = useState<Record<string, unknown> | null>(null)
  const [regimeLoading, setRegimeLoading]   = useState(false)

  // Calendar state
  const [calendarEvents, setCalendarEvents] = useState<Record<string, unknown>[]>([])
  const [calendarLoading, setCalendarLoading] = useState(false)

  useEffect(() => {
    setEventsLoading(true)
    getNewsEvents(30)
      .then(r => {
        const v = r as { events?: Record<string, unknown>[] }
        setNewsEvents(v.events ?? [])
      })
      .catch(() => {})
      .finally(() => setEventsLoading(false))
  }, [])

  useEffect(() => {
    if (tab === 'Calendar' && calendarEvents.length === 0) {
      setCalendarLoading(true)
      getEconomicCalendar()
        .then(r => {
          const v = r as { events?: Record<string, unknown>[] }
          setCalendarEvents(v.events ?? [])
        })
        .catch(() => {})
        .finally(() => setCalendarLoading(false))
    }
  }, [tab])

  const runNewsAnalysis = useCallback(async () => {
    if (!headline.trim()) return
    setNewsLoading(true)
    try {
      const res = await analyzeNews({ text: headline })
      setNewsResult(res)
    } catch { /* ignore */ }
    finally { setNewsLoading(false) }
  }, [headline])

  const runRegime = useCallback(async () => {
    setRegimeLoading(true)
    try {
      const res = await getCurrentRegime(regimeSymbol)
      setRegimeResult(res)
    } catch { /* ignore */ }
    finally { setRegimeLoading(false) }
  }, [regimeSymbol])

  return (
    <div className="space-y-5">

      {/* Header */}
      <div className="flex items-center gap-2">
        <Newspaper size={16} className="text-brand-yellow" />
        <div>
          <h1 className="text-lg font-bold text-gray-100">Market Intelligence</h1>
          <p className="text-xs text-gray-500 mt-0.5">News analysis, regime detection, economic calendar</p>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-1">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={clsx(
              'flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium border transition-colors',
              tab === t
                ? 'bg-brand-yellow/15 border-brand-yellow/30 text-brand-yellow'
                : 'text-gray-400 hover:text-gray-200 hover:bg-surface-elevated border-transparent',
            )}
          >
            {t === 'News'     && <Newspaper size={12} />}
            {t === 'Regime'   && <Brain size={12} />}
            {t === 'Calendar' && <Calendar size={12} />}
            {t}
          </button>
        ))}
      </div>

      {/* Tab: News */}
      {tab === 'News' && (
        <div className="space-y-4">
          <Card>
            <CardHeader title="News Sentiment Analysis" icon={<Newspaper size={14} />} />
            <CardBody className="space-y-3">
              <div>
                <label className="block text-xs text-gray-400 mb-2 uppercase tracking-wider">Headline / Article Text</label>
                <textarea
                  value={headline}
                  onChange={(e) => setHeadline(e.target.value)}
                  rows={3}
                  placeholder="Enter news headline or article..."
                  className="w-full bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-3 py-2 font-mono focus:outline-none focus:border-brand-yellow resize-none"
                />
              </div>
              <button
                onClick={runNewsAnalysis}
                disabled={newsLoading || !headline.trim()}
                className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-yellow/15 border border-brand-yellow/30 text-brand-yellow text-xs font-medium hover:bg-brand-yellow/25 disabled:opacity-50 transition-colors"
              >
                {newsLoading ? <Loader2 size={12} className="animate-spin" /> : <Newspaper size={12} />}
                Analyze
              </button>

              {newsResult && (
                <div className="mt-3 space-y-3 border-t border-surface-border pt-3">
                  <div className="flex items-center gap-3 flex-wrap">
                    <Tag
                      label={String(newsResult.sentiment ?? newsResult.label ?? 'NEUTRAL')}
                      color={
                        String(newsResult.sentiment ?? '').includes('POS') ? 'green'
                        : String(newsResult.sentiment ?? '').includes('NEG') ? 'red'
                        : 'gray'
                      }
                    />
                    {newsResult.score != null && (
                      <span className="text-sm font-mono text-gray-200">
                        Score: <strong>{Number(newsResult.score).toFixed(3)}</strong>
                      </span>
                    )}
                  </div>
                  {Boolean(newsResult.features) && (
                    <div>
                      <div className="text-xs text-gray-400 mb-1">Features</div>
                      <div className="grid grid-cols-2 gap-2">
                        {Object.entries(newsResult.features as Record<string, unknown>).slice(0, 6).map(([k, v]) => (
                          <div key={k} className="text-xs bg-surface-elevated rounded p-1.5">
                            <span className="text-gray-500">{k}: </span>
                            <span className="text-gray-200 font-mono">{String(v)}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                  {Array.isArray(newsResult.entities) && newsResult.entities.length > 0 && (
                    <div>
                      <div className="text-xs text-gray-400 mb-1">Entities</div>
                      <div className="flex flex-wrap gap-1.5">
                        {(newsResult.entities as string[]).map((e, i) => (
                          <Tag key={i} label={e} color="blue" />
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}
            </CardBody>
          </Card>

          <Card>
            <CardHeader title="Recent News Events" subtitle={`${newsEvents.length} events`} />
            {eventsLoading ? (
              <div className="flex items-center justify-center py-8">
                <Loader2 size={16} className="animate-spin text-gray-500" />
              </div>
            ) : (
              <Table
                columns={[
                  {
                    key: 'timestamp', label: 'Time',
                    render: (v) => <span className="text-xs text-gray-500">{fmtDateTime(String(v))}</span>,
                  },
                  { key: 'headline', label: 'Headline' },
                  {
                    key: 'sentiment', label: 'Sentiment',
                    render: (v) => v ? (
                      <Tag
                        label={String(v)}
                        color={
                          String(v).includes('POS') ? 'green'
                          : String(v).includes('NEG') ? 'red'
                          : 'gray'
                        }
                      />
                    ) : <span className="text-gray-600">—</span>,
                  },
                ]}
                rows={newsEvents.slice(0, 30)}
                keyFn={(r) => String(r.id ?? r.timestamp ?? Math.random())}
                emptyMessage="No news events"
                compact
              />
            )}
          </Card>
        </div>
      )}

      {/* Tab: Regime */}
      {tab === 'Regime' && (
        <Card>
          <CardHeader title="Regime Detection" icon={<Brain size={14} />} />
          <CardBody className="space-y-4">
            <div className="flex flex-wrap items-center gap-3">
              <span className="text-xs text-gray-400">Symbol:</span>
              {REGIME_SYMBOLS.map((s) => (
                <button
                  key={s}
                  onClick={() => setRegimeSymbol(s)}
                  className={clsx(
                    'px-2.5 py-1 rounded text-xs font-mono border transition-colors',
                    regimeSymbol === s
                      ? 'bg-brand-blue/15 border-brand-blue/30 text-brand-blue'
                      : 'bg-surface-elevated border-surface-border text-gray-400 hover:text-gray-200',
                  )}
                >
                  {s}
                </button>
              ))}
              <button
                onClick={runRegime}
                disabled={regimeLoading}
                className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-blue/15 border border-brand-blue/30 text-brand-blue text-xs font-medium hover:bg-brand-blue/25 disabled:opacity-50 transition-colors"
              >
                {regimeLoading ? <Loader2 size={12} className="animate-spin" /> : <Brain size={12} />}
                Detect Regime
              </button>
            </div>

            {regimeResult && (
              <div className="space-y-4 pt-3 border-t border-surface-border">
                <div className="flex items-center gap-3">
                  <span className="text-sm text-gray-400">Current Regime:</span>
                  {regimeBadge(String(regimeResult.regime ?? 'UNKNOWN'))}
                </div>

                {Boolean(regimeResult.probabilities) && (
                  <div className="space-y-2">
                    <div className="text-xs text-gray-400 uppercase tracking-wider">Probability Breakdown</div>
                    {Object.entries(regimeResult.probabilities as Record<string, number>)
                      .sort(([, a], [, b]) => b - a)
                      .map(([regime, prob]) => (
                        <div key={regime} className="space-y-1">
                          <div className="flex items-center justify-between text-xs">
                            <span className="text-gray-300">{regime}</span>
                            <span className="font-mono text-gray-400">{(prob * 100).toFixed(1)}%</span>
                          </div>
                          <div className="h-1.5 bg-surface-elevated rounded-full overflow-hidden">
                            <div
                              className={clsx(
                                'h-full rounded-full transition-all',
                                regime.toLowerCase().includes('bull') ? 'bg-brand-green'
                                : regime.toLowerCase().includes('bear') ? 'bg-brand-red'
                                : 'bg-brand-blue',
                              )}
                              style={{ width: `${prob * 100}%` }}
                            />
                          </div>
                        </div>
                      ))}
                  </div>
                )}
              </div>
            )}
          </CardBody>
        </Card>
      )}

      {/* Tab: Calendar */}
      {tab === 'Calendar' && (
        <Card>
          <CardHeader title="Economic Calendar" subtitle={`${calendarEvents.length} events`} icon={<Calendar size={14} />} />
          {calendarLoading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 size={16} className="animate-spin text-gray-500" />
            </div>
          ) : (
            <Table
              columns={[
                {
                  key: 'date', label: 'Date',
                  render: (v) => <span className="text-xs font-mono text-gray-300">{String(v)}</span>,
                },
                { key: 'event', label: 'Event' },
                { key: 'country', label: 'Country' },
                {
                  key: 'impact', label: 'Impact',
                  render: (v) => (
                    <Tag
                      label={String(v ?? 'LOW')}
                      color={
                        String(v) === 'HIGH' ? 'red'
                        : String(v) === 'MEDIUM' ? 'yellow'
                        : 'gray'
                      }
                    />
                  ),
                },
                { key: 'forecast', label: 'Forecast' },
                { key: 'previous', label: 'Previous' },
              ]}
              rows={calendarEvents.slice(0, 50)}
              keyFn={(r) => String(r.id ?? r.event ?? Math.random())}
              emptyMessage="No calendar events"
              compact
            />
          )}
        </Card>
      )}

    </div>
  )
}
