import { useEffect, useState, useCallback } from 'react'
import {
  Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { Brain, Loader2 } from 'lucide-react'
import { clsx } from 'clsx'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Table } from '../components/shared/Table'
import { Tag } from '../components/shared/Badge'
import { useStore } from '../store'
import { getModelCatalog, getVolatilityForecast, classifyRegime, getSentiment } from '../api'
import { pct, num } from '../utils'

const TOOLTIP_STYLE = {
  backgroundColor: '#161b22',
  border: '1px solid #30363d',
  borderRadius: 6,
  fontSize: 12,
  color: '#e6edf3',
}

const TABS = ['Catalog', 'Volatility', 'Regime', 'Sentiment']

export function Models() {
  const modelCatalog    = useStore((s) => s.modelCatalog)
  const setModelCatalog = useStore((s) => s.setModelCatalog)
  const loading         = useStore((s) => s.loading.models)
  const setLoading      = useStore((s) => s.setLoading)

  const [tab, setTab] = useState('Catalog')

  // Volatility state
  const [volSymbol, setVolSymbol] = useState('NIFTY')
  const [volDays, setVolDays]     = useState(30)
  const [volResult, setVolResult] = useState<Record<string, unknown> | null>(null)
  const [volLoading, setVolLoading] = useState(false)

  // Regime state
  const [regimeSymbol, setRegimeSymbol] = useState('NIFTY')
  const [regimeResult, setRegimeResult] = useState<Record<string, unknown> | null>(null)
  const [regimeLoading, setRegimeLoading] = useState(false)

  // Sentiment state
  const [sentimentText, setSentimentText]   = useState('')
  const [sentimentResult, setSentimentResult] = useState<Record<string, unknown> | null>(null)
  const [sentimentLoading, setSentimentLoading] = useState(false)

  useEffect(() => {
    if (modelCatalog.length === 0) {
      setLoading('models', true)
      getModelCatalog()
        .then(r => setModelCatalog(r.models))
        .catch(() => {})
        .finally(() => setLoading('models', false))
    }
  }, [])

  const runVolatility = useCallback(async () => {
    setVolLoading(true)
    try {
      const res = await getVolatilityForecast({ symbol: volSymbol, days: volDays })
      setVolResult(res as unknown as Record<string, unknown>)
    } catch { /* ignore */ }
    finally { setVolLoading(false) }
  }, [volSymbol, volDays])

  const runRegime = useCallback(async () => {
    setRegimeLoading(true)
    try {
      const res = await classifyRegime({ symbol: regimeSymbol })
      setRegimeResult(res)
    } catch { /* ignore */ }
    finally { setRegimeLoading(false) }
  }, [regimeSymbol])

  const runSentiment = useCallback(async () => {
    if (!sentimentText.trim()) return
    setSentimentLoading(true)
    try {
      const res = await getSentiment({ text: sentimentText })
      setSentimentResult(res)
    } catch { /* ignore */ }
    finally { setSentimentLoading(false) }
  }, [sentimentText])

  const volForecast = volResult?.forecast as { annualized_volatility: number; daily_volatility: number; lower_95: number; upper_95: number } | undefined

  // Build chart data from forecast
  const volChartData = volForecast ? [
    { label: 'Daily', value: volForecast.daily_volatility * 100, low: volForecast.lower_95 * 100, high: volForecast.upper_95 * 100 },
    { label: 'Annualized', value: volForecast.annualized_volatility * 100, low: volForecast.lower_95 * 100, high: volForecast.upper_95 * 100 },
  ] : []

  const regimeProbs = regimeResult?.probabilities as Record<string, number> | undefined

  return (
    <div className="space-y-5">

      {/* Header */}
      <div className="flex items-center gap-2">
        <Brain size={16} className="text-brand-purple" />
        <div>
          <h1 className="text-lg font-bold text-gray-100">ML Models</h1>
          <p className="text-xs text-gray-500 mt-0.5">Volatility forecasting, regime classification, sentiment analysis</p>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-1">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={clsx(
              'px-3 py-1.5 rounded-md text-xs font-medium border transition-colors',
              tab === t
                ? 'bg-brand-purple/15 border-brand-purple/30 text-brand-purple'
                : 'text-gray-400 hover:text-gray-200 hover:bg-surface-elevated border-transparent',
            )}
          >
            {t}
          </button>
        ))}
      </div>

      {/* Tab: Catalog */}
      {tab === 'Catalog' && (
        <Card>
          <CardHeader title="Model Catalog" subtitle={`${modelCatalog.length} models`} icon={<Brain size={14} />} />
          {loading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 size={20} className="animate-spin text-gray-500" />
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 p-4">
              {modelCatalog.map((m) => (
                <div key={m.name} className="bg-surface-elevated border border-surface-border rounded-lg p-3 space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-semibold text-gray-200">{m.name}</span>
                    <Tag
                      label={m.status}
                      color={m.status === 'active' ? 'green' : m.status === 'candidate' ? 'yellow' : 'gray'}
                    />
                  </div>
                  <div className="text-xs text-gray-500">Family: <span className="text-gray-300">{m.family}</span></div>
                  <div className="text-xs text-gray-500 font-mono">{m.promotion_rule}</div>
                </div>
              ))}
              {modelCatalog.length === 0 && (
                <div className="col-span-3 text-center py-8 text-gray-500 text-sm">No models in catalog</div>
              )}
            </div>
          )}
        </Card>
      )}

      {/* Tab: Volatility */}
      {tab === 'Volatility' && (
        <div className="space-y-4">
          <Card>
            <CardHeader title="Volatility Forecast" icon={<Brain size={14} />} />
            <CardBody className="space-y-3">
              <div className="flex flex-wrap items-center gap-3">
                <div>
                  <label className="block text-xs text-gray-400 mb-1 uppercase tracking-wider">Symbol</label>
                  <input
                    value={volSymbol}
                    onChange={(e) => setVolSymbol(e.target.value.toUpperCase())}
                    className="bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-2 py-1.5 font-mono w-28 focus:outline-none focus:border-brand-purple"
                  />
                </div>
                <div>
                  <label className="block text-xs text-gray-400 mb-1 uppercase tracking-wider">Days</label>
                  <div className="flex gap-1">
                    {[7, 14, 30, 60].map((d) => (
                      <button
                        key={d}
                        onClick={() => setVolDays(d)}
                        className={clsx(
                          'px-2 py-1.5 rounded text-xs font-mono border transition-colors',
                          volDays === d
                            ? 'bg-brand-purple/15 border-brand-purple/30 text-brand-purple'
                            : 'bg-surface-elevated border-surface-border text-gray-400 hover:text-gray-200',
                        )}
                      >
                        {d}d
                      </button>
                    ))}
                  </div>
                </div>
                <button
                  onClick={runVolatility}
                  disabled={volLoading}
                  className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-purple/15 border border-brand-purple/30 text-brand-purple text-xs font-medium hover:bg-brand-purple/25 disabled:opacity-50 transition-colors self-end"
                >
                  {volLoading ? <Loader2 size={12} className="animate-spin" /> : <Brain size={12} />}
                  Forecast
                </button>
              </div>

              {volForecast && (
                <>
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mt-2">
                    {[
                      { label: 'Daily Vol', value: `${(volForecast.daily_volatility * 100).toFixed(2)}%` },
                      { label: 'Annual Vol', value: `${(volForecast.annualized_volatility * 100).toFixed(1)}%` },
                      { label: '95% Low', value: `${(volForecast.lower_95 * 100).toFixed(2)}%` },
                      { label: '95% High', value: `${(volForecast.upper_95 * 100).toFixed(2)}%` },
                    ].map((s) => (
                      <div key={s.label} className="bg-surface-elevated rounded-lg p-3">
                        <div className="text-[10px] text-gray-500 uppercase tracking-wider">{s.label}</div>
                        <div className="text-lg font-bold font-mono text-brand-purple mt-1">{s.value}</div>
                      </div>
                    ))}
                  </div>
                  <div className="h-48 mt-2">
                    <ResponsiveContainer width="100%" height="100%">
                      <AreaChart data={volChartData} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                        <defs>
                          <linearGradient id="volGrad" x1="0" y1="0" x2="0" y2="1">
                            <stop offset="5%"  stopColor="#a371f7" stopOpacity={0.3} />
                            <stop offset="95%" stopColor="#a371f7" stopOpacity={0} />
                          </linearGradient>
                        </defs>
                        <CartesianGrid strokeDasharray="3 3" stroke="#30363d" vertical={false} />
                        <XAxis dataKey="label" tick={{ fill: '#6e7681', fontSize: 10 }} axisLine={false} tickLine={false} />
                        <YAxis
                          tickFormatter={(v: number) => `${v.toFixed(1)}%`}
                          tick={{ fill: '#6e7681', fontSize: 10 }}
                          axisLine={false}
                          tickLine={false}
                          width={40}
                        />
                        <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v: number) => [`${v.toFixed(2)}%`]} />
                        <Area type="monotone" dataKey="value" stroke="#a371f7" strokeWidth={2} fill="url(#volGrad)" dot />
                      </AreaChart>
                    </ResponsiveContainer>
                  </div>
                </>
              )}
            </CardBody>
          </Card>
        </div>
      )}

      {/* Tab: Regime */}
      {tab === 'Regime' && (
        <Card>
          <CardHeader title="Regime Classification" icon={<Brain size={14} />} />
          <CardBody className="space-y-4">
            <div className="flex items-center gap-3">
              <input
                value={regimeSymbol}
                onChange={(e) => setRegimeSymbol(e.target.value.toUpperCase())}
                placeholder="Symbol"
                className="bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-2 py-1.5 font-mono w-32 focus:outline-none focus:border-brand-blue"
              />
              <button
                onClick={runRegime}
                disabled={regimeLoading}
                className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-blue/15 border border-brand-blue/30 text-brand-blue text-xs font-medium hover:bg-brand-blue/25 disabled:opacity-50 transition-colors"
              >
                {regimeLoading ? <Loader2 size={12} className="animate-spin" /> : <Brain size={12} />}
                Classify Regime
              </button>
            </div>

            {regimeResult && (
              <div className="space-y-4">
                <div className="flex items-center gap-3">
                  <span className="text-sm text-gray-400">Current Regime:</span>
                  <span className={clsx(
                    'text-lg font-bold font-mono',
                    String(regimeResult.regime).toLowerCase().includes('bull') ? 'text-brand-green'
                    : String(regimeResult.regime).toLowerCase().includes('bear') ? 'text-brand-red'
                    : String(regimeResult.regime).toLowerCase().includes('vol') ? 'text-brand-orange'
                    : 'text-gray-200',
                  )}>
                    {String(regimeResult.regime)}
                  </span>
                </div>
                {regimeProbs && (
                  <div className="space-y-2">
                    <div className="text-xs text-gray-400 uppercase tracking-wider">Probability Breakdown</div>
                    {Object.entries(regimeProbs)
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

      {/* Tab: Sentiment */}
      {tab === 'Sentiment' && (
        <Card>
          <CardHeader title="Sentiment Analysis" icon={<Brain size={14} />} />
          <CardBody className="space-y-4">
            <div>
              <label className="block text-xs text-gray-400 mb-2 uppercase tracking-wider">News Text</label>
              <textarea
                value={sentimentText}
                onChange={(e) => setSentimentText(e.target.value)}
                rows={4}
                placeholder="Enter news headline or article text..."
                className="w-full bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded px-3 py-2 font-mono focus:outline-none focus:border-brand-blue resize-none"
              />
            </div>
            <button
              onClick={runSentiment}
              disabled={sentimentLoading || !sentimentText.trim()}
              className="flex items-center gap-2 px-4 py-2 rounded-md bg-brand-yellow/15 border border-brand-yellow/30 text-brand-yellow text-xs font-medium hover:bg-brand-yellow/25 disabled:opacity-50 transition-colors"
            >
              {sentimentLoading ? <Loader2 size={12} className="animate-spin" /> : <Brain size={12} />}
              Analyze Sentiment
            </button>

            {sentimentResult && (
              <div className="space-y-3">
                <div className="flex items-center gap-3">
                  <Tag
                    label={String(sentimentResult.sentiment ?? sentimentResult.label ?? 'NEUTRAL')}
                    color={
                      String(sentimentResult.sentiment ?? '').includes('POS') ? 'green'
                      : String(sentimentResult.sentiment ?? '').includes('NEG') ? 'red'
                      : 'gray'
                    }
                  />
                  {sentimentResult.score != null && (
                    <span className="text-sm font-mono font-bold text-gray-200">
                      Score: {Number(sentimentResult.score).toFixed(3)}
                    </span>
                  )}
                </div>
                {Array.isArray(sentimentResult.entities) && (
                  <div>
                    <div className="text-xs text-gray-400 mb-1">Entities</div>
                    <div className="flex flex-wrap gap-1.5">
                      {(sentimentResult.entities as string[]).map((e, i) => (
                        <Tag key={i} label={e} color="blue" />
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </CardBody>
        </Card>
      )}

    </div>
  )
}
