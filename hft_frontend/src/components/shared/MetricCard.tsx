import { clsx } from 'clsx'
import type { ReactNode } from 'react'
import { TrendingDown, TrendingUp } from 'lucide-react'

interface MetricCardProps {
  label: string
  value: string | number
  sub?: string
  trend?: 'up' | 'down' | 'neutral'
  trendValue?: string
  icon?: ReactNode
  accent?: 'green' | 'red' | 'blue' | 'yellow' | 'purple' | 'cyan' | 'gray'
  color?: 'green' | 'red' | 'blue' | 'yellow' | 'purple' | 'cyan' | 'gray'
  className?: string
}

const accentBorder: Record<string, string> = {
  green: 'border-l-brand-green',
  red: 'border-l-brand-red',
  blue: 'border-l-brand-blue',
  yellow: 'border-l-brand-yellow',
  purple: 'border-l-brand-purple',
  cyan: 'border-l-brand-cyan',
  gray: 'border-l-gray-600',
}

export function MetricCard({ label, value, sub, trend, trendValue, icon, accent, color, className }: MetricCardProps) {
  const resolvedAccent = accent ?? color
  return (
    <div
      className={clsx(
        'bg-surface-card border border-surface-border rounded-lg p-4 flex flex-col gap-1',
        resolvedAccent && `border-l-2 ${accentBorder[resolvedAccent]}`,
        className,
      )}
    >
      <div className="flex items-center justify-between">
        <span className="text-xs text-gray-500 font-medium uppercase tracking-wider">{label}</span>
        {icon && <span className="text-gray-500">{icon}</span>}
      </div>

      <div className="flex items-end gap-2 mt-1">
        <span className="text-xl font-bold font-mono text-gray-100 leading-none">{value}</span>
        {trendValue && trend && (
          <span
            className={clsx(
              'flex items-center gap-0.5 text-xs font-medium mb-0.5',
              trend === 'up' && 'text-brand-green',
              trend === 'down' && 'text-brand-red',
              trend === 'neutral' && 'text-gray-500',
            )}
          >
            {trend === 'up' ? <TrendingUp size={12} /> : trend === 'down' ? <TrendingDown size={12} /> : null}
            {trendValue}
          </span>
        )}
      </div>

      {sub && <span className="text-xs text-gray-500 truncate">{sub}</span>}
    </div>
  )
}
