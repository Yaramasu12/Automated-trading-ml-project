import { useEffect, useState } from 'react'
import { useStore } from './store'

// The four honest states a live dashboard can be in. "closed" is deliberately
// distinct from "stale": a silent feed while the market is closed is EXPECTED,
// not an error, and must not read as a red alarm.
export type ConnState = 'connecting' | 'offline' | 'stale' | 'closed' | 'live'
export type Tone = 'green' | 'amber' | 'red' | 'neutral'

export interface ConnectionStatus {
  state: ConnState
  label: string
  tone: Tone
  ageSeconds: number | null
  marketStatus: string | null
  asOf: string | null
  detail: string
}

// The backend heartbeats every 15s; miss ~2 and the connection is effectively
// frozen even if the socket still reports "open".
const FROZEN_AFTER_MS = 35_000

const TONE: Record<ConnState, Tone> = {
  connecting: 'neutral',
  offline: 'red',
  stale: 'amber',
  closed: 'neutral',
  live: 'green',
}

const LABEL: Record<ConnState, string> = {
  connecting: 'CONNECTING',
  offline: 'OFFLINE',
  stale: 'STALE',
  closed: 'MARKET CLOSED',
  live: 'LIVE',
}

export function fmtAge(seconds: number | null): string {
  if (seconds == null) return '—'
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  return `${Math.floor(seconds / 3600)}h ago`
}

/**
 * Derives the one true connection/freshness status from the store. Ticks every
 * second so "age" counts up on screen. Every freshness badge in the app should
 * read from this hook so the whole UI agrees on what "live" means.
 */
export function useConnectionStatus(): ConnectionStatus {
  const wsConnected = useStore((s) => s.wsConnected)
  const lastSnapshotAt = useStore((s) => s.lastSnapshotAt)
  const liveFeed = useStore((s) => s.liveFeed)

  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])

  const ageMs = lastSnapshotAt != null ? now - lastSnapshotAt : null
  const ageSeconds = ageMs != null ? Math.floor(ageMs / 1000) : null
  const fresh = liveFeed?.freshness
  const marketStatus = fresh?.market_status ?? null
  const asOf = fresh?.as_of ?? null

  let state: ConnState
  let detail: string
  if (!wsConnected) {
    state = lastSnapshotAt == null ? 'connecting' : 'offline'
    detail = state === 'connecting' ? 'Connecting to the trading engine…' : 'Disconnected — retrying every few seconds.'
  } else if (ageMs != null && ageMs > FROZEN_AFTER_MS) {
    state = 'stale'
    detail = `No update from the engine in ${fmtAge(ageSeconds)} — the connection may be frozen.`
  } else if (fresh?.stale) {
    state = 'stale'
    detail = 'Market is open but the tick feed has gone silent — prices may be frozen.'
  } else if (fresh && !fresh.market_open) {
    state = 'closed'
    detail = `Market is closed (${marketStatus ?? 'off-hours'}). Data updates resume at the next session.`
  } else {
    state = 'live'
    const tickAge = fresh?.freshest_tick_age_seconds
    detail = tickAge != null ? `Live — freshest tick ${fmtAge(Math.floor(tickAge))}.` : 'Live and streaming.'
  }

  return { state, label: LABEL[state], tone: TONE[state], ageSeconds, marketStatus, asOf, detail }
}
