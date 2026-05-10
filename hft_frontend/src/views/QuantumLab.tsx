import { useCallback, useEffect, useState } from 'react'
import { clsx } from 'clsx'
import { Atom, Cpu, Loader2, RefreshCw, ShieldCheck } from 'lucide-react'
import { getQuantumResults, getQuantumStatus, runQuantumOptimizePreview } from '../api'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { JsonPanel, KeyValueGrid, PageHeader, ProgressBar, StatTile } from '../components/shared/Command'
import { Table } from '../components/shared/Table'
import { Tag } from '../components/shared/Badge'
import { useStore } from '../store'
import { fmtDateTime, pct } from '../utils'
import type { QuantumOptimizationResult } from '../types'

const SAMPLE_CANDIDATES = JSON.stringify([
  { symbol: 'NIFTY', side: 'BUY', expected_edge: 0.014, risk_estimate: 0.31, liquidity_score: 0.94 },
  { symbol: 'BANKNIFTY', side: 'BUY', expected_edge: 0.018, risk_estimate: 0.42, liquidity_score: 0.91 },
  { symbol: 'RELIANCE', side: 'BUY', expected_edge: 0.011, risk_estimate: 0.26, liquidity_score: 0.88 },
  { symbol: 'TCS', side: 'SELL', expected_edge: 0.009, risk_estimate: 0.22, liquidity_score: 0.86 },
], null, 2)

function backendColor(available: boolean): 'green' | 'gray' {
  return available ? 'green' : 'gray'
}

export function QuantumLab() {
  const status = useStore((s) => s.quantumStatus)
  const latest = useStore((s) => s.latestQuantumResult)
  const setStatus = useStore((s) => s.setQuantumStatus)
  const setLatest = useStore((s) => s.setLatestQuantumResult)
  const setError = useStore((s) => s.setError)

  const [candidateText, setCandidateText] = useState(SAMPLE_CANDIDATES)
  const [riskAversion, setRiskAversion] = useState(0.5)
  const [cardinality, setCardinality] = useState(2)
  const [loading, setLoading] = useState(false)
  const [optimizing, setOptimizing] = useState(false)
  const [history, setHistory] = useState<Partial<QuantumOptimizationResult>[]>([])
  const [jsonError, setJsonError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [statusRes, historyRes] = await Promise.allSettled([
        getQuantumStatus(),
        getQuantumResults(20),
      ])
      if (statusRes.status === 'fulfilled') setStatus(statusRes.value)
      if (historyRes.status === 'fulfilled') setHistory(historyRes.value.results as Partial<QuantumOptimizationResult>[])
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load Quantum Lab')
    } finally {
      setLoading(false)
    }
  }, [setError, setStatus])

  useEffect(() => { load() }, [load])

  const runOptimize = async () => {
    setJsonError(null)
    let candidates: unknown
    try {
      candidates = JSON.parse(candidateText)
      if (!Array.isArray(candidates)) throw new Error('Candidate payload must be an array')
    } catch (e) {
      setJsonError(e instanceof Error ? e.message : 'Invalid candidate JSON')
      return
    }

    setOptimizing(true)
    try {
      const res = await runQuantumOptimizePreview({
        candidates,
        risk_aversion: riskAversion,
        cardinality_limit: cardinality,
      })
      setLatest(res)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Quantum optimization failed')
    } finally {
      setOptimizing(false)
    }
  }

  const availableBackends = status?.backends?.filter((b) => b.available).length ?? 0
  const beatsBaseline = latest?.beats_baseline ?? (
    latest?.classical_baseline_objective != null
      ? latest.objective_value >= latest.classical_baseline_objective
      : undefined
  )

  return (
    <div className="space-y-5">
      <PageHeader
        title="Quantum Lab"
        subtitle="QUBO/CQM portfolio optimization, backend gates, and classical baseline comparison"
        icon={<Atom size={17} className="text-brand-cyan" />}
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
        <StatTile label="Lab State" value={status?.enabled ? 'ENABLED' : 'DISABLED'} accent={status?.enabled ? 'green' : 'gray'} sub="advisory only" />
        <StatTile label="Active Backend" value={status?.backend ?? '--'} accent="cyan" sub={`${availableBackends} backend(s) available`} />
        <StatTile label="Timeout" value={`${status?.timeout_seconds ?? '--'}s`} accent="orange" sub="optimizer budget" />
        <StatTile label="Last Gate" value={latest ? (beatsBaseline ? 'PASS' : 'CHECK') : '--'} accent={latest ? (beatsBaseline ? 'green' : 'yellow') : 'gray'} sub={latest?.trace_id.slice(0, 18) ?? 'no result yet'} />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-[.9fr_1.1fr] gap-5">
        <Card className="border-brand-cyan/20">
          <CardHeader title="Optimize Preview" subtitle="Candidate basket with risk constraints" icon={<Cpu size={14} />} />
          <CardBody className="space-y-4">
            <label className="space-y-1 block">
              <span className="text-xs text-gray-400 uppercase tracking-wider">Candidates JSON</span>
              <textarea
                value={candidateText}
                onChange={(e) => setCandidateText(e.target.value)}
                rows={11}
                className="w-full bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded-md px-3 py-2 font-mono focus:outline-none focus:border-brand-cyan resize-y"
              />
            </label>
            {jsonError && <div className="text-xs text-brand-red">{jsonError}</div>}

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <label className="space-y-2">
                <span className="text-xs text-gray-400 uppercase tracking-wider">Risk Aversion: {riskAversion.toFixed(2)}</span>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={riskAversion}
                  onChange={(e) => setRiskAversion(Number(e.target.value))}
                  className="w-full accent-cyan-400"
                />
              </label>
              <label className="space-y-1">
                <span className="text-xs text-gray-400 uppercase tracking-wider">Cardinality Limit</span>
                <input
                  type="number"
                  min={1}
                  max={20}
                  value={cardinality}
                  onChange={(e) => setCardinality(Number(e.target.value))}
                  className="w-full bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded-md px-3 py-2 font-mono focus:outline-none focus:border-brand-cyan"
                />
              </label>
            </div>

            <button
              onClick={runOptimize}
              disabled={optimizing}
              className="flex items-center justify-center gap-2 px-4 py-2 rounded-md bg-brand-cyan/15 border border-brand-cyan/30 text-brand-cyan text-xs font-semibold hover:bg-brand-cyan/25 disabled:opacity-50"
            >
              {optimizing ? <Loader2 size={12} className="animate-spin" /> : <Atom size={12} />}
              Run Optimization
            </button>
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Optimization Result" subtitle="Quantum gate must beat or diversify the classical baseline" icon={<ShieldCheck size={14} />} />
          <CardBody className="space-y-4">
            {latest ? (
              <>
                <div className="flex flex-wrap gap-2">
                  {latest.selected_symbols.map((symbol) => <Tag key={symbol} label={symbol} color="cyan" />)}
                  <Tag label={latest.backend_used} color="blue" />
                  {latest.constraints_satisfied === false && <Tag label="constraints failed" color="red" />}
                  {latest.stable === false && <Tag label="unstable" color="yellow" />}
                </div>
                <KeyValueGrid
                  columns="grid-cols-2"
                  items={[
                    { label: 'Objective', value: latest.objective_value.toFixed(5), accent: 'cyan' },
                    { label: 'Baseline', value: latest.classical_baseline_objective != null ? latest.classical_baseline_objective.toFixed(5) : '--', accent: 'blue' },
                    { label: 'Edge Sum', value: latest.expected_edge_sum != null ? pct(latest.expected_edge_sum * 100, 2) : '--', accent: 'green' },
                    { label: 'Risk Score', value: latest.risk_score != null ? pct(latest.risk_score * 100, 1) : '--', accent: 'red' },
                  ]}
                />
                {latest.classical_baseline_objective != null && (
                  <ProgressBar
                    label="Objective vs baseline"
                    value={Math.max(0, latest.objective_value)}
                    max={Math.max(latest.objective_value, latest.classical_baseline_objective, 0.0001)}
                    accent={beatsBaseline ? 'green' : 'yellow'}
                    right={beatsBaseline ? 'beats baseline' : 'baseline leads'}
                  />
                )}
                <JsonPanel value={latest} />
              </>
            ) : (
              <div className="text-xs text-gray-500 text-center py-12">Run optimization to inspect backend gates</div>
            )}
          </CardBody>
        </Card>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
        <Card>
          <CardHeader title="Backend Status" subtitle={`${status?.backends?.length ?? 0} registered`} />
          <CardBody className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {(status?.backends ?? []).map((backend) => (
              <div key={backend.name} className={clsx('rounded-lg border p-3', backend.available ? 'border-brand-green/20 bg-brand-green/5' : 'border-surface-border bg-surface-card')}>
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-semibold text-gray-100 font-mono">{backend.name}</span>
                  <Tag label={backend.available ? 'available' : 'offline'} color={backendColor(backend.available)} />
                </div>
                <div className="mt-2 text-xs text-gray-500">{backend.error ?? backend.reason ?? 'ready for research lane'}</div>
                {backend.latency_ms != null && <div className="mt-1 text-xs font-mono text-gray-400">{backend.latency_ms.toFixed(1)}ms</div>}
              </div>
            ))}
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Recent Quantum Trace Links" subtitle={`${history.length} persisted`} />
          <Table<Partial<QuantumOptimizationResult>>
            columns={[
              { key: 'trace_id', label: 'Trace', render: (v) => <span className="text-brand-cyan text-xs">{String(v).slice(0, 18)}</span> },
              { key: 'backend_used', label: 'Backend', render: (v) => String(v ?? '--') },
              { key: 'selected_symbols', label: 'Selected', render: (v) => Array.isArray(v) ? v.join(', ') : '--' },
              { key: 'ts', label: 'Time', render: (v) => v ? <span className="text-xs text-gray-500">{fmtDateTime(String(v))}</span> : <span className="text-gray-600">--</span> },
            ]}
            rows={history}
            keyFn={(row, index) => `${row.trace_id ?? 'q'}-${index}`}
            emptyMessage="No quantum trace results"
            compact
          />
        </Card>
      </div>
    </div>
  )
}
