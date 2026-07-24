import { clsx } from 'clsx'
import type { ReactNode } from 'react'

interface CardProps {
  children: ReactNode
  className?: string
  elevated?: boolean
  onClick?: () => void
}

export function Card({ children, className, elevated, onClick }: CardProps) {
  return (
    <div
      onClick={onClick}
      className={clsx(
        'rounded-xl border shadow-card',
        elevated
          ? 'bg-surface-elevated border-surface-border-strong'
          : 'bg-surface-card border-surface-border',
        onClick && 'cursor-pointer transition-all hover:border-surface-border-strong hover:shadow-card-hover',
        className,
      )}
    >
      {children}
    </div>
  )
}

interface CardHeaderProps {
  title: string
  subtitle?: string
  action?: ReactNode
  icon?: ReactNode
}

export function CardHeader({ title, subtitle, action, icon }: CardHeaderProps) {
  return (
    <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-surface-border">
      <div className="flex items-center gap-2.5 min-w-0">
        {icon && (
          <span className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-lg bg-surface-inset text-ink-muted">
            {icon}
          </span>
        )}
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-ink leading-tight truncate">{title}</h3>
          {subtitle && <p className="text-xs text-ink-faint mt-0.5 truncate">{subtitle}</p>}
        </div>
      </div>
      {action && <div className="flex-shrink-0">{action}</div>}
    </div>
  )
}

export function CardBody({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={clsx('p-4', className)}>{children}</div>
}
