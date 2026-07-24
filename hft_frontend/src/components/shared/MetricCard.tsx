import { clsx } from 'clsx'
import type { ReactNode } from 'react'
import { TrendingDown, TrendingUp } from 'lucide-react'

type Accent = 'green' | 'red' | 'blue' | 'yellow' | 'purple' | 'cyan' | 'gray'

interface MetricCardProps {
  label: string
  value: string | number
  sub?: string
  trend?: 'up' | 'down' | 'neutral'
  trendValue?: string
  icon?: ReactNode
  accent?: Accent
  color?: Accent
  className?: string
}

const accentBar: Record<Accent, string> = {
  green: 'bg-brand-green',
  red: 'bg-brand-red',
  blue: 'bg-brand-blue',
  yellow: 'bg-brand-yellow',
  purple: 'bg-brand-purple',
  cyan: 'bg-brand-cyan',
  gray: 'bg-surface-border-strong',
}

const accentText: Record<Accent, string> = {
  green: 'text-brand-green',
  red: 'text-brand-red',
  blue: 'text-brand-blue',
  yellow: 'text-brand-yellow',
  purple: 'text-brand-purple',
  cyan: 'text-brand-cyan',
  gray: 'text-ink-faint',
}

export function MetricCard({ label, value, sub, trend, trendValue, icon, accent, color, className }: MetricCardProps) {
  const resolved = accent ?? color
  return (
    <div
      className={clsx(
        'relative overflow-hidden rounded-xl border border-surface-border bg-surface-card p-4 shadow-card',
        'flex flex-col gap-1.5',
        className,
      )}
    >
      {resolved && <span className={clsx('absolute inset-y-0 left-0 w-[3px]', accentBar[resolved])} />}
      <div className="flex items-center justify-between">
        <span className="eyebrow">{label}</span>
        {icon && <span className={clsx(resolved ? accentText[resolved] : 'text-ink-faint')}>{icon}</span>}
      </div>

      <div className="flex items-end gap-2">
        <span className="text-2xl font-bold font-mono text-ink leading-none tracking-tight">{value}</span>
        {trendValue && trend && (
          <span
            className={clsx(
              'flex items-center gap-0.5 text-xs font-semibold mb-0.5',
              trend === 'up' && 'text-brand-green',
              trend === 'down' && 'text-brand-red',
              trend === 'neutral' && 'text-ink-faint',
            )}
          >
            {trend === 'up' ? <TrendingUp size={13} /> : trend === 'down' ? <TrendingDown size={13} /> : null}
            {trendValue}
          </span>
        )}
      </div>

      {sub && <span className="text-xs text-ink-faint truncate">{sub}</span>}
    </div>
  )
}
