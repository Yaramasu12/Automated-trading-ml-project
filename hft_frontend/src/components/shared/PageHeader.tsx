import type { ReactNode } from 'react'

/** Consistent view header: title + optional subtitle on the left, actions on the right. */
export function PageHeader({
  title,
  subtitle,
  icon,
  actions,
}: {
  title: string
  subtitle?: string
  icon?: ReactNode
  actions?: ReactNode
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 mb-5">
      <div className="flex items-center gap-3 min-w-0">
        {icon && (
          <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-xl bg-surface-card border border-surface-border text-brand-blue">
            {icon}
          </div>
        )}
        <div className="min-w-0">
          <h1 className="text-lg font-semibold text-ink leading-tight tracking-tight">{title}</h1>
          {subtitle && <p className="text-xs text-ink-faint mt-0.5">{subtitle}</p>}
        </div>
      </div>
      {actions && <div className="flex items-center gap-2 flex-shrink-0">{actions}</div>}
    </div>
  )
}
