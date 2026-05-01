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

