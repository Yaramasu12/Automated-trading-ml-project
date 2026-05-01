import assert from 'node:assert/strict';
import { buildStatusModel, modeCopy, MODES } from '../src/components/Dashboard.js';

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

console.log('dashboard tests passed');

