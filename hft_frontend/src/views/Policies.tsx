import { useEffect, useState, useCallback } from 'react'
import { clsx } from 'clsx'
import {
  FlaskConical, Loader2, RefreshCw, RotateCcw, TrendingUp, CheckCircle, AlertTriangle,
} from 'lucide-react'
import { Card, CardBody, CardHeader } from '../components/shared/Card'
import { Tag } from '../components/shared/Badge'
import { useStore } from '../store'
import { listPolicies, promotePolicy, rollbackPolicy } from '../api'
import type { PolicyInfo, PolicyStatus } from '../types'

const PIPELINE_STAGES: PolicyStatus[] = ['research', 'shadow', 'paper', 'live_canary', 'live_approved', 'disabled']

const STATUS_COLOR: Record<PolicyStatus, string> = {
  research:      'blue',
  shadow:        'yellow',
  paper:         'cyan',
  live_canary:   'orange',
  live_approved: 'green',
  disabled:      'gray',
}

const STATUS_ACTIVE_CLASS: Record<PolicyStatus, string> = {
  research:      'bg-brand-blue/20 border-brand-blue/40 text-brand-blue',
  shadow:        'bg-brand-yellow/20 border-brand-yellow/40 text-brand-yellow',
  paper:         'bg-brand-cyan/20 border-brand-cyan/40 text-brand-cyan',
  live_canary:   'bg-brand-orange/20 border-brand-orange/40 text-brand-orange',
  live_approved: 'bg-brand-green/20 border-brand-green/40 text-brand-green',
  disabled:      'bg-gray-700/40 border-gray-600/40 text-gray-400',
}

const NEXT_STATUS: Record<PolicyStatus, PolicyStatus | null> = {
  research:      'shadow',
  shadow:        'paper',
  paper:         'live_canary',
  live_canary:   'live_approved',
  live_approved: null,
  disabled:      null,
}

const PREV_STATUS: Record<PolicyStatus, PolicyStatus | null> = {
  research:      null,
  shadow:        'research',
  paper:         'shadow',
  live_canary:   'paper',
  live_approved: 'live_canary',
  disabled:      'research',
}

export function Policies() {
  const policies    = useStore((s) => s.policies)
  const setPolicies = useStore((s) => s.setPolicies)
  const setError    = useStore((s) => s.setError)

  const [loading, setLoading]     = useState(false)
  const [filterStatus, setFilterStatus] = useState<PolicyStatus | 'all'>('all')
  const [promoting, setPromoting] = useState<Record<string, boolean>>({})
  const [rolling, setRolling]     = useState<Record<string, boolean>>({})

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await listPolicies()
      setPolicies(res.policies)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load policies')
    } finally {
      setLoading(false)
    }
  }, [setPolicies, setError])

  useEffect(() => { load() }, [load])

  const handlePromote = async (policy: PolicyInfo) => {
    const next = NEXT_STATUS[policy.status]
    if (!next) return
    setPromoting(p => ({ ...p, [policy.policy_id]: true }))
    try {
      await promotePolicy(policy.policy_id, next)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Promotion failed')
    } finally {
      setPromoting(p => ({ ...p, [policy.policy_id]: false }))
    }
  }

  const handleRollback = async (policy: PolicyInfo) => {
    setRolling(r => ({ ...r, [policy.policy_id]: true }))
    try {
      await rollbackPolicy(policy.policy_id)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Rollback failed')
    } finally {
      setRolling(r => ({ ...r, [policy.policy_id]: false }))
    }
  }

  // Stage counts
  const stageCounts = PIPELINE_STAGES.reduce((acc, s) => {
    acc[s] = policies.filter(p => p.status === s).length
    return acc
  }, {} as Record<PolicyStatus, number>)

  const filtered = filterStatus === 'all'
    ? policies
    : policies.filter(p => p.status === filterStatus)

  return (
    <div className="space-y-5">

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <FlaskConical size={16} className="text-brand-cyan" />
          <div>
            <h1 className="text-lg font-bold text-gray-100">RL Policy Registry</h1>
            <p className="text-xs text-gray-500 mt-0.5">Multi-agent reinforcement learning · promotion lifecycle</p>
          </div>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-surface-elevated border border-surface-border text-xs text-gray-400 hover:text-gray-200 transition-colors"
        >
          {loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
          Refresh
        </button>
      </div>

      {/* Advisory Notice */}
      <div className="flex items-center gap-2 px-4 py-3 rounded-lg bg-brand-yellow/5 border border-brand-yellow/20">
        <AlertTriangle size={14} className="text-brand-yellow flex-shrink-0" />
        <span className="text-xs text-brand-yellow">
          Advisory only — RL policies never submit live orders directly. All trades go through the risk engine.
        </span>
      </div>

      {/* Pipeline Diagram */}
      <Card>
        <CardHeader title="Promotion Pipeline" subtitle="Click a stage to filter" />
        <CardBody>
          <div className="flex items-center gap-1 overflow-x-auto pb-2">
            {PIPELINE_STAGES.map((stage, idx) => (
              <div key={stage} className="flex items-center">
                <button
                  onClick={() => setFilterStatus(filterStatus === stage ? 'all' : stage)}
                  className={clsx(
                    'px-3 py-2 rounded-lg text-xs font-medium border transition-colors whitespace-nowrap',
                    filterStatus === stage
                      ? STATUS_ACTIVE_CLASS[stage]
                      : 'bg-surface-elevated border-surface-border text-gray-400 hover:text-gray-200',
                  )}
                >
                  <div className="capitalize">{stage.replace('_', ' ')}</div>
                  <div className={clsx(
                    'text-center font-bold font-mono mt-0.5',
                    stageCounts[stage] > 0 ? 'text-gray-100' : 'text-gray-600',
                  )}>
                    {stageCounts[stage]}
                  </div>
                </button>
                {idx < PIPELINE_STAGES.length - 1 && (
                  <div className="mx-1 text-gray-700">→</div>
                )}
              </div>
            ))}
          </div>
        </CardBody>
      </Card>

      {/* Filter tabs */}
      <div className="flex items-center gap-1 flex-wrap">
        <button
          onClick={() => setFilterStatus('all')}
          className={clsx(
            'px-3 py-1.5 rounded-md text-xs font-medium border transition-colors',
            filterStatus === 'all'
              ? 'bg-brand-cyan/15 border-brand-cyan/30 text-brand-cyan'
              : 'text-gray-400 hover:text-gray-200 border-transparent',
          )}
        >
          All ({policies.length})
        </button>
        {PIPELINE_STAGES.filter(s => stageCounts[s] > 0).map((stage) => (
          <button
            key={stage}
            onClick={() => setFilterStatus(filterStatus === stage ? 'all' : stage)}
            className={clsx(
              'px-3 py-1.5 rounded-md text-xs font-medium border transition-colors capitalize',
              filterStatus === stage
                ? 'bg-brand-cyan/15 border-brand-cyan/30 text-brand-cyan'
                : 'text-gray-400 hover:text-gray-200 border-transparent',
            )}
          >
            {stage.replace('_', ' ')} ({stageCounts[stage]})
          </button>
        ))}
      </div>

      {/* Policy Cards */}
      {loading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 size={20} className="animate-spin text-gray-500" />
        </div>
      ) : filtered.length === 0 ? (
        <div className="text-center py-12 text-gray-500">
          <FlaskConical size={32} className="mx-auto mb-3 text-gray-700" />
          <p className="text-sm">No policies {filterStatus !== 'all' ? `in ${filterStatus} stage` : 'registered'}</p>
        </div>
      ) : (
        <div className="space-y-3">
          {filtered.map((policy) => {
            const nextStatus = NEXT_STATUS[policy.status]
            const stageIdx   = PIPELINE_STAGES.indexOf(policy.status)
            const gate       = policy.promotion_gate
            return (
              <Card key={policy.policy_id}>
                <CardBody className="!py-4">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap mb-1">
                        <span className="text-sm font-semibold text-gray-100 font-mono">{policy.policy_id}</span>
                        <Tag
                          label={policy.status.replace('_', ' ')}
                          color={STATUS_COLOR[policy.status] as 'green' | 'red' | 'blue' | 'yellow' | 'purple' | 'cyan' | 'orange' | 'gray'}
                        />
                        {policy.can_submit_live_orders && (
                          <Tag label="Live Orders" color="green" />
                        )}
                        {nextStatus && gate && (
                          <Tag label={gate.approved ? 'gate ready' : 'gate blocked'} color={gate.approved ? 'green' : 'red'} />
                        )}
                      </div>
                      <div className="text-xs text-gray-400 mb-3">
                        {policy.name || policy.policy_id}
                        {policy.promoted_at && (
                          <span className="ml-2 text-gray-600">Promoted: {policy.promoted_at.slice(0, 10)}</span>
                        )}
                      </div>
                      {nextStatus && gate && !gate.approved && (
                        <div className="text-xs text-brand-red mb-3">
                          Gate: {gate.reason.replace(/_/g, ' ')}
                        </div>
                      )}

                      {/* Pipeline progress bar */}
                      <div className="flex items-center gap-1 mb-3">
                        {PIPELINE_STAGES.filter(s => s !== 'disabled').map((stage, i) => (
                          <div key={stage} className="flex items-center">
                            <div className={clsx(
                              'w-5 h-5 rounded-full flex items-center justify-center text-[9px] font-bold border transition-colors',
                              i < stageIdx
                                ? 'bg-brand-green/20 border-brand-green/30 text-brand-green'
                                : i === stageIdx
                                  ? 'bg-brand-cyan/20 border-brand-cyan/30 text-brand-cyan'
                                  : 'bg-surface-elevated border-surface-border text-gray-600',
                            )}>
                              {i < stageIdx
                                ? <CheckCircle size={10} />
                                : i + 1}
                            </div>
                            {i < PIPELINE_STAGES.filter(s => s !== 'disabled').length - 1 && (
                              <div className={clsx(
                                'w-4 h-px mx-0.5',
                                i < stageIdx ? 'bg-brand-green/40' : 'bg-surface-border',
                              )} />
                            )}
                          </div>
                        ))}
                      </div>
                    </div>

                    {/* Actions */}
                    <div className="flex items-center gap-2 flex-shrink-0">
                      {nextStatus && (
                        <button
                          onClick={() => handlePromote(policy)}
                          disabled={promoting[policy.policy_id] || gate?.approved === false}
                          className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-brand-blue/15 border border-brand-blue/30 text-brand-blue text-xs font-medium hover:bg-brand-blue/25 disabled:opacity-50 transition-colors"
                        >
                          {promoting[policy.policy_id] ? <Loader2 size={11} className="animate-spin" /> : <TrendingUp size={11} />}
                          Promote → {nextStatus.replace('_', ' ')}
                        </button>
                      )}
                      {policy.status !== 'research' && (
                        <button
                          onClick={() => handleRollback(policy)}
                          disabled={rolling[policy.policy_id]}
                          className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md bg-surface-elevated border border-surface-border text-gray-400 text-xs hover:text-gray-200 disabled:opacity-50 transition-colors"
                        >
                          {rolling[policy.policy_id] ? <Loader2 size={11} className="animate-spin" /> : <RotateCcw size={11} />}
                          Rollback
                        </button>
                      )}
                    </div>
                  </div>
                </CardBody>
              </Card>
            )
          })}
        </div>
      )}

    </div>
  )
}
