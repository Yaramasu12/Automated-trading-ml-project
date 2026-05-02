const API_BASE = globalThis.TRADING_API_BASE || 'http://localhost:8000';

export async function getHealth() {
  return request('/health');
}

export async function runBacktest(payload) {
  return request('/backtests/run', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function getDataStatus() {
  return request('/data/status');
}

export async function getAccountStatus() {
  return request('/account/status');
}

export async function getAccountSnapshot() {
  return request('/account/snapshot');
}

export async function getMonitoringMetrics() {
  return request('/monitoring/metrics');
}

export async function getStrategyCatalog() {
  return request('/strategies/catalog');
}

export async function evaluateStrategies(payload) {
  return request('/strategies/evaluate', {
    method: 'POST',
    body: JSON.stringify(payload || {}),
  });
}

export async function runModelCheck(payload) {
  return request('/models/volatility-forecast', {
    method: 'POST',
    body: JSON.stringify(payload || {}),
  });
}

export async function scanSignals(payload) {
  return request('/signals/scan', {
    method: 'POST',
    body: JSON.stringify(payload || {}),
  });
}

export async function runShadow(payload) {
  return request('/shadow/run', {
    method: 'POST',
    body: JSON.stringify(payload || {}),
  });
}

export async function getTargetProgress(payload) {
  return request('/portfolio/target-progress', {
    method: 'POST',
    body: JSON.stringify(payload || {}),
  });
}

export async function refreshInstruments() {
  return request('/data/instruments/refresh', {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

export async function setExecutionMode(mode) {
  return request('/execution-mode', {
    method: 'POST',
    body: JSON.stringify({ mode }),
  });
}

export async function previewOrder(payload) {
  return request('/orders/preview', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function paperOrder(payload) {
  return request('/orders/paper', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function armLive(armed) {
  return request('/live/arm', {
    method: 'POST',
    body: JSON.stringify({ armed }),
  });
}

export async function setKillSwitch(active) {
  return request('/kill-switch', {
    method: 'POST',
    body: JSON.stringify({ active }),
  });
}

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `Request failed: ${response.status}`);
  }
  return response.json();
}
