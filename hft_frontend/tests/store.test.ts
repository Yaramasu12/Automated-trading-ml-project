import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useStore } from '../src/store'
import { getBatchTicks } from '../src/api'
import type { WsDashboardMessage } from '../src/types'

function snapshot(overrides: Partial<WsDashboardMessage> = {}): WsDashboardMessage {
  return {
    type: 'snapshot',
    timestamp: '2026-07-05T10:00:00Z',
    authenticated: true,
    state: {
      execution_mode: 'PAPER',
      live_armed: false,
      kill_switch_active: false,
    } as WsDashboardMessage['state'],
    monitoring: { status: 'ok' } as WsDashboardMessage['monitoring'],
    live_feed: { running: true } as WsDashboardMessage['live_feed'],
    ...overrides,
  }
}

describe('applyWsSnapshot', () => {
  it('stores auth-gated fields when the snapshot includes them', () => {
    const db = { trades: 5 } as unknown as NonNullable<WsDashboardMessage['db']>
    useStore.getState().applyWsSnapshot(snapshot({ db }))
    expect(useStore.getState().dbSummary).toEqual(db)
    expect(useStore.getState().runtimeState?.execution_mode).toBe('PAPER')
  })

  it('clears auth-gated fields when an unauthenticated snapshot omits them', () => {
    useStore.getState().applyWsSnapshot(
      snapshot({ db: { trades: 5 } as unknown as NonNullable<WsDashboardMessage['db']> }),
    )
    useStore.getState().applyWsSnapshot(snapshot({ authenticated: false }))
    expect(useStore.getState().dbSummary).toBeNull()
    expect(useStore.getState().livePortfolio).toBeNull()
    // Public fields still update
    expect(useStore.getState().runtimeState?.execution_mode).toBe('PAPER')
  })
})

describe('getBatchTicks', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  function mockFetch() {
    return vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => {
      const body = JSON.parse(String((init as RequestInit).body))
      const ticks: Record<string, unknown> = {}
      for (const sym of body.symbols) ticks[sym] = { symbol: sym, available: true }
      return new Response(JSON.stringify({ ticks }), { status: 200 })
    })
  }

  it('uppercases, trims, and dedupes symbols into one request', async () => {
    const spy = mockFetch()
    const res = await getBatchTicks([' nifty ', 'NIFTY', 'banknifty', ''])
    expect(spy).toHaveBeenCalledTimes(1)
    const sent = JSON.parse(String((spy.mock.calls[0][1] as RequestInit).body))
    expect(sent.symbols).toEqual(['NIFTY', 'BANKNIFTY'])
    expect(Object.keys(res.ticks)).toEqual(['NIFTY', 'BANKNIFTY'])
  })

  it('chunks large symbol lists (250 per request) and merges the results', async () => {
    const spy = mockFetch()
    const symbols = Array.from({ length: 300 }, (_, i) => `SYM${i}`)
    const res = await getBatchTicks(symbols)
    expect(spy).toHaveBeenCalledTimes(2)
    expect(Object.keys(res.ticks)).toHaveLength(300)
  })
})
