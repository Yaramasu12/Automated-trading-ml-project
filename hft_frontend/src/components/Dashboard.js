import { armLive, getHealth, runBacktest, setKillSwitch } from '../services/api.js';

export const MODES = ['BACKTEST', 'PAPER', 'LIVE'];

export function modeCopy(mode, liveArmed = false) {
  if (mode === 'LIVE') {
    return liveArmed ? 'Live armed' : 'Live locked';
  }
  if (mode === 'PAPER') {
    return 'Paper execution';
  }
  return 'Backtest mode';
}

export function buildStatusModel(health) {
  const state = health?.state || {};
  return {
    mode: state.execution_mode || 'BACKTEST',
    broker: state.broker || 'ANGEL_ONE',
    liveArmed: Boolean(state.live_armed),
    killSwitch: Boolean(state.kill_switch_active),
    angelOneConfigured: Boolean(state.angel_one_configured),
    drawdownLimit: health?.risk_limits?.max_drawdown ?? 0.1,
    statusText: modeCopy(state.execution_mode || 'BACKTEST', Boolean(state.live_armed)),
  };
}

export function renderDashboard(root) {
  if (!root) {
    return;
  }
  const state = {
    status: null,
    backtest: null,
    error: '',
    loading: false,
  };

  root.innerHTML = `
    <section class="shell">
      <header class="topbar">
        <div>
          <h1>AI Trading Control</h1>
          <p>NSE/BSE equities, futures, and options with shared backtest, paper, and live controls.</p>
        </div>
        <div class="status-pill" data-role="mode">Loading</div>
      </header>

      <section class="metrics">
        <article><span>Broker</span><strong data-role="broker">-</strong></article>
        <article><span>Live</span><strong data-role="live">-</strong></article>
        <article><span>Kill Switch</span><strong data-role="kill">-</strong></article>
        <article><span>Drawdown Limit</span><strong data-role="drawdown">-</strong></article>
      </section>

      <section class="controls">
        <button data-action="refresh">Refresh</button>
        <button data-action="backtest">Run 1M Backtest</button>
        <button data-action="arm">Arm Live</button>
        <button data-action="disarm">Disarm</button>
        <button data-action="halt" class="danger">Kill Switch</button>
      </section>

      <section class="result" data-role="result"></section>
      <section class="error" data-role="error"></section>
    </section>
  `;

  const paint = () => {
    const model = buildStatusModel(state.status);
    root.querySelector('[data-role="mode"]').textContent = model.statusText;
    root.querySelector('[data-role="broker"]').textContent = model.broker;
    root.querySelector('[data-role="live"]').textContent = model.liveArmed ? 'Armed' : 'Locked';
    root.querySelector('[data-role="kill"]').textContent = model.killSwitch ? 'Active' : 'Clear';
    root.querySelector('[data-role="drawdown"]').textContent = `${(model.drawdownLimit * 100).toFixed(1)}%`;
    root.querySelector('[data-role="error"]').textContent = state.error;
    const result = root.querySelector('[data-role="result"]');
    if (state.backtest) {
      const metrics = state.backtest.metrics;
      result.innerHTML = `
        <h2>Latest Backtest</h2>
        <dl>
          <div><dt>PnL</dt><dd>${formatCurrency(metrics.total_pnl)}</dd></div>
          <div><dt>Return</dt><dd>${formatPct(metrics.return_pct)}</dd></div>
          <div><dt>Max Drawdown</dt><dd>${formatPct(metrics.max_drawdown)}</dd></div>
          <div><dt>Trades</dt><dd>${metrics.trade_count}</dd></div>
          <div><dt>Profit Factor</dt><dd>${metrics.profit_factor.toFixed(2)}</dd></div>
        </dl>
      `;
    }
  };

  const load = async () => {
    state.error = '';
    try {
      state.status = await getHealth();
    } catch (error) {
      state.error = error.message;
    }
    paint();
  };

  root.querySelector('[data-action="refresh"]').addEventListener('click', load);
  root.querySelector('[data-action="backtest"]').addEventListener('click', async () => {
    state.error = '';
    try {
      state.backtest = await runBacktest({ days: 30 });
      state.status = await getHealth();
    } catch (error) {
      state.error = error.message;
    }
    paint();
  });
  root.querySelector('[data-action="arm"]').addEventListener('click', async () => {
    state.error = '';
    try {
      state.status = { state: await armLive(true), risk_limits: state.status?.risk_limits };
    } catch (error) {
      state.error = error.message;
    }
    paint();
  });
  root.querySelector('[data-action="disarm"]').addEventListener('click', async () => {
    state.error = '';
    try {
      state.status = { state: await armLive(false), risk_limits: state.status?.risk_limits };
    } catch (error) {
      state.error = error.message;
    }
    paint();
  });
  root.querySelector('[data-action="halt"]').addEventListener('click', async () => {
    state.error = '';
    try {
      state.status = { state: await setKillSwitch(true), risk_limits: state.status?.risk_limits };
    } catch (error) {
      state.error = error.message;
    }
    paint();
  });

  load();
}

function formatCurrency(value) {
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
    maximumFractionDigits: 0,
  }).format(value || 0);
}

function formatPct(value) {
  return `${((value || 0) * 100).toFixed(2)}%`;
}

