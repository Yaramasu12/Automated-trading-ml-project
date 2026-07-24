import { clsx } from 'clsx'
import { isValidElement, type ReactNode } from 'react'

export interface Column<T> {
  key: string
  header?: string
  label?: string
  render?: (value: unknown, row: T) => ReactNode
  align?: 'left' | 'right' | 'center'
  className?: string
}

interface TableProps<T> {
  columns: Column<T>[]
  data?: T[]
  rows?: T[]
  keyFn: (row: T, index: number) => string
  emptyText?: string
  emptyMessage?: string
  className?: string
  compact?: boolean
}

export function Table<T>({
  columns,
  data,
  rows,
  keyFn,
  emptyText,
  emptyMessage,
  className,
  compact,
}: TableProps<T>) {
  const items = rows ?? data ?? []
  const empty = emptyMessage ?? emptyText ?? 'No data'

  return (
    <div className={clsx('overflow-x-auto', className)}>
      <table className="w-full text-sm border-collapse">
        <thead>
          <tr className="border-b border-surface-border">
            {columns.map((col) => (
              <th
                key={col.key}
                className={clsx(
                  'font-semibold text-ink-faint uppercase tracking-wider text-[10px] bg-surface-inset/40',
                  compact ? 'px-3 py-2' : 'px-4 py-2.5',
                  col.align === 'right' && 'text-right',
                  col.align === 'center' && 'text-center',
                  !col.align && 'text-left',
                  col.className,
                )}
              >
                {col.label ?? col.header ?? col.key}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {items.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="text-center text-ink-faint py-10 text-xs">
                {empty}
              </td>
            </tr>
          ) : (
            items.map((row, index) => (
              <tr
                key={keyFn(row, index)}
                className="border-b border-surface-border/60 last:border-0 hover:bg-surface-elevated/60 transition-colors"
              >
                {columns.map((col) => {
                  const value = (row as Record<string, unknown>)[col.key]
                  return (
                    <td
                      key={col.key}
                      className={clsx(
                        'text-ink-muted font-mono',
                        compact ? 'px-3 py-2' : 'px-4 py-2.5',
                        col.align === 'right' && 'text-right',
                        col.align === 'center' && 'text-center',
                        col.className,
                      )}
                    >
                      {col.render
                        ? col.render(value, row)
                        : isValidElement(value)
                          ? value
                          : String(value ?? '')}
                    </td>
                  )
                })}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  )
}
