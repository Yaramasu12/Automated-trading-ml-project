import { clsx } from 'clsx'
import type { ReactNode } from 'react'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export interface Column<T = any> {
  key: string
  label?: string
  /** Legacy alias for label */
  header?: string
  render?: (val: unknown, row: T) => ReactNode
  align?: 'left' | 'right' | 'center'
  className?: string
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export interface TableProps<T = any> {
  columns: Column<T>[]
  rows?: T[]
  /** Legacy alias for rows */
  data?: T[]
  keyFn?: (row: T, index?: number) => string
  emptyMessage?: string
  /** Legacy alias for emptyMessage */
  emptyText?: string
  className?: string
  compact?: boolean
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function Table<T = any>({
  columns,
  rows,
  data,
  keyFn,
  emptyMessage,
  emptyText,
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
                  col.align === 'right'  && 'text-right',
                  col.align === 'center' && 'text-center',
                  !col.align             && 'text-left',
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
            items.map((row, idx) => (
              <tr
                key={keyFn ? keyFn(row, idx) : String(idx)}
                className="border-b border-surface-border/50 hover:bg-surface-elevated/50 transition-colors"
              >
                {columns.map((col) => (
                  <td
                    key={col.key}
                    className={clsx(
                      'text-gray-300 font-mono',
                      compact ? 'px-3 py-2' : 'px-4 py-3',
                      col.align === 'right'  && 'text-right',
                      col.align === 'center' && 'text-center',
                      col.className,
                    )}
                  >
                    {col.render
                      ? col.render((row as Record<string, unknown>)[col.key], row)
                      : ((row as Record<string, unknown>)[col.key] as ReactNode)}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  )
}
