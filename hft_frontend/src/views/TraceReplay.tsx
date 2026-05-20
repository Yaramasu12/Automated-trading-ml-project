import { useCallback, useEffect, useState } from 'react'
import { clsx } from 'clsx'
import { Database, Loader2, RefreshCw, Search, Waypoints } from 'lucide-react'
import { getTraceReplay, listTraces } from '../api'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { JsonPanel, KeyValueGrid, PageHeader, StatTile } from '../components/shared/Command'
import { Table } from '../components/shared/Table'
import { Tag } from '../components/shared/Badge'
import { useStore } from '../store'
import { fmtDateTime } from '../utils'
import type { DecisionTrace, TraceReplayResponse } from '../types'

function modeColor(mode: string) {
  if (mode.startsWith('LIVE')) return 'red'
  if (mode === 'PAPER') return 'yellow'
  if (mode === 'SHADOW_LIVE') return 'orange'
  return 'blue'
}

export function TraceReplay() {
  const traces = useStore((s) => s.recentTraces)
  const setTraces = useStore((s) => s.setRecentTraces)
  const setError = useStore((s) => s.setError)

  const [selectedId, setSelectedId] = useState('')
  const [selected, setSelected] = useState<DecisionTrace | null>(null)
  const [replay, setReplay] = useState<TraceReplayResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await listTraces(60)
      setTraces(res.traces)
      if (!selectedId && res.traces[0]) setSelectedId(res.traces[0].trace_id)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load traces')
    } finally {
      setLoading(false)
    }
  }, [selectedId, setError, setTraces])

  useEffect(() => { load() }, [load])

  const loadDetail = useCallback(async (traceId: string) => {
    if (!traceId) return
    setDetailLoading(true)
    try {
      const nextReplay = await getTraceReplay(traceId)
      setReplay(nextReplay)
      setSelected(nextReplay.trace ?? null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Trace not found')
    } finally {
      setDetailLoading(false)
    }
  }, [setError])

  useEffect(() => { if (selectedId) loadDetail(selectedId) }, [selectedId, loadDetail])

  const events = replay?.timeline ?? []
  const riskDecisions = replay?.risk_decisions ?? selected?.risk_decisions ?? []

  return (
    <div className="space-y-5">
      <PageHeader
        title="Trace Replay"
        subtitle="Decision trace store across scan, agent, optimizer, risk, and order lifecycle"
        icon={<Waypoints size={17} className="text-brand-yellow" />}
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
        <StatTile label="Traces Loaded" value={traces.length} accent="yellow" sub="recent store window" />
        <StatTile label="Selected Mode" value={selected?.execution_mode ?? '--'} accent={selected ? modeColor(selected.execution_mode) as 'red' | 'yellow' | 'orange' | 'blue' : 'gray'} sub={selected?.created_at ? fmtDateTime(selected.created_at) : 'select a trace'} />
        <StatTile label="Replay Status" value={replay?.summary?.status ?? '--'} accent={replay?.summary?.lifecycle_complete ? 'green' : 'purple'} sub={replay ? `${replay.summary?.order_count ?? 0} order links` : 'timeline'} />
        <StatTile label="Paper Journal" value={replay?.summary?.journal_event_count ?? 0} accent="cyan" sub="fills/slippage/labels" />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-[.75fr_1.25fr] gap-5">
        <Card>
          <CardHeader
            title="Trace Index"
            subtitle={`${traces.length} recent`}
            icon={<Database size={14} />}
            action={
              <div className="flex items-center gap-1.5 px-2 py-1 rounded-md bg-surface-elevated border border-surface-border">
                <Search size={12} className="text-gray-500" />
                <input
                  value={selectedId}
                  onChange={(e) => setSelectedId(e.target.value)}
                  className="w-44 bg-transparent text-xs text-gray-300 font-mono outline-none"
                  placeholder="trace id"
                />
              </div>
            }
          />
          <CardBody className="space-y-2 max-h-[680px] overflow-y-auto">
            {traces.map((trace) => {
              const active = selectedId === trace.trace_id
              return (
                <button
                  key={trace.trace_id}
                  onClick={() => setSelectedId(trace.trace_id)}
                  className={clsx(
                    'w-full text-left rounded-lg border p-3 transition-colors',
                    active ? 'bg-brand-yellow/10 border-brand-yellow/30' : 'bg-surface-card border-surface-border hover:bg-surface-elevated',
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-xs text-brand-yellow truncate">{trace.trace_id}</span>
                    <Tag label={trace.execution_mode} color={modeColor(trace.execution_mode) as 'red' | 'yellow' | 'orange' | 'blue'} />
                  </div>
                  <div className="mt-2 text-xs text-gray-500">{fmtDateTime(trace.created_at)}</div>
                  <div className="mt-2 flex flex-wrap gap-1">
                    {trace.symbol_universe.slice(0, 5).map((symbol) => <Tag key={symbol} label={symbol} color="blue" />)}
                  </div>
                </button>
              )
            })}
            {traces.length === 0 && (
              <div className="text-xs text-gray-500 text-center py-8">No traces found</div>
            )}
          </CardBody>
        </Card>

        <div className="space-y-5">
          <Card className="border-brand-yellow/20">
            <CardHeader
              title="Selected Trace"
              subtitle={selected?.trace_id ?? 'No trace selected'}
              icon={<Waypoints size={14} />}
            />
            <CardBody className="space-y-4">
              {detailLoading ? (
                <div className="flex items-center justify-center py-12">
                  <Loader2 size={18} className="animate-spin text-gray-500" />
                </div>
              ) : selected ? (
                <>
                  <KeyValueGrid
                    columns="md:grid-cols-4"
                    items={[
                      { label: 'Mode', value: selected.execution_mode, accent: modeColor(selected.execution_mode) as 'red' | 'yellow' | 'orange' | 'blue' },
                      { label: 'Symbols', value: selected.symbol_universe.length, accent: 'blue' },
                      { label: 'Quantum Result', value: selected.quantum_result_id ?? '--', accent: selected.quantum_result_id ? 'cyan' : 'gray' },
                      { label: 'Orders', value: replay?.summary?.order_count ?? selected.order_intent_ids?.length ?? 0, accent: replay?.summary?.order_count ? 'green' : 'gray' },
                      { label: 'Fills', value: replay?.summary?.fill_count ?? 0, accent: replay?.summary?.fill_count ? 'green' : 'gray' },
                      { label: 'Slippage', value: replay?.summary?.slippage_count ?? 0, accent: replay?.summary?.slippage_count ? 'orange' : 'gray' },
                      { label: 'Labels', value: replay?.summary?.label_count ?? 0, accent: replay?.summary?.label_count ? 'purple' : 'gray' },
                      { label: 'Learning', value: replay?.summary?.learning_update_count ?? 0, accent: replay?.summary?.learning_update_count ? 'cyan' : 'gray' },
                    ]}
                  />
                  <div className="flex flex-wrap gap-2">
                    {(selected.symbol_universe ?? []).map((symbol) => <Tag key={symbol} label={symbol} color="blue" />)}
                    {Object.keys(selected.neural_model_versions ?? {}).map((model) => <Tag key={model} label={model} color="purple" />)}
                  </div>
                </>
              ) : (
                <div className="text-xs text-gray-500 text-center py-12">Select or search a trace</div>
              )}
            </CardBody>
          </Card>

          <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
            <Card>
              <CardHeader title="Timeline" subtitle={`${events.length} replay events`} />
              <CardBody className="space-y-3">
                {events.map((event, index) => (
                  <div key={`${event.ts}-${index}`} className="flex gap-3">
                    <div className="flex flex-col items-center">
                      <span className="w-2 h-2 rounded-full bg-brand-yellow mt-1.5" />
                      {index < events.length - 1 && <span className="w-px flex-1 bg-surface-border mt-1" />}
                    </div>
                    <div className="min-w-0 pb-3">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-xs font-semibold text-gray-200">{String(event.event_type ?? '')}</span>
                        <Tag label={String(event.component ?? event.source ?? '')} color={event.source === 'oms' ? 'blue' : event.source === 'label' ? 'green' : 'yellow'} />
                        {!!event.order_id && <Tag label={String(event.order_id).slice(0, 8)} color="gray" />}
                      </div>
                      <div className="mt-1 text-[10px] text-gray-600 font-mono">{event.ts ? fmtDateTime(String(event.ts)) : '--'}</div>
                      {!!event.reason && <div className="mt-1 text-xs text-gray-500">{String(event.reason)}</div>}
                    </div>
                  </div>
                ))}
                {events.length === 0 && <div className="text-xs text-gray-500 text-center py-8">No events persisted for this trace</div>}
              </CardBody>
            </Card>

            <Card>
              <CardHeader title="Risk Decisions" subtitle={`${riskDecisions.length} records`} />
              <Table<Record<string, unknown>>
                columns={[
                  { key: 'component', label: 'Component', render: (v) => String(v ?? '--') },
                  { key: 'approved', label: 'Approved', render: (v) => <Tag label={String(v ?? '--')} color={v === false ? 'red' : 'green'} /> },
                  { key: 'reason', label: 'Reason', render: (v) => <span className="text-xs text-gray-500">{String(v ?? '--')}</span> },
                ]}
                rows={riskDecisions}
                keyFn={(_, index) => String(index)}
                emptyMessage="No risk decisions"
                compact
              />
            </Card>
          </div>

          {selected && (
            <Card>
              <CardHeader title="Raw Replay Payload" subtitle="Joined trace, OMS, labels, and fills" />
              <CardBody>
                <JsonPanel value={replay ?? selected} />
              </CardBody>
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}
