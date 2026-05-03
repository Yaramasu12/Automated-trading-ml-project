import assert from 'node:assert/strict';
import { describe, it } from 'vitest';
import {
  buildAccountStatusModel,
  buildDataStatusModel,
  buildStatusModel,
  modeCopy,
  MODES,
} from '../src/components/Dashboard.js';

describe('dashboard model helpers', () => {
  it('formats runtime mode and status models', () => {
    assert.deepEqual(MODES, ['BACKTEST', 'PAPER', 'LIVE']);
    assert.equal(modeCopy('BACKTEST'), 'Backtest mode');
    assert.equal(modeCopy('PAPER'), 'Paper execution');
    assert.equal(modeCopy('LIVE', false), 'Live locked');
    assert.equal(modeCopy('LIVE', true), 'Live armed');

    const model = buildStatusModel({
      state: {
        execution_mode: 'LIVE',
        broker: 'ANGEL_ONE',
        live_armed: false,
        kill_switch_active: true,
        angel_one_configured: false,
      },
      risk_limits: { max_drawdown: 0.1 },
    });

    assert.equal(model.mode, 'LIVE');
    assert.equal(model.broker, 'ANGEL_ONE');
    assert.equal(model.liveArmed, false);
    assert.equal(model.killSwitch, true);
    assert.equal(model.drawdownLimit, 0.1);
    assert.equal(model.statusText, 'Live locked');
  });

  it('formats data and account readiness models', () => {
    const dataModel = buildDataStatusModel({
      instrument_cache_exists: true,
      current_universe_count: 100403,
      angel_one_configured: false,
      instrument_cache_path: 'data/processed/angel_one_instruments.json',
    });

    assert.equal(dataModel.cacheReady, true);
    assert.equal(dataModel.universeCount, 100403);
    assert.equal(dataModel.angelOneConfigured, false);

    const accountModel = buildAccountStatusModel({
      read_only_available: true,
      live_orders_possible: false,
      live_armed: false,
    });

    assert.equal(accountModel.readOnlyAvailable, true);
    assert.equal(accountModel.liveOrdersPossible, false);
    assert.equal(accountModel.liveArmed, false);
  });
});
