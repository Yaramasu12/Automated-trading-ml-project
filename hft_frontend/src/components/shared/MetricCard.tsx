import { clsx } from 'clsx'
import type { ReactNode } from 'react'
import { TrendingDown, TrendingUp } from 'lucide-react'

interface MetricCardProps {
  label: string
  value: string | number
  sub?: string
  /** Positive number = green arrow up, negative = red arrow down */
  trend?: number
  icon?: ReactNode
  color?: 'green' | 'red' | 'blue' | 'yellow' | 'purple' | 'cyan' | 'gray'
  /** Legacy alias for color */
  accent?: 'green' | 'red' | 'blue' | 'yellow' | 'purple' | 'cyan' | 'gray'
  className?: string
  /** Legacy string trend */
  trendValue?: string
}

const accentBorder: Record<string, string> = {
  green:  'border-l-brand-green',
  red:    'border-l-brand-red',
  blue:   'border-l-brand-blue',
  yellow: 'border-l-brand-yellow',
  purple: 'border-l-brand-purple',
  cyan:   'border-l-brand-cyan',
  gray:   'border-l-gray-600',
}

const colorText: Record<string, string> = {
  green:  'text-brand-green',
  red:    'text-brand-red',
  blue:   'text-brand-blue',
  yellow: 'text-brand-yellow',
  purple: 'text-brand-purple',
  cyan:   'text-brand-cyan',
  gray:   'text-gray-400',
}

export function MetricCard({ label, value, sub, trend, trendValue, icon, color, accent, className }: MetricCardProps) {
  const resolvedColor = color ?? accent
  const trendDir = typeof trend === 'number' ? (trend > 0 ? 'up' : trend < 0 ? 'down' : 'neutral') : undefined

  return (
    <div
      className={clsx(
        'bg-surface-card border border-surface-border rounded-lg p-4 flex flex-col gap-1',
        resolvedColor && `border-l-2 ${accentBorder[resolvedColor]}`,
        className,
      )}
    >
      <div className="flex items-center justify-between">
        <span className="text-xs text-gray-500 font-medium uppercase tracking-wider">{label}</span>
        {icon && <span className="text-gray-500">{icon}</span>}
      </div>

      <div className="flex items-end gap-2 mt-1">
        <span className={clsx(
          'text-xl font-bold font-mono leading-none',
          resolvedColor ? colorText[resolvedColor] : 'text-gray-100',
        )}>
          {value}
        </span>
        {trendDir && trendDir !== 'neutral' && (
          <span className={clsx(
            'flex items-center gap-0.5 text-xs font-medium mb-0.5',
            trendDir === 'up'   && 'text-brand-green',
            trendDir === 'down' && 'text-brand-red',
          )}>
            {trendDir === 'up' ? <TrendingUp size={12} /> : <TrendingDown size={12} />}
            {trendValue}
          </span>
        )}
        {/* Legacy trendValue without numeric trend */}
        {!trendDir && trendValue && (
          <span className="flex items-center gap-0.5 text-xs font-medium mb-0.5 text-gray-400">
            {trendValue}
          </span>
        )}
      </div>

      {sub && <span className="text-xs text-gray-500 truncate">{sub}</span>}
    </div>
  )
}
