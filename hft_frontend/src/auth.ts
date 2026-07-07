// API token handling (audit fix H4).
//
// The token is entered at runtime and kept in localStorage — it must NEVER be
// baked into the bundle via VITE_* env vars: Vite inlines those at build time,
// so anyone who can load the dashboard JS (the dev server binds 0.0.0.0) would
// own the full control-plane token, including kill switch and live arming.

const STORAGE_KEY = 'trading_api_token'

export function getApiToken(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? ''
  } catch {
    return ''
  }
}

export function setApiToken(token: string): void {
  try {
    if (token) localStorage.setItem(STORAGE_KEY, token)
    else localStorage.removeItem(STORAGE_KEY)
  } catch {
    // storage unavailable (private mode) — requests will go out unauthenticated
  }
}

export function promptForApiToken(): void {
  const current = getApiToken()
  const entered = window.prompt(
    'API token (stored only in this browser):',
    current ? '••••••••' : '',
  )
  if (entered === null) return            // cancelled
  if (entered === '••••••••') return      // unchanged
  setApiToken(entered.trim())
  window.location.reload()
}
