import { clsx } from 'clsx'

type Variant =
  | 'green'
  | 'red'
  | 'yellow'
  | 'blue'
  | 'purple'
  | 'cyan'
  | 'orange'
  | 'gray'

const variantMap: Record<Variant, string> = {
  green: 'bg-brand-green/12 text-brand-green border-brand-green/25',
  red: 'bg-brand-red/12 text-brand-red border-brand-red/25',
  yellow: 'bg-brand-yellow/12 text-brand-yellow border-brand-yellow/25',
  blue: 'bg-brand-blue/12 text-brand-blue border-brand-blue/25',
  purple: 'bg-brand-purple/12 text-brand-purple border-brand-purple/25',
  cyan: 'bg-brand-cyan/12 text-brand-cyan border-brand-cyan/25',
  orange: 'bg-brand-orange/12 text-brand-orange border-brand-orange/25',
  gray: 'bg-surface-inset text-ink-muted border-surface-border-strong',
}

const dotColor: Record<Variant, string> = {
  green: 'bg-brand-green',
  red: 'bg-brand-red',
  yellow: 'bg-brand-yellow',
  blue: 'bg-brand-blue',
  purple: 'bg-brand-purple',
  cyan: 'bg-brand-cyan',
  orange: 'bg-brand-orange',
  gray: 'bg-ink-faint',
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
        'inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-semibold border whitespace-nowrap',
        variantMap[variant],
        className,
      )}
    >
      {dot && (
        <span className="relative flex h-1.5 w-1.5">
          <span className={clsx('absolute inline-flex h-full w-full rounded-full opacity-60 animate-ping-slow', dotColor[variant])} />
          <span className={clsx('relative inline-flex h-1.5 w-1.5 rounded-full', dotColor[variant])} />
        </span>
      )}
      {children}
    </span>
  )
}

export function execModeBadge(mode: string) {
  if (mode.startsWith('LIVE')) return <Badge variant="red" dot>{mode}</Badge>
  if (mode === 'SHADOW_LIVE') return <Badge variant="orange" dot>SHADOW</Badge>
  if (mode === 'PAPER') return <Badge variant="yellow" dot>PAPER</Badge>
  if (mode === '...') return <Badge variant="gray">…</Badge>
  return <Badge variant="blue">BACKTEST</Badge>
}

export function regimeBadge(regime: string) {
  const lower = regime.toLowerCase()
  if (lower.includes('bull') || lower.includes('trending_up')) return <Badge variant="green">{regime}</Badge>
  if (lower.includes('bear') || lower.includes('trending_down')) return <Badge variant="red">{regime}</Badge>
  if (lower.includes('volatile') || lower.includes('high_vol')) return <Badge variant="orange">{regime}</Badge>
  if (lower.includes('event_risk')) return <Badge variant="red">{regime}</Badge>
  if (lower.includes('low_vol') || lower.includes('calm')) return <Badge variant="cyan">{regime}</Badge>
  return <Badge variant="gray">{regime}</Badge>
}

export function statusBadge(status: string) {
  const upper = status.toUpperCase()
  if (upper === 'HEALTHY' || upper === 'ACTIVE' || upper === 'CHAMPION') return <Badge variant="green">{status}</Badge>
  if (upper === 'DEGRADED' || upper === 'CANDIDATE') return <Badge variant="yellow">{status}</Badge>
  if (upper === 'HALTED' || upper === 'INACTIVE') return <Badge variant="red">{status}</Badge>
  return <Badge variant="gray">{status}</Badge>
}

interface TagProps {
  label: string
  color?: Variant
}

export function Tag({ label, color = 'gray' }: TagProps) {
  return <Badge variant={color}>{label}</Badge>
}
