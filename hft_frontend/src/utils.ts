// Missing/NaN-safe formatters. Previously inr()/num() rendered "₹NaN" and
// pct() THREW (undefined.toFixed) — blanking whole pages — when a value was
// missing. All now show an em-dash for non-finite input.
const PLACEHOLDER = '—'

export function inr(value: number | null | undefined, decimals = 0): string {
  if (value == null || !Number.isFinite(value)) return PLACEHOLDER
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
    maximumFractionDigits: decimals,
    minimumFractionDigits: decimals,
  }).format(value)
}

export function pct(value: number | null | undefined, decimals = 2): string {
  if (value == null || !Number.isFinite(value)) return PLACEHOLDER
  return `${value >= 0 ? '+' : ''}${value.toFixed(decimals)}%`
}

export function num(value: number | null | undefined, decimals = 2): string {
  if (value == null || !Number.isFinite(value)) return PLACEHOLDER
  return new Intl.NumberFormat('en-IN', {
    maximumFractionDigits: decimals,
    minimumFractionDigits: decimals,
  }).format(value)
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return PLACEHOLDER
  const d = new Date(iso)
  if (isNaN(d.getTime())) return PLACEHOLDER
  return d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' })
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return PLACEHOLDER
  const d = new Date(iso)
  if (isNaN(d.getTime())) return PLACEHOLDER
  return d.toLocaleTimeString('en-IN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  })
}

export function fmtDateTime(iso: string): string {
  return `${fmtDate(iso)} ${fmtTime(iso)}`
}

export function fmtLatency(ms: number): string {
  if (ms < 1000) return `${ms.toFixed(1)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

export function fmtUptime(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max)
}

export function pctOf(part: number, total: number): number {
  return total === 0 ? 0 : (part / total) * 100
}

export function colorForPnl(value: number): string {
  if (value > 0) return 'text-brand-green'
  if (value < 0) return 'text-brand-red'
  return 'text-gray-400'
}

export function colorForPct(value: number): string {
  if (value > 0) return 'text-brand-green'
  if (value < 0) return 'text-brand-red'
  return 'text-gray-400'
}
