import { clsx } from 'clsx'
import type { ReactNode } from 'react'

/** Empty-state placeholder — a calm, explanatory panel instead of a bare "No data". */
export function EmptyState({
  icon,
  title,
  hint,
  className,
  action,
}: {
  icon?: ReactNode
  title: string
  hint?: string
  className?: string
  action?: ReactNode
}) {
  return (
    <div className={clsx('flex flex-col items-center justify-center gap-2 px-6 py-10 text-center', className)}>
      {icon && (
        <div className="flex h-11 w-11 items-center justify-center rounded-full bg-surface-inset text-ink-faint">
          {icon}
        </div>
      )}
      <p className="text-sm font-medium text-ink-muted">{title}</p>
      {hint && <p className="text-xs text-ink-faint max-w-xs">{hint}</p>}
      {action && <div className="mt-1">{action}</div>}
    </div>
  )
}

/** Shimmering skeleton block for loading states. */
export function Skeleton({ className }: { className?: string }) {
  return <div className={clsx('rounded-md bg-surface-elevated animate-shimmer', className)} />
}

export function SkeletonText({ lines = 3, className }: { lines?: number; className?: string }) {
  return (
    <div className={clsx('space-y-2', className)}>
      {Array.from({ length: lines }).map((_, i) => (
        <Skeleton key={i} className={clsx('h-3', i === lines - 1 ? 'w-2/3' : 'w-full')} />
      ))}
    </div>
  )
}
