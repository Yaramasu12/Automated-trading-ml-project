import { clsx } from 'clsx'
import type { ReactNode } from 'react'

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
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-surface-border">
            {columns.map((col) => (
              <th
                key={col.key}
                className={clsx(
                  'font-medium text-gray-500 uppercase tracking-wider text-xs',
                  compact ? 'px-3 py-2' : 'px-4 py-3',
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
              <td colSpan={columns.length} className="text-center text-gray-500 py-8 text-xs">
                {empty}
              </td>
            </tr>
          ) : (
            items.map((row, index) => (
              <tr
                key={keyFn(row, index)}
                className="border-b border-surface-border/50 hover:bg-surface-elevated/50 transition-colors"
              >
                {columns.map((col) => {
                  const value = (row as Record<string, unknown>)[col.key]
                  return (
                    <td
                      key={col.key}
                      className={clsx(
                        'text-gray-300 font-mono',
                        compact ? 'px-3 py-2' : 'px-4 py-3',
                        col.align === 'right' && 'text-right',
                        col.align === 'center' && 'text-center',
                        col.className,
                      )}
                    >
                      {col.render ? col.render(value, row) : String(value ?? '')}
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
