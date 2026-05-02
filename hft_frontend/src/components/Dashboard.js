import {
  armLive,
  evaluateStrategies,
  getAccountSnapshot,
  getAccountStatus,
  getDataStatus,
  getHealth,
  getMonitoringMetrics,
  getStrategyCatalog,
  getTargetProgress,
  paperOrder,
  previewOrder,
  refreshInstruments,
  runModelCheck,
  runBacktest,
  runShadow,
  scanSignals,
  setExecutionMode,
  setKillSwitch,
} from '../services/api.js';

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

export function buildDataStatusModel(dataStatus) {
  return {
    cacheReady: Boolean(dataStatus?.instrument_cache_exists),
    universeCount: Number(dataStatus?.current_universe_count || 0),
    angelOneConfigured: Boolean(dataStatus?.angel_one_configured),
    cachePath: dataStatus?.instrument_cache_path || '-',
  };
}

export function buildAccountStatusModel(accountStatus) {
  return {
    readOnlyAvailable: Boolean(accountStatus?.read_only_available),
    liveOrdersPossible: Boolean(accountStatus?.live_orders_possible),
    liveArmed: Boolean(accountStatus?.live_armed),
  };
}

export function renderDashboard(root) {
  if (!root) {
    return;
  }
  const state = {
    status: null,
    dataStatus: null,
    accountStatus: null,
    accountSnapshot: null,
    strategyCatalog: null,
    targetProgress: null,
    backtest: null,
    orderPreview: null,
    paperOrder: null,
    strategyEvaluation: null,
    modelCheck: null,
    signalScan: null,
    shadowRun: null,
    monitoring: null,
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
        <article><span>Instrument Cache</span><strong data-role="cache">-</strong></article>
        <article><span>Universe</span><strong data-role="universe">-</strong></article>
        <article><span>Strategies</span><strong data-role="strategies">-</strong></article>
        <article><span>Target Gap</span><strong data-role="target-gap">-</strong></article>
        <article><span>Account Read</span><strong data-role="account-read">-</strong></article>
        <article><span>Live Orders</span><strong data-role="live-orders">-</strong></article>
      </section>

      <section class="controls">
        <button data-action="refresh">Refresh</button>
        <button data-mode="BACKTEST">Backtest</button>
        <button data-mode="PAPER">Paper</button>
        <button data-mode="LIVE">Live</button>
        <button data-action="refresh-instruments">Refresh Instruments</button>
        <button data-action="account">Fetch Account Snapshot</button>
        <button data-action="backtest">Run 1M Backtest</button>
        <button data-action="evaluate-strategies">Rank Strategies</button>
        <button data-action="model-check">Model Check</button>
        <button data-action="ops-metrics">Ops Metrics</button>
        <button data-action="signal-scan">Signal Scan</button>
        <button data-action="shadow-run">Shadow Run</button>
        <button data-action="preview-order">Preview Order</button>
        <button data-action="paper-order">Paper Fill</button>
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
    const dataModel = buildDataStatusModel(state.dataStatus);
    const accountModel = buildAccountStatusModel(state.accountStatus);
    root.querySelector('[data-role="mode"]').textContent = model.statusText;
    root.querySelector('[data-role="broker"]').textContent = model.broker;
    root.querySelector('[data-role="live"]').textContent = model.liveArmed ? 'Armed' : 'Locked';
    root.querySelector('[data-role="kill"]').textContent = model.killSwitch ? 'Active' : 'Clear';
    root.querySelector('[data-role="drawdown"]').textContent = `${(model.drawdownLimit * 100).toFixed(1)}%`;
    root.querySelector('[data-role="cache"]').textContent = dataModel.cacheReady ? 'Ready' : 'Missing';
    root.querySelector('[data-role="universe"]').textContent = dataModel.universeCount.toLocaleString('en-IN');
    root.querySelector('[data-role="strategies"]').textContent = state.strategyCatalog?.count || '-';
    root.querySelector('[data-role="target-gap"]').textContent = formatCurrency(state.targetProgress?.current_gap || 0);
    root.querySelector('[data-role="account-read"]').textContent = accountModel.readOnlyAvailable ? 'Ready' : 'Locked';
    root.querySelector('[data-role="live-orders"]').textContent = accountModel.liveOrdersPossible ? 'Possible' : 'Blocked';
    root.querySelector('[data-role="error"]').textContent = state.error;
    const result = root.querySelector('[data-role="result"]');
    if (state.monitoring) {
      result.innerHTML = `
        <h2>Ops Metrics</h2>
        <dl>
          <div><dt>Status</dt><dd>${state.monitoring.status}</dd></div>
          <div><dt>Uptime</dt><dd>${Math.round(state.monitoring.uptime_seconds)}s</dd></div>
          <div><dt>Total Orders</dt><dd>${state.monitoring.total_orders}</dd></div>
          <div><dt>Rejection Rate</dt><dd>${formatPct(state.monitoring.rejection_rate)}</dd></div>
          <div><dt>Avg Latency</dt><dd>${state.monitoring.average_latency_ms.toFixed(1)} ms</dd></div>
          <div><dt>Events</dt><dd>${state.monitoring.event_count}</dd></div>
        </dl>
      `;
    } else if (state.shadowRun) {
      const rows = state.shadowRun.executions.map((execution) => `
        <tr>
          <td>${execution.underlying}</td>
          <td>${execution.order.strategy_name}</td>
          <td>${execution.order.symbol}</td>
          <td>${execution.order.status}</td>
          <td>${execution.order.latency_ms?.toFixed(1) || '-'}</td>
        </tr>
      `).join('');
      result.innerHTML = `
        <h2>Shadow Run</h2>
        <dl>
          <div><dt>Submitted</dt><dd>${state.shadowRun.submitted_orders}</dd></div>
          <div><dt>Filled</dt><dd>${state.shadowRun.filled_orders}</dd></div>
          <div><dt>Rejected</dt><dd>${state.shadowRun.rejected_orders}</dd></div>
          <div><dt>Latency</dt><dd>${state.shadowRun.average_latency_ms.toFixed(1)} ms</dd></div>
        </dl>
        <table>
          <thead><tr><th>Underlying</th><th>Strategy</th><th>Symbol</th><th>Status</th><th>Latency</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      `;
    } else if (state.signalScan) {
      const rows = state.signalScan.scans.flatMap((scan) => scan.candidates.map((candidate) => `
        <tr>
          <td>${scan.underlying}</td>
          <td>${scan.regime}</td>
          <td>${candidate.strategy_name}</td>
          <td>${candidate.signal?.side || '-'}</td>
          <td>${candidate.risk_decision?.reason || candidate.reason}</td>
        </tr>
      `)).join('');
      result.innerHTML = `
        <h2>Signal Scan</h2>
        <dl>
          <div><dt>Approved</dt><dd>${state.signalScan.approved_candidates}</dd></div>
          <div><dt>Rejected</dt><dd>${state.signalScan.rejected_candidates}</dd></div>
          <div><dt>Submitted</dt><dd>${state.signalScan.submitted_orders}</dd></div>
        </dl>
        <table>
          <thead><tr><th>Underlying</th><th>Regime</th><th>Strategy</th><th>Side</th><th>Risk</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      `;
    } else if (state.modelCheck) {
      const forecast = state.modelCheck.forecast;
      const interval = state.modelCheck.interval_evaluation;
      result.innerHTML = `
        <h2>Model Check</h2>
        <dl>
          <div><dt>Model</dt><dd>${forecast.model_name}</dd></div>
          <div><dt>Daily Vol</dt><dd>${formatPct(forecast.daily_volatility)}</dd></div>
          <div><dt>Annual Vol</dt><dd>${formatPct(forecast.annualized_volatility)}</dd></div>
          <div><dt>Coverage</dt><dd>${formatPct(interval.coverage)}</dd></div>
        </dl>
      `;
    } else if (state.strategyEvaluation) {
      const rows = state.strategyEvaluation.leaderboard.slice(0, 5).map((item) => `
        <tr>
          <td>${item.rank}</td>
          <td>${item.strategy_name}</td>
          <td>${item.family}</td>
          <td>${formatPct(item.metrics.return_pct)}</td>
          <td>${formatPct(item.metrics.max_drawdown)}</td>
          <td>${item.metrics.profit_factor.toFixed(2)}</td>
        </tr>
      `).join('');
      result.innerHTML = `
        <h2>Strategy Leaderboard</h2>
        <table>
          <thead><tr><th>Rank</th><th>Strategy</th><th>Family</th><th>Return</th><th>DD</th><th>PF</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      `;
    } else if (state.orderPreview) {
      result.innerHTML = `
        <h2>Order Preview</h2>
        <dl>
          <div><dt>Decision</dt><dd>${state.orderPreview.approved ? 'Approved' : 'Rejected'}</dd></div>
          <div><dt>Reason</dt><dd>${state.orderPreview.reason}</dd></div>
          <div><dt>Symbol</dt><dd>${state.orderPreview.intent.symbol}</dd></div>
          <div><dt>Notional</dt><dd>${formatCurrency(state.orderPreview.intent.notional_value)}</dd></div>
        </dl>
      `;
    } else if (state.paperOrder) {
      result.innerHTML = `
        <h2>Paper Order</h2>
        <dl>
          <div><dt>Status</dt><dd>${state.paperOrder.order.status}</dd></div>
          <div><dt>Risk</dt><dd>${state.paperOrder.risk_decision.reason}</dd></div>
          <div><dt>Order Id</dt><dd>${state.paperOrder.order.broker_order_id || '-'}</dd></div>
          <div><dt>Equity</dt><dd>${formatCurrency(state.paperOrder.portfolio.equity)}</dd></div>
        </dl>
      `;
    } else if (state.accountSnapshot) {
      const snapshot = state.accountSnapshot.snapshot || {};
      const rms = snapshot.rms?.data || {};
      const holdings = snapshot.holdings?.data || [];
      const positions = snapshot.positions?.data || [];
      result.innerHTML = `
        <h2>Account Snapshot</h2>
        <dl>
          <div><dt>Available Cash</dt><dd>${formatCurrency(Number(rms.availablecash || rms.availableCash || 0))}</dd></div>
          <div><dt>Holdings</dt><dd>${Array.isArray(holdings) ? holdings.length : 0}</dd></div>
          <div><dt>Positions</dt><dd>${Array.isArray(positions) ? positions.length : 0}</dd></div>
          <div><dt>Live Orders</dt><dd>${state.accountSnapshot.live_orders_possible ? 'Possible' : 'Blocked'}</dd></div>
        </dl>
      `;
    } else if (state.backtest) {
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
      const [health, dataStatus, accountStatus] = await Promise.all([
        getHealth(),
        getDataStatus(),
        getAccountStatus(),
      ]);
      const [strategyCatalog, targetProgress] = await Promise.all([
        getStrategyCatalog(),
        getTargetProgress({ start_date: '2026-01-01' }),
      ]);
      state.status = health;
      state.dataStatus = dataStatus;
      state.accountStatus = accountStatus;
      state.strategyCatalog = strategyCatalog;
      state.targetProgress = targetProgress;
    } catch (error) {
      state.error = error.message;
    }
    paint();
  };

  root.querySelector('[data-action="refresh"]').addEventListener('click', load);
  root.querySelectorAll('[data-mode]').forEach((button) => {
    button.addEventListener('click', async () => {
      state.error = '';
      try {
        state.status = { state: await setExecutionMode(button.dataset.mode), risk_limits: state.status?.risk_limits };
        state.accountStatus = await getAccountStatus();
      } catch (error) {
        state.error = error.message;
      }
      paint();
    });
  });
  root.querySelector('[data-action="refresh-instruments"]').addEventListener('click', async () => {
    state.error = '';
    try {
      await refreshInstruments();
      state.dataStatus = await getDataStatus();
    } catch (error) {
      state.error = error.message;
    }
    paint();
  });
  root.querySelector('[data-action="account"]').addEventListener('click', async () => {
    state.error = '';
    try {
      state.accountSnapshot = await getAccountSnapshot();
      state.accountStatus = await getAccountStatus();
      state.backtest = null;
    } catch (error) {
      state.error = error.message;
    }
    paint();
  });
  root.querySelector('[data-action="backtest"]').addEventListener('click', async () => {
    state.error = '';
    try {
      state.backtest = await runBacktest({ days: 30 });
      state.accountSnapshot = null;
      state.orderPreview = null;
      state.paperOrder = null;
      state.strategyEvaluation = null;
      state.modelCheck = null;
      state.signalScan = null;
      state.shadowRun = null;
      state.monitoring = null;
      state.status = await getHealth();
    } catch (error) {
      state.error = error.message;
    }
    paint();
  });
  root.querySelector('[data-action="evaluate-strategies"]').addEventListener('click', async () => {
    state.error = '';
    try {
      state.strategyEvaluation = await evaluateStrategies({
        days: 30,
        underlyings: ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'RELIANCE', 'TCS'],
      });
      state.orderPreview = null;
      state.paperOrder = null;
      state.accountSnapshot = null;
      state.backtest = null;
      state.modelCheck = null;
      state.signalScan = null;
      state.shadowRun = null;
      state.monitoring = null;
    } catch (error) {
      state.error = error.message;
    }
    paint();
  });
  root.querySelector('[data-action="model-check"]').addEventListener('click', async () => {
    state.error = '';
    try {
      state.modelCheck = await runModelCheck({ symbol: 'NIFTY', days: 30, model_name: 'garch_baseline' });
      state.strategyEvaluation = null;
      state.orderPreview = null;
      state.paperOrder = null;
      state.accountSnapshot = null;
      state.backtest = null;
      state.signalScan = null;
      state.shadowRun = null;
      state.monitoring = null;
    } catch (error) {
      state.error = error.message;
    }
    paint();
  });
  root.querySelector('[data-action="ops-metrics"]').addEventListener('click', async () => {
    state.error = '';
    try {
      state.monitoring = await getMonitoringMetrics();
      state.modelCheck = null;
      state.signalScan = null;
      state.shadowRun = null;
      state.strategyEvaluation = null;
      state.orderPreview = null;
      state.paperOrder = null;
      state.accountSnapshot = null;
      state.backtest = null;
    } catch (error) {
      state.error = error.message;
    }
    paint();
  });
  root.querySelector('[data-action="signal-scan"]').addEventListener('click', async () => {
    state.error = '';
    try {
      state.signalScan = await scanSignals({
        days: 30,
        underlyings: ['NIFTY', 'BANKNIFTY', 'RELIANCE'],
      });
      state.modelCheck = null;
      state.strategyEvaluation = null;
      state.orderPreview = null;
      state.paperOrder = null;
      state.accountSnapshot = null;
      state.backtest = null;
      state.shadowRun = null;
      state.monitoring = null;
    } catch (error) {
      state.error = error.message;
    }
    paint();
  });
  root.querySelector('[data-action="shadow-run"]').addEventListener('click', async () => {
    state.error = '';
    try {
      const nextState = await setExecutionMode('PAPER');
      state.status = { state: nextState, risk_limits: state.status?.risk_limits };
      state.accountStatus = await getAccountStatus();
      state.shadowRun = await runShadow({
        days: 30,
        underlyings: ['NIFTY', 'BANKNIFTY', 'RELIANCE'],
      });
      state.signalScan = null;
      state.modelCheck = null;
      state.strategyEvaluation = null;
      state.orderPreview = null;
      state.paperOrder = null;
      state.accountSnapshot = null;
      state.backtest = null;
      state.monitoring = null;
    } catch (error) {
      state.error = error.message;
    }
    paint();
  });
  root.querySelector('[data-action="preview-order"]').addEventListener('click', async () => {
    state.error = '';
    try {
      state.orderPreview = await previewOrder(demoOrderPayload());
      state.paperOrder = null;
      state.accountSnapshot = null;
      state.backtest = null;
      state.strategyEvaluation = null;
      state.modelCheck = null;
      state.signalScan = null;
      state.shadowRun = null;
      state.monitoring = null;
    } catch (error) {
      state.error = error.message;
    }
    paint();
  });
  root.querySelector('[data-action="paper-order"]').addEventListener('click', async () => {
    state.error = '';
    try {
      state.paperOrder = await paperOrder(demoOrderPayload());
      state.orderPreview = null;
      state.accountSnapshot = null;
      state.backtest = null;
      state.strategyEvaluation = null;
      state.modelCheck = null;
      state.signalScan = null;
      state.shadowRun = null;
      state.monitoring = null;
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

function demoOrderPayload() {
  return {
    symbol: 'RELIANCE',
    side: 'BUY',
    quantity: 1,
    price: 2800,
    strategy_name: 'manual_dashboard',
  };
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
