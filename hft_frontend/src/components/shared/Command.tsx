import { clsx } from 'clsx'
import type { ReactNode } from 'react'

type Accent = 'green' | 'red' | 'blue' | 'yellow' | 'purple' | 'cyan' | 'orange' | 'gray'

const accentText: Record<Accent, string> = {
  green: 'text-brand-green',
  red: 'text-brand-red',
  blue: 'text-brand-blue',
  yellow: 'text-brand-yellow',
  purple: 'text-brand-purple',
  cyan: 'text-brand-cyan',
  orange: 'text-brand-orange',
  gray: 'text-ink-muted',
}

const accentBg: Record<Accent, string> = {
  green: 'bg-brand-green/10 border-brand-green/20',
  red: 'bg-brand-red/10 border-brand-red/20',
  blue: 'bg-brand-blue/10 border-brand-blue/20',
  yellow: 'bg-brand-yellow/10 border-brand-yellow/20',
  purple: 'bg-brand-purple/10 border-brand-purple/20',
  cyan: 'bg-brand-cyan/10 border-brand-cyan/20',
  orange: 'bg-brand-orange/10 border-brand-orange/20',
  gray: 'bg-surface-card border-surface-border',
}

export function PageHeader({
  title,
  subtitle,
  icon,
  action,
}: {
  title: string
  subtitle?: string
  icon?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 mb-1">
      <div className="flex items-center gap-3 min-w-0">
        {icon && (
          <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-xl bg-surface-card border border-surface-border text-brand-blue">
            {icon}
          </div>
        )}
        <div className="min-w-0">
          <h1 className="text-lg font-semibold text-ink leading-tight tracking-tight truncate">{title}</h1>
          {subtitle && <p className="text-xs text-ink-faint mt-0.5">{subtitle}</p>}
        </div>
      </div>
      {action && <div className="flex items-center gap-2 flex-shrink-0">{action}</div>}
    </div>
  )
}

export function StatTile({
  label,
  value,
  sub,
  accent = 'gray',
  icon,
}: {
  label: string
  value: ReactNode
  sub?: ReactNode
  accent?: Accent
  icon?: ReactNode
}) {
  return (
    <div className={clsx('rounded-xl border p-3.5 min-w-0 shadow-card', accentBg[accent])}>
      <div className="flex items-center justify-between gap-2">
        <span className="eyebrow truncate">{label}</span>
        {icon && <span className={clsx('shrink-0', accentText[accent])}>{icon}</span>}
      </div>
      <div className={clsx('mt-1.5 text-lg font-bold font-mono leading-none truncate', accentText[accent])}>{value}</div>
      {sub && <div className="mt-1 text-[10px] text-ink-faint truncate">{sub}</div>}
    </div>
  )
}

export function ProgressBar({
  value,
  max = 100,
  accent = 'blue',
  label,
  right,
}: {
  value: number
  max?: number
  accent?: Accent
  label?: ReactNode
  right?: ReactNode
}) {
  const pct = max > 0 ? Math.min(100, Math.max(0, (value / max) * 100)) : 0
  const fill: Record<Accent, string> = {
    green: 'bg-brand-green',
    red: 'bg-brand-red',
    blue: 'bg-brand-blue',
    yellow: 'bg-brand-yellow',
    purple: 'bg-brand-purple',
    cyan: 'bg-brand-cyan',
    orange: 'bg-brand-orange',
    gray: 'bg-surface-border-strong',
  }

  return (
    <div className="space-y-1.5">
      {(label || right) && (
        <div className="flex items-center justify-between gap-2 text-xs">
          <span className="text-ink-muted truncate">{label}</span>
          <span className="font-mono text-ink-muted shrink-0">{right}</span>
        </div>
      )}
      <div className="h-1.5 bg-surface-inset rounded-full overflow-hidden">
        <div className={clsx('h-full rounded-full transition-all', fill[accent])} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

export function KeyValueGrid({
  items,
  columns = 'md:grid-cols-4',
}: {
  items: Array<{ label: string; value: ReactNode; accent?: Accent }>
  columns?: string
}) {
  return (
    <div className={clsx('grid grid-cols-2 gap-3', columns)}>
      {items.map((item) => (
        <div key={item.label} className="bg-surface-inset border border-surface-border rounded-lg p-2.5 min-w-0">
          <div className="eyebrow truncate">{item.label}</div>
          <div className={clsx('text-sm font-bold font-mono mt-1 truncate', item.accent ? accentText[item.accent] : 'text-ink')}>
            {item.value}
          </div>
        </div>
      ))}
    </div>
  )
}

export function JsonPanel({ value }: { value: unknown }) {
  return (
    <pre className="max-h-72 overflow-auto rounded-lg bg-surface-inset border border-surface-border p-3 text-[11px] leading-relaxed text-ink-muted font-mono">
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}
