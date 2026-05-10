import { clsx } from 'clsx'

type Variant = 'green' | 'red' | 'yellow' | 'blue' | 'purple' | 'cyan' | 'orange' | 'gray'

const variantMap: Record<Variant, string> = {
  green:  'bg-brand-green/15 text-brand-green border-brand-green/30',
  red:    'bg-brand-red/15 text-brand-red border-brand-red/30',
  yellow: 'bg-brand-yellow/15 text-brand-yellow border-brand-yellow/30',
  blue:   'bg-brand-blue/15 text-brand-blue border-brand-blue/30',
  purple: 'bg-brand-purple/15 text-brand-purple border-brand-purple/30',
  cyan:   'bg-brand-cyan/15 text-brand-cyan border-brand-cyan/30',
  orange: 'bg-brand-orange/15 text-brand-orange border-brand-orange/30',
  gray:   'bg-gray-700/40 text-gray-400 border-gray-600/30',
}

interface BadgeProps {
  children: React.ReactNode
  variant?: Variant
  dot?: boolean
  className?: string
}

export function Badge({ children, variant = 'gray', dot, className }: BadgeProps) {
  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium border',
        variantMap[variant],
        className,
      )}
    >
      {dot && (
        <span
          className={clsx(
            'w-1.5 h-1.5 rounded-full animate-pulse-slow',
            variant === 'green'  && 'bg-brand-green',
            variant === 'red'    && 'bg-brand-red',
            variant === 'yellow' && 'bg-brand-yellow',
            variant === 'blue'   && 'bg-brand-blue',
            variant === 'purple' && 'bg-brand-purple',
            variant === 'cyan'   && 'bg-brand-cyan',
            variant === 'orange' && 'bg-brand-orange',
            variant === 'gray'   && 'bg-gray-500',
          )}
        />
      )}
      {children}
    </span>
  )
}

export function execModeBadge(mode: string) {
  if (mode.startsWith('LIVE')) return <Badge variant="red" dot>{mode}</Badge>
  if (mode === 'SHADOW_LIVE')  return <Badge variant="orange" dot>SHADOW</Badge>
  if (mode === 'PAPER')        return <Badge variant="yellow" dot>PAPER</Badge>
  return <Badge variant="blue">BACKTEST</Badge>
}

export function regimeBadge(regime: string) {
  const lower = regime.toLowerCase()
  if (lower.includes('bull') || lower.includes('trending_up'))   return <Badge variant="green">{regime}</Badge>
  if (lower.includes('bear') || lower.includes('trending_down')) return <Badge variant="red">{regime}</Badge>
  if (lower.includes('volatile') || lower.includes('high_vol'))  return <Badge variant="orange">{regime}</Badge>
  if (lower.includes('event_risk'))                              return <Badge variant="red">{regime}</Badge>
  if (lower.includes('low_vol') || lower.includes('calm'))       return <Badge variant="cyan">{regime}</Badge>
  return <Badge variant="gray">{regime}</Badge>
}

export function statusBadge(status: string) {
  const upper = status.toUpperCase()
  if (upper === 'HEALTHY' || upper === 'ACTIVE' || upper === 'CHAMPION') return <Badge variant="green">{status}</Badge>
  if (upper === 'DEGRADED' || upper === 'CANDIDATE')                      return <Badge variant="yellow">{status}</Badge>
  if (upper === 'HALTED' || upper === 'INACTIVE')                         return <Badge variant="red">{status}</Badge>
  return <Badge variant="gray">{status}</Badge>
}

// ── New exports per spec ──────────────────────────────────────────────────────

type StatusDotStatus = 'healthy' | 'degraded' | 'halted' | 'live' | 'paper' | 'off'

const dotColorMap: Record<StatusDotStatus, string> = {
  healthy:  'bg-brand-green',
  live:     'bg-brand-green',
  degraded: 'bg-brand-yellow',
  paper:    'bg-brand-yellow',
  halted:   'bg-brand-red',
  off:      'bg-gray-600',
}

export function StatusDot({ status }: { status: StatusDotStatus }) {
  return (
    <span className={clsx(
      'inline-block w-2 h-2 rounded-full',
      dotColorMap[status],
      (status === 'healthy' || status === 'live') && 'animate-pulse-slow',
    )} />
  )
}

type TagColor = 'green' | 'red' | 'blue' | 'yellow' | 'purple' | 'cyan' | 'orange' | 'gray'

export function Tag({ label, color }: { label: string; color: TagColor }) {
  return <Badge variant={color}>{label}</Badge>
}
