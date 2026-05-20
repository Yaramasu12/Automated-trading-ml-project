import { useCallback, useEffect, useMemo, useState } from 'react'
import { clsx } from 'clsx'
import { BrainCircuit, Loader2, RefreshCw, Sparkles } from 'lucide-react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { getNeuralStatus, runNeuralPredictPreview } from '../api'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { KeyValueGrid, PageHeader, ProgressBar, StatTile } from '../components/shared/Command'
import { Table } from '../components/shared/Table'
import { Tag } from '../components/shared/Badge'
import { useStore } from '../store'
import { pct } from '../utils'
import type { ForecastPrediction } from '../types'

const TOOLTIP_STYLE = {
  backgroundColor: '#161b22',
  border: '1px solid #30363d',
  borderRadius: 6,
  fontSize: 12,
  color: '#e6edf3',
}

export function NeuralLab() {
  const status = useStore((s) => s.neuralStatus)
  const latest = useStore((s) => s.latestNeuralBundle)
  const setStatus = useStore((s) => s.setNeuralStatus)
  const setLatest = useStore((s) => s.setLatestNeuralBundle)
  const setError = useStore((s) => s.setError)

  const [symbols, setSymbols] = useState('NIFTY,BANKNIFTY,RELIANCE,TCS')
  const [loading, setLoading] = useState(false)
  const [predicting, setPredicting] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setStatus(await getNeuralStatus())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load Neural Lab')
    } finally {
      setLoading(false)
    }
  }, [setError, setStatus])

  useEffect(() => { load() }, [load])

  const runPreview = async () => {
    setPredicting(true)
    try {
      const syms = symbols.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean)
      setLatest(await runNeuralPredictPreview({ symbols: syms }))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Neural prediction failed')
    } finally {
      setPredicting(false)
    }
  }

  const chartData = useMemo(() => (latest?.forecasts ?? []).map((f) => ({
    symbol: f.symbol,
    direction: Number((f.direction_probability * 100).toFixed(1)),
    expected: Number((f.expected_return * 100).toFixed(2)),
    uncertainty: Number((f.model_uncertainty * 100).toFixed(1)),
  })), [latest])

  const shouldTrade = latest?.should_trade ?? ((latest?.overall_uncertainty ?? 1) < 0.65)
  const uncertainty = latest?.overall_uncertainty ?? 0
  const modelItems = status?.models
    ? Object.entries(status.models).map(([label, value]) => ({ label: label.replace(/_/g, ' '), value, accent: 'blue' as const }))
    : []

  return (
    <div className="space-y-5">
      <PageHeader
        title="Neural Lab"
        subtitle="Forecast ensemble, volatility, tail-risk, and graph correlation previews"
        icon={<BrainCircuit size={17} className="text-brand-blue" />}
        action={
          <button
            onClick={load}
            disabled={loading}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-surface-elevated border border-surface-border text-xs text-gray-400 hover:text-gray-200 disabled:opacity-50"
          >
            {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
            Refresh
          </button>
        }
      />

      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
        <StatTile label="Lab State" value={status?.enabled ? 'ENABLED' : 'DISABLED'} accent={status?.enabled ? 'green' : 'gray'} sub="advisory until validated" />
        <StatTile label="Uncertainty" value={latest ? pct(uncertainty * 100, 1) : '--'} accent={uncertainty < 0.45 ? 'green' : uncertainty < 0.7 ? 'yellow' : 'red'} sub={latest?.trace_id?.slice(0, 18) ?? 'no prediction yet'} />
        <StatTile label="Trade Gate" value={latest ? (shouldTrade ? 'PASS' : 'SKIP') : '--'} accent={latest ? (shouldTrade ? 'green' : 'red') : 'gray'} sub="uncertainty-aware" />
        <StatTile label="Forecasts" value={latest?.forecasts?.length ?? 0} accent="cyan" sub="symbols in bundle" />
      </div>

      <Card className="border-brand-blue/20">
        <CardHeader title="Prediction Preview" subtitle="Runs in-process baseline models from the backend" icon={<Sparkles size={14} />} />
        <CardBody className="space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-[1fr_auto] gap-3">
            <label className="space-y-1">
              <span className="text-xs text-gray-400 uppercase tracking-wider">Symbols</span>
              <input
                value={symbols}
                onChange={(e) => setSymbols(e.target.value)}
                className="w-full bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded-md px-3 py-2 font-mono focus:outline-none focus:border-brand-blue"
              />
            </label>
            <button
              onClick={runPreview}
              disabled={predicting}
              className="self-end flex items-center justify-center gap-2 px-4 py-2 rounded-md bg-brand-blue/15 border border-brand-blue/30 text-brand-blue text-xs font-semibold hover:bg-brand-blue/25 disabled:opacity-50"
            >
              {predicting ? <Loader2 size={12} className="animate-spin" /> : <BrainCircuit size={12} />}
              Run Prediction
            </button>
          </div>

          {latest && (
            <div className="grid grid-cols-1 xl:grid-cols-[1.2fr_.8fr] gap-5 pt-3 border-t border-surface-border">
              <div className="h-72 min-w-0">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={chartData}>
                    <CartesianGrid stroke="#30363d" strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="symbol" tick={{ fill: '#8b949e', fontSize: 11 }} axisLine={false} tickLine={false} />
                    <YAxis tick={{ fill: '#8b949e', fontSize: 11 }} axisLine={false} tickLine={false} />
                    <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v: number, n: string) => [`${v.toFixed(2)}%`, n]} />
                    <Bar dataKey="direction" fill="#58a6ff" radius={[4, 4, 0, 0]} name="P(up)" />
                    <Bar dataKey="uncertainty" fill="#d29922" radius={[4, 4, 0, 0]} name="Uncertainty" />
                  </BarChart>
                </ResponsiveContainer>
              </div>

              <div className="space-y-4">
                <ProgressBar
                  label="Overall uncertainty"
                  value={uncertainty * 100}
                  accent={uncertainty < 0.45 ? 'green' : uncertainty < 0.7 ? 'yellow' : 'red'}
                  right={pct(uncertainty * 100, 1)}
                />
                {latest.correlation_risk && (
                  <KeyValueGrid
                    columns="grid-cols-2"
                    items={[
                      { label: 'Max correlation', value: pct(latest.correlation_risk.max_pairwise_correlation * 100, 1), accent: 'orange' },
                      { label: 'Contagion risk', value: pct(latest.correlation_risk.contagion_risk_score * 100, 1), accent: 'red' },
                      { label: 'Model', value: latest.correlation_risk.model_id, accent: 'blue' },
                      { label: 'Symbols', value: latest.correlation_risk.symbols.length, accent: 'cyan' },
                    ]}
                  />
                )}
                {latest.model_versions && Object.keys(latest.model_versions).length > 0 && (
                  <div className="flex flex-wrap gap-1.5">
                    {Object.entries(latest.model_versions).map(([k, v]) => <Tag key={k} label={`${k}:${v}`} color="blue" />)}
                  </div>
                )}
              </div>
            </div>
          )}
        </CardBody>
      </Card>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
        <Card>
          <CardHeader title="Forecast Bundle" subtitle={`${latest?.forecasts?.length ?? 0} rows`} />
          <Table<ForecastPrediction>
            columns={[
              { key: 'symbol', label: 'Symbol', render: (v) => <span className="text-brand-blue font-semibold">{String(v)}</span> },
              { key: 'direction_probability', label: 'P(up)', align: 'right', render: (v) => <span className={clsx(Number(v) > 0.6 ? 'text-brand-green' : Number(v) < 0.4 ? 'text-brand-red' : 'text-brand-yellow')}>{pct(Number(v) * 100, 1)}</span> },
              { key: 'expected_return', label: 'Expected', align: 'right', render: (v) => <span className={clsx(Number(v) >= 0 ? 'text-brand-green' : 'text-brand-red')}>{pct(Number(v) * 100, 2)}</span> },
              { key: 'confidence', label: 'Conf', align: 'right', render: (v) => pct(Number(v) * 100, 0) },
              { key: 'model_id', label: 'Model' },
            ]}
            rows={latest?.forecasts ?? []}
            keyFn={(row) => row.symbol}
            emptyMessage="Run a prediction to populate forecasts"
            compact
          />
        </Card>

        <Card>
          <CardHeader title="Model Stack" subtitle="Backend advertised serving models" />
          <CardBody className="space-y-4">
            {modelItems.length > 0 ? <KeyValueGrid items={modelItems} columns="grid-cols-2" /> : (
              <div className="text-xs text-gray-500 text-center py-6">No neural model status yet</div>
            )}
            {latest?.tail_risks?.length ? (
              <div className="space-y-2">
                <div className="text-xs text-gray-400 uppercase tracking-wider">Tail Risk</div>
                {latest.tail_risks.map((risk) => (
                  <ProgressBar
                    key={risk.symbol}
                    label={risk.symbol}
                    value={(risk.extreme_move_probability ?? 0) * 100}
                    accent={(risk.extreme_move_probability ?? 0) > 0.35 ? 'red' : 'yellow'}
                    right={pct((risk.expected_max_drawdown ?? 0) * 100, 1)}
                  />
                ))}
              </div>
            ) : null}
          </CardBody>
        </Card>
      </div>
    </div>
  )
}

