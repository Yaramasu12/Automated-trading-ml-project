import { useEffect, useState } from 'react'
import {
  Line,
  LineChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  RadarChart,
  PolarGrid,
  PolarAngleAxis,
  Radar,
} from 'recharts'
import { Brain, Loader2, Play, RefreshCw } from 'lucide-react'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Badge, statusBadge } from '../components/shared/Badge'
import { useStore } from '../store'
import { classifyRegime, getGarchForecast, getModelCatalog, getVolatilityForecast, runWalkForward } from '../api'
import { pct, num } from '../utils'
import type { ModelCatalogEntry, WalkForwardResult } from '../types'

const TOOLTIP_STYLE = {
  backgroundColor: '#161b22',
  border: '1px solid #30363d',
  borderRadius: 6,
  fontSize: 12,
  color: '#e6edf3',
}

export function Models() {
  const modelCatalog = useStore((s) => s.modelCatalog)
  const setModelCatalog = useStore((s) => s.setModelCatalog)
  const walkForwardResult = useStore((s) => s.walkForwardResult)
  const setWalkForwardResult = useStore((s) => s.setWalkForwardResult)
  const loading = useStore((s) => s.loading)
  const setLoading = useStore((s) => s.setLoading)

  const [volForecast, setVolForecast] = useState<{ annualized_volatility: number; daily_volatility: number; lower_95: number; upper_95: number; sample_size: number } | null>(null)
  const [garchParams, setGarchParams] = useState<Record<string, number> | null>(null)
  const [regime, setRegime] = useState<{ regime: string; probabilities: Record<string, number> } | null>(null)

  const [volSymbol, setVolSymbol] = useState('NIFTY')
  const [wfStrategy, setWfStrategy] = useState('iron_condor')
  const [wfDays, setWfDays] = useState('60')
  const [loadingGarch, setLoadingGarch] = useState(false)
  const [loadingRegime, setLoadingRegime] = useState(false)

  useEffect(() => {
    getModelCatalog()
      .then((r) => setModelCatalog(r.models))
      .catch(() => {})
  }, [])

  async function fetchVolatility() {
    setLoadingGarch(true)
    try {
      const [vol, garch, reg] = await Promise.allSettled([
        getVolatilityForecast({ symbol: volSymbol }),
        getGarchForecast({ symbol: volSymbol }),
        classifyRegime({ symbol: volSymbol }),
      ])
      if (vol.status === 'fulfilled') setVolForecast(vol.value.forecast)
      if (garch.status === 'fulfilled') setGarchParams(garch.value.garch_params)
      if (reg.status === 'fulfilled') setRegime({ regime: reg.value.regime, probabilities: reg.value.probabilities })
    } finally {
      setLoadingGarch(false)
    }
  }

  async function runWF() {
    setLoading('walkForward', true)
    try {
      const result = await runWalkForward({
        strategy_name: wfStrategy,
        total_days: parseInt(wfDays, 10),
        underlyings: ['NIFTY'],
      })
      setWalkForwardResult(result)
    } catch (e) {
      // ignore
    } finally {
      setLoading('walkForward', false)
    }
  }

  const wfChartData = walkForwardResult?.windows.map((w) => ({
    window: `W${w.window_index + 1}`,
    train_sharpe: parseFloat(w.train_metrics.sharpe_like.toFixed(2)),
    test_sharpe: parseFloat(w.test_metrics.sharpe_like.toFixed(2)),
    test_return: parseFloat(w.test_metrics.return_pct.toFixed(2)),
  })) ?? []

  const regimeProbData = regime
    ? Object.entries(regime.probabilities).map(([k, v]) => ({ regime: k, probability: parseFloat((v * 100).toFixed(1)) }))
    : []

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-lg font-bold text-gray-100">AI Models</h1>
        <p className="text-xs text-gray-500 mt-0.5">Volatility forecasting, regime classification, walk-forward analysis</p>
      </div>

      {/* Model catalog */}
      <Card>
        <CardHeader
          title="Model Catalog"
          subtitle={`${modelCatalog.length} models`}
          icon={<Brain size={14} />}
          action={
            <button onClick={() => getModelCatalog().then((r) => setModelCatalog(r.models)).catch(() => {})} className="text-gray-500 hover:text-gray-300">
              <RefreshCw size={12} />
            </button>
          }
        />
        {modelCatalog.length === 0 ? (
          <div className="text-center py-6 text-gray-600 text-sm">No models loaded</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-surface-border text-gray-500 uppercase tracking-wider">
                  <th className="px-4 py-2 text-left">Name</th>
                  <th className="px-4 py-2 text-left">Family</th>
                  <th className="px-4 py-2 text-left">Status</th>
                  <th className="px-4 py-2 text-left">Promotion Rule</th>
                </tr>
              </thead>
              <tbody>
                {modelCatalog.map((m: ModelCatalogEntry) => (
                  <tr key={m.name} className="border-b border-surface-border/40 hover:bg-surface-elevated/40">
                    <td className="px-4 py-2 font-mono font-medium text-gray-200">{m.name}</td>
                    <td className="px-4 py-2 text-gray-400">{m.family}</td>
                    <td className="px-4 py-2">{statusBadge(m.status)}</td>
                    <td className="px-4 py-2 text-gray-500">{m.promotion_rule}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Volatility & regime analysis */}
      <Card>
        <CardHeader title="Volatility & Regime Analysis" />
        <CardBody className="flex flex-wrap items-end gap-4">
          <div>
            <label className="block text-xs text-gray-500 mb-1">Symbol</label>
            <input
              value={volSymbol}
              onChange={(e) => setVolSymbol(e.target.value)}
              className="w-36 bg-surface-elevated border border-surface-border rounded-md px-3 py-1.5 text-sm text-gray-200 font-mono focus:outline-none focus:border-brand-blue"
            />
          </div>
          <button
            onClick={fetchVolatility}
            disabled={loadingGarch}
            className="flex items-center gap-2 px-4 py-1.5 rounded-md bg-brand-purple/20 border border-brand-purple/40 text-brand-purple text-sm font-medium hover:bg-brand-purple/30 disabled:opacity-50 transition-colors"
          >
            {loadingGarch ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
            Analyze
          </button>
        </CardBody>
      </Card>

      {(volForecast || garchParams || regime) && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
          {/* Vol forecast */}
          {volForecast && (
            <Card>
              <CardHeader title="Volatility Forecast" subtitle={volSymbol} />
              <CardBody className="space-y-3">
                <div className="text-center">
                  <div className="text-3xl font-bold font-mono text-brand-cyan">
                    {pct(volForecast.annualized_volatility * 100)}
                  </div>
                  <div className="text-xs text-gray-500 mt-1">Annualized Vol</div>
                </div>
                <div className="grid grid-cols-2 gap-2 text-xs">
                  <div className="bg-surface-elevated rounded p-2">
                    <div className="text-gray-500">Daily</div>
                    <div className="font-mono text-gray-200">{pct(volForecast.daily_volatility * 100)}</div>
                  </div>
                  <div className="bg-surface-elevated rounded p-2">
                    <div className="text-gray-500">Sample</div>
                    <div className="font-mono text-gray-200">{volForecast.sample_size}d</div>
                  </div>
                  <div className="bg-surface-elevated rounded p-2">
                    <div className="text-gray-500">Lower 95%</div>
                    <div className="font-mono text-gray-200">{pct(volForecast.lower_95 * 100)}</div>
                  </div>
                  <div className="bg-surface-elevated rounded p-2">
                    <div className="text-gray-500">Upper 95%</div>
                    <div className="font-mono text-gray-200">{pct(volForecast.upper_95 * 100)}</div>
                  </div>
                </div>
              </CardBody>
            </Card>
          )}

          {/* GARCH params */}
          {garchParams && (
            <Card>
              <CardHeader title="GARCH(1,1) Parameters" subtitle="Fitted model" />
              <CardBody className="space-y-2">
                {Object.entries(garchParams).map(([k, v]) => (
                  <div key={k} className="flex items-center justify-between py-1.5 border-b border-surface-border/50">
                    <span className="text-sm text-gray-400 font-mono">{k}</span>
                    <span className="font-mono font-bold text-brand-yellow">{num(v, 4)}</span>
                  </div>
                ))}
                {garchParams['alpha'] !== undefined && garchParams['beta'] !== undefined && (
                  <div className="flex items-center justify-between py-1.5 mt-1">
                    <span className="text-xs text-gray-500">Persistence (α+β)</span>
                    <span className={`font-mono text-sm font-bold ${(garchParams['alpha'] + garchParams['beta']) > 0.98 ? 'text-brand-red' : 'text-brand-green'}`}>
                      {num(garchParams['alpha'] + garchParams['beta'], 4)}
                    </span>
                  </div>
                )}
              </CardBody>
            </Card>
          )}

          {/* Regime */}
          {regime && (
            <Card>
              <CardHeader title="Market Regime" subtitle={volSymbol} />
              <CardBody>
                <div className="text-center mb-4">
                  <Badge variant="purple" className="text-sm px-3 py-1">{regime.regime}</Badge>
                </div>
                <div className="h-40">
                  <ResponsiveContainer width="100%" height="100%">
                    <RadarChart data={regimeProbData}>
                      <PolarGrid stroke="#30363d" />
                      <PolarAngleAxis dataKey="regime" tick={{ fill: '#6e7681', fontSize: 9 }} />
                      <Radar dataKey="probability" stroke="#a371f7" fill="#a371f7" fillOpacity={0.3} />
                    </RadarChart>
                  </ResponsiveContainer>
                </div>
                <div className="space-y-1 mt-2">
                  {regimeProbData.map((r) => (
                    <div key={r.regime} className="flex items-center justify-between text-xs">
                      <span className="text-gray-500">{r.regime}</span>
                      <div className="flex items-center gap-2">
                        <div className="w-20 h-1.5 bg-surface-elevated rounded-full overflow-hidden">
                          <div className="h-full bg-brand-purple rounded-full" style={{ width: `${r.probability}%` }} />
                        </div>
                        <span className="font-mono text-gray-300 w-10 text-right">{r.probability.toFixed(1)}%</span>
                      </div>
                    </div>
                  ))}
                </div>
              </CardBody>
            </Card>
          )}
        </div>
      )}

      {/* Walk-forward */}
      <Card>
        <CardHeader title="Walk-Forward Analysis" subtitle="Strategy robustness test" />
        <CardBody className="flex flex-wrap items-end gap-4">
          <div>
            <label className="block text-xs text-gray-500 mb-1">Strategy</label>
            <input
              value={wfStrategy}
              onChange={(e) => setWfStrategy(e.target.value)}
              className="w-40 bg-surface-elevated border border-surface-border rounded-md px-3 py-1.5 text-sm text-gray-200 font-mono focus:outline-none focus:border-brand-blue"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Total Days</label>
            <input
              type="number"
              value={wfDays}
              onChange={(e) => setWfDays(e.target.value)}
              className="w-20 bg-surface-elevated border border-surface-border rounded-md px-3 py-1.5 text-sm text-gray-200 font-mono focus:outline-none focus:border-brand-blue"
            />
          </div>
          <button
            onClick={runWF}
            disabled={loading.walkForward}
            className="flex items-center gap-2 px-4 py-1.5 rounded-md bg-brand-cyan/20 border border-brand-cyan/40 text-brand-cyan text-sm font-medium hover:bg-brand-cyan/30 disabled:opacity-50 transition-colors"
          >
            {loading.walkForward ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
            Run Walk-Forward
          </button>
        </CardBody>
      </Card>

      {walkForwardResult && (
        <Card>
          <CardHeader
            title={`Walk-Forward: ${walkForwardResult.strategy_name}`}
            subtitle={`${walkForwardResult.window_count} windows · ${walkForwardResult.total_days}d`}
            action={
              <Badge variant={walkForwardResult.degradation_detected ? 'red' : 'green'}>
                {walkForwardResult.degradation_detected ? 'DEGRADED' : 'ROBUST'}
              </Badge>
            }
          />
          <div className="grid grid-cols-3 border-b border-surface-border">
            <div className="p-4 text-center border-r border-surface-border">
              <div className="text-xl font-bold font-mono text-brand-cyan">{walkForwardResult.mean_test_sharpe.toFixed(2)}</div>
              <div className="text-xs text-gray-500 mt-1">Mean Test Sharpe</div>
            </div>
            <div className="p-4 text-center border-r border-surface-border">
              <div className={`text-xl font-bold font-mono ${walkForwardResult.mean_test_return >= 0 ? 'text-brand-green' : 'text-brand-red'}`}>
                {pct(walkForwardResult.mean_test_return)}
              </div>
              <div className="text-xs text-gray-500 mt-1">Mean Test Return</div>
            </div>
            <div className="p-4 text-center">
              <div className={`text-xl font-bold font-mono ${walkForwardResult.degradation_detected ? 'text-brand-red' : 'text-brand-green'}`}>
                {walkForwardResult.degradation_detected ? 'YES' : 'NO'}
              </div>
              <div className="text-xs text-gray-500 mt-1">Degradation</div>
            </div>
          </div>
          <CardBody className="h-48 !p-2">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={wfChartData} margin={{ top: 4, right: 16, bottom: 0, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#30363d" />
                <XAxis dataKey="window" tick={{ fill: '#6e7681', fontSize: 10 }} axisLine={false} tickLine={false} />
                <YAxis tick={{ fill: '#6e7681', fontSize: 10 }} axisLine={false} tickLine={false} />
                <Tooltip contentStyle={TOOLTIP_STYLE} />
                <Line type="monotone" dataKey="train_sharpe" stroke="#58a6ff" strokeWidth={2} dot={{ r: 3 }} name="Train Sharpe" />
                <Line type="monotone" dataKey="test_sharpe" stroke="#3fb950" strokeWidth={2} dot={{ r: 3 }} name="Test Sharpe" strokeDasharray="4 2" />
              </LineChart>
            </ResponsiveContainer>
          </CardBody>
        </Card>
      )}
    </div>
  )
}
