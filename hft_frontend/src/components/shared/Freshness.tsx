import { clsx } from 'clsx'
import { AlertTriangle, Wifi, WifiOff, Moon, Loader2 } from 'lucide-react'
import { useConnectionStatus, fmtAge, type Tone } from '../../freshness'

const TONE_PILL: Record<Tone, string> = {
  green: 'bg-brand-green/12 text-brand-green border-brand-green/25',
  amber: 'bg-brand-yellow/12 text-brand-yellow border-brand-yellow/25',
  red: 'bg-brand-red/12 text-brand-red border-brand-red/25',
  neutral: 'bg-surface-inset text-ink-muted border-surface-border-strong',
}

const TONE_DOT: Record<Tone, string> = {
  green: 'bg-brand-green',
  amber: 'bg-brand-yellow',
  red: 'bg-brand-red',
  neutral: 'bg-ink-faint',
}

/**
 * The single freshness indicator for the whole app. Reads useConnectionStatus so
 * every surface agrees on what "live" means. Shows a pulsing dot only when truly
 * live; amber/red/neutral otherwise, with the age and a human explanation on hover.
 */
export function FreshnessBadge({ className }: { className?: string }) {
  const { state, label, tone, ageSeconds, detail } = useConnectionStatus()
  const Icon =
    state === 'offline' ? WifiOff : state === 'closed' ? Moon : state === 'connecting' ? Loader2 : state === 'stale' ? AlertTriangle : Wifi
  return (
    <span
      title={detail}
      className={clsx(
        'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold',
        TONE_PILL[tone],
        className,
      )}
    >
      {state === 'live' ? (
        <span className="relative flex h-1.5 w-1.5">
          <span className={clsx('absolute inline-flex h-full w-full rounded-full opacity-60 animate-ping-slow', TONE_DOT[tone])} />
          <span className={clsx('relative inline-flex h-1.5 w-1.5 rounded-full', TONE_DOT[tone])} />
        </span>
      ) : (
        <Icon size={12} className={clsx(state === 'connecting' && 'animate-spin')} />
      )}
      <span>{label}</span>
      {state !== 'closed' && ageSeconds != null && ageSeconds >= 3 && (
        <span className="text-ink-faint font-normal">· {fmtAge(ageSeconds)}</span>
      )}
    </span>
  )
}

/**
 * Full-width banner shown only when data cannot be trusted (offline or frozen).
 * Deliberately silent when the market is merely closed — that's expected calm.
 */
export function StaleBanner({ className }: { className?: string }) {
  const { state, detail } = useConnectionStatus()
  if (state !== 'offline' && state !== 'stale') return null
  const isOffline = state === 'offline'
  return (
    <div
      className={clsx(
        'flex items-center gap-2.5 rounded-lg border px-3.5 py-2.5 text-sm animate-fade-in',
        isOffline
          ? 'bg-brand-red/10 border-brand-red/30 text-brand-red'
          : 'bg-brand-yellow/10 border-brand-yellow/30 text-brand-yellow',
        className,
      )}
    >
      {isOffline ? <WifiOff size={16} className="flex-shrink-0" /> : <AlertTriangle size={16} className="flex-shrink-0" />}
      <span className="font-medium">{isOffline ? 'Disconnected from the trading engine.' : 'Data may be stale.'}</span>
      <span className="text-ink-muted hidden sm:inline">{detail}</span>
    </div>
  )
}
