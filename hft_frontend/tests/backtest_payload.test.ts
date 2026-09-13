import { describe, expect, it } from 'vitest'
import { buildBacktestPayload } from '../src/views/Backtest'

// Regression for audit_results/2026-09-13/AUDIT_REPORT.md finding #10
// (medium): the backtest dashboard sent `strategy_name` (singular) while
// api/runtime.py's run_backtest reads `strategy_names` (a list) -- the
// filter was silently a no-op because the backend never saw the key it
// was looking for.
describe('buildBacktestPayload', () => {
  it('sends strategy_names as a list, matching the backend contract', () => {
    const payload = buildBacktestPayload({
      capital: 1_000_000,
      startDate: '2026-08-01',
      days: 30,
      underlyings: ['NIFTY', 'BANKNIFTY'],
      strategyFilter: 'short_vol_condor',
    })

    expect(payload).toHaveProperty('strategy_names')
    expect(payload).not.toHaveProperty('strategy_name')
    expect(payload.strategy_names).toEqual(['short_vol_condor'])
  })

  it('omits strategy_names entirely when no filter is set, not an empty/undefined singular field', () => {
    const payload = buildBacktestPayload({
      capital: 1_000_000,
      startDate: '2026-08-01',
      days: 30,
      underlyings: ['NIFTY'],
      strategyFilter: '',
    })

    expect(payload.strategy_names).toBeUndefined()
  })

  it('passes through the other fields unchanged', () => {
    const payload = buildBacktestPayload({
      capital: 2_500_000,
      startDate: '2026-01-15',
      days: 60,
      underlyings: ['RELIANCE'],
      strategyFilter: '',
    })

    expect(payload.starting_capital).toBe(2_500_000)
    expect(payload.start).toBe('2026-01-15')
    expect(payload.days).toBe(60)
    expect(payload.underlyings).toEqual(['RELIANCE'])
  })
})
