import { useCallback, useEffect, useMemo, useState } from 'react'
import { clsx } from 'clsx'
import { Brain, Loader2, RefreshCw, ShieldAlert, Vote, Workflow } from 'lucide-react'
import { getAICouncilDecisions, getAICouncilStatus, runAICouncilPreview } from '../api'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { JsonPanel, KeyValueGrid, PageHeader, ProgressBar, StatTile } from '../components/shared/Command'
import { Table } from '../components/shared/Table'
import { Tag } from '../components/shared/Badge'
import { useStore } from '../store'
import { fmtDateTime, pct } from '../utils'
import type { AgentVote, AICouncilDecision, StrategyProposal } from '../types'

function agentName(vote: AgentVote) {
  return vote.agent_name ?? vote.agent ?? 'agent'
}

function actionColor(action: string): 'green' | 'red' | 'yellow' | 'gray' {
  if (action.includes('BUY') || action === 'PROCEED') return 'green'
  if (action.includes('SELL') || action === 'HALT') return 'red'
  if (action === 'REDUCE') return 'yellow'
  return 'gray'
}

function normalizeDecision(raw: unknown): AICouncilDecision | null {
  if (!raw || typeof raw !== 'object') return null
  if (Array.isArray(raw)) return normalizeDecision(raw[0])
  const d = raw as Partial<AICouncilDecision>
  if (!d.trace_id || !d.action) return null
  return {
    trace_id: String(d.trace_id),
    action: String(d.action),
    confidence: Number(d.confidence ?? 0),
    consensus_score: Number(d.consensus_score ?? 0),
    votes: Array.isArray(d.votes) ? d.votes : [],
    strategy_proposals: Array.isArray(d.strategy_proposals) ? d.strategy_proposals : [],
    risk_critique: d.risk_critique ?? null,
    portfolio_proposal: d.portfolio_proposal ?? null,
    execution_advice: d.execution_advice ?? null,
    debate_summary: d.debate_summary ?? '',
    model_ids_used: Array.isArray(d.model_ids_used) ? d.model_ids_used : [],
    evidence_ids: Array.isArray(d.evidence_ids) ? d.evidence_ids : [],
    ts: d.ts,
    timestamp: d.timestamp,
  }
}

export function AICouncil() {
  const status = useStore((s) => s.aiCouncilStatus)
  const decisions = useStore((s) => s.aiCouncilDecisions)
  const setStatus = useStore((s) => s.setAICouncilStatus)
  const setDecisions = useStore((s) => s.setAICouncilDecisions)
  const setError = useStore((s) => s.setError)

  const [symbols, setSymbols] = useState('NIFTY,BANKNIFTY,RELIANCE')
  const [regime, setRegime] = useState('NEUTRAL')
  const [preview, setPreview] = useState<AICouncilDecision | null>(null)
  const [loading, setLoading] = useState(false)
  const [previewing, setPreviewing] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [statusRes, decisionRes] = await Promise.allSettled([
        getAICouncilStatus(),
        getAICouncilDecisions(20),
      ])
      if (statusRes.status === 'fulfilled') setStatus(statusRes.value)
      if (decisionRes.status === 'fulfilled') {
        const normalized = decisionRes.value.decisions
          .map((item) => normalizeDecision(item))
          .filter(Boolean) as AICouncilDecision[]
        setDecisions(normalized)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load AI Council')
    } finally {
      setLoading(false)
    }
  }, [setDecisions, setError, setStatus])

  useEffect(() => { load() }, [load])

  const runPreview = async () => {
    setPreviewing(true)
    try {
      const syms = symbols.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean)
      const res = await runAICouncilPreview({ symbols: syms, regime })
      setPreview(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'AI Council preview failed')
    } finally {
      setPreviewing(false)
    }
  }

  const activeDecision = preview ?? decisions[0] ?? null
  const voteRows = activeDecision?.votes ?? []
  const proposalRows = activeDecision?.strategy_proposals ?? []
  const statusTiles = useMemo(() => ([
    {
      label: 'Gateway',
      value: status?.gateway_available ? 'LIVE' : status?.fallback_active ? 'STUB' : 'OFF',
      accent: status?.gateway_available ? 'green' as const : status?.fallback_active ? 'yellow' as const : 'gray' as const,
      sub: status?.gateway_runtime ?? 'runtime unknown',
    },
    {
      label: 'Primary Model',
      value: status?.primary_model ?? 'not configured',
      accent: 'purple' as const,
      sub: status?.models ? `${status.models.coordinator} coordinator` : undefined,
    },
    {
      label: 'Consensus',
      value: activeDecision ? pct(activeDecision.consensus_score * 100, 0) : '--',
      accent: activeDecision && activeDecision.consensus_score >= 0.65 ? 'green' as const : 'yellow' as const,
      sub: activeDecision?.trace_id.slice(0, 18),
    },
    {
      label: 'Council Action',
      value: activeDecision?.action ?? 'NO DECISION',
      accent: actionColor(activeDecision?.action ?? '') as 'green' | 'red' | 'yellow' | 'gray',
      sub: activeDecision ? `${pct(activeDecision.confidence * 100, 0)} confidence` : 'run a preview',
    },
  ]), [activeDecision, status])

  return (
    <div className="space-y-5">
      <PageHeader
        title="AI Council"
        subtitle="Local Gemma-compatible specialist agents, debate, risk critic, and structured trade proposals"
        icon={<Brain size={17} className="text-brand-purple" />}
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
        {statusTiles.map((tile) => <StatTile key={tile.label} {...tile} />)}
      </div>

      <Card className="border-brand-purple/20">
        <CardHeader title="Preview Council Decision" subtitle="Structured advisory output only" icon={<Vote size={14} />} />
        <CardBody className="space-y-3">
          <div className="grid grid-cols-1 md:grid-cols-[1fr_180px_auto] gap-3">
            <label className="space-y-1">
              <span className="text-xs text-gray-400 uppercase tracking-wider">Symbol Universe</span>
              <input
                value={symbols}
                onChange={(e) => setSymbols(e.target.value)}
                className="w-full bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded-md px-3 py-2 font-mono focus:outline-none focus:border-brand-purple"
              />
            </label>
            <label className="space-y-1">
              <span className="text-xs text-gray-400 uppercase tracking-wider">Regime</span>
              <select
                value={regime}
                onChange={(e) => setRegime(e.target.value)}
                className="w-full bg-surface-elevated border border-surface-border text-xs text-gray-200 rounded-md px-3 py-2 font-mono focus:outline-none focus:border-brand-purple"
              >
                {['NEUTRAL', 'BULLISH', 'BEARISH', 'VOLATILE', 'TRENDING_UP', 'TRENDING_DOWN', 'EVENT_RISK'].map((r) => (
                  <option key={r} value={r}>{r}</option>
                ))}
              </select>
            </label>
            <button
              onClick={runPreview}
              disabled={previewing}
              className="self-end flex items-center justify-center gap-2 px-4 py-2 rounded-md bg-brand-purple/15 border border-brand-purple/30 text-brand-purple text-xs font-semibold hover:bg-brand-purple/25 disabled:opacity-50"
            >
              {previewing ? <Loader2 size={12} className="animate-spin" /> : <Workflow size={12} />}
              Run Preview
            </button>
          </div>

          {activeDecision && (
            <div className="grid grid-cols-1 lg:grid-cols-[1.3fr_.7fr] gap-4 pt-3 border-t border-surface-border">
              <div className="space-y-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Tag label={activeDecision.action} color={actionColor(activeDecision.action) as 'green' | 'red' | 'yellow' | 'gray'} />
                  {activeDecision.risk_critique?.veto && <Tag label="Risk critic veto" color="red" />}
                  {activeDecision.debate_summary && <Tag label="Debate recorded" color="yellow" />}
                  {(activeDecision.model_ids_used ?? []).map((model) => <Tag key={model} label={model} color="purple" />)}
                </div>
                <ProgressBar
                  label="Consensus"
                  value={activeDecision.consensus_score * 100}
                  accent={activeDecision.consensus_score > 0.65 ? 'green' : 'yellow'}
                  right={pct(activeDecision.consensus_score * 100, 0)}
                />
                <Table<AgentVote>
                  columns={[
                    { key: 'agent_name', label: 'Agent', render: (_, row) => <span className="text-brand-purple font-semibold">{agentName(row)}</span> },
                    { key: 'action', label: 'Vote', render: (v) => <Tag label={String(v)} color={actionColor(String(v)) as 'green' | 'red' | 'yellow' | 'gray'} /> },
                    { key: 'confidence', label: 'Conf', align: 'right', render: (v) => pct(Number(v) * 100, 0) },
                    { key: 'reasoning', label: 'Reasoning', render: (v) => <span className="text-xs text-gray-500 line-clamp-2">{String(v)}</span> },
                  ]}
                  rows={voteRows}
                  keyFn={(row, i) => `${agentName(row)}-${i}`}
                  emptyMessage="No agent votes"
                  compact
                />
              </div>

              <div className="space-y-3">
                <KeyValueGrid
                  columns="grid-cols-2"
                  items={[
                    { label: 'Risk Veto', value: activeDecision.risk_critique?.veto ? 'YES' : 'NO', accent: activeDecision.risk_critique?.veto ? 'red' : 'green' },
                    { label: 'Risk Score', value: activeDecision.risk_critique ? activeDecision.risk_critique.risk_score.toFixed(2) : '--', accent: 'red' },
                    { label: 'Max Heat', value: activeDecision.portfolio_proposal ? pct(activeDecision.portfolio_proposal.max_heat * 100, 1) : '--', accent: 'orange' },
                    { label: 'Order Type', value: activeDecision.execution_advice?.preferred_order_type ?? '--', accent: 'cyan' },
                  ]}
                />
                {activeDecision.risk_critique?.concerns?.length ? (
                  <div className="rounded-lg bg-brand-red/5 border border-brand-red/20 p-3">
                    <div className="flex items-center gap-2 text-xs font-semibold text-brand-red mb-2">
                      <ShieldAlert size={13} />
                      Risk Critic Concerns
                    </div>
                    <div className="space-y-1">
                      {activeDecision.risk_critique.concerns.map((concern) => (
                        <div key={concern} className="text-xs text-gray-400">{concern}</div>
                      ))}
                    </div>
                  </div>
                ) : null}
                {activeDecision.debate_summary && (
                  <div className="rounded-lg bg-surface-elevated border border-surface-border p-3 text-xs text-gray-400">
                    {activeDecision.debate_summary}
                  </div>
                )}
              </div>
            </div>
          )}
        </CardBody>
      </Card>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
        <Card>
          <CardHeader title="Strategy Proposals" subtitle={`${proposalRows.length} typed proposals`} />
          <Table<StrategyProposal>
            columns={[
              { key: 'symbol', label: 'Symbol', render: (v) => <span className="text-brand-blue font-semibold">{String(v)}</span> },
              { key: 'side', label: 'Side', render: (v) => <Tag label={String(v)} color={String(v) === 'BUY' ? 'green' : 'red'} /> },
              { key: 'edge_estimate', label: 'Edge', align: 'right', render: (v) => pct(Number(v) * 100, 2) },
              { key: 'risk_estimate', label: 'Risk', align: 'right', render: (v) => pct(Number(v) * 100, 2) },
              { key: 'confidence', label: 'Conf', align: 'right', render: (v) => pct(Number(v) * 100, 0) },
            ]}
            rows={proposalRows}
            keyFn={(row, i) => `${row.agent_name}-${row.symbol}-${i}`}
            emptyMessage="No proposals yet"
            compact
          />
        </Card>

        <Card>
          <CardHeader title="Recent Council Decisions" subtitle={`${decisions.length} loaded`} />
          <Table<AICouncilDecision>
            columns={[
              { key: 'trace_id', label: 'Trace', render: (v) => <span className="text-brand-purple text-xs">{String(v).slice(0, 18)}</span> },
              { key: 'action', label: 'Action', render: (v) => <Tag label={String(v)} color={actionColor(String(v)) as 'green' | 'red' | 'yellow' | 'gray'} /> },
              { key: 'consensus_score', label: 'Consensus', align: 'right', render: (v) => pct(Number(v) * 100, 0) },
              { key: 'ts', label: 'Time', render: (v, row) => <span className="text-xs text-gray-500">{fmtDateTime(String(v ?? row.timestamp ?? row.trace_id))}</span> },
            ]}
            rows={decisions}
            keyFn={(row) => row.trace_id}
            emptyMessage="No persisted council decisions"
            compact
          />
        </Card>
      </div>

      {activeDecision && (
        <Card>
          <CardHeader title="Raw Structured Output" subtitle="Debug trace payload" />
          <CardBody>
            <JsonPanel value={activeDecision} />
          </CardBody>
        </Card>
      )}
    </div>
  )
}
