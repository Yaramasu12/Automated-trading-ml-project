import { clsx } from 'clsx'
import type { ReactNode } from 'react'

interface Column<T> {
  key: string
  header: string
  render: (row: T) => ReactNode
  align?: 'left' | 'right' | 'center'
  className?: string
}

interface TableProps<T> {
  columns: Column<T>[]
  data: T[]
  keyFn: (row: T) => string
  emptyText?: string
  className?: string
  compact?: boolean
}

export function Table<T>({ columns, data, keyFn, emptyText = 'No data', className, compact }: TableProps<T>) {
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
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="text-center text-gray-500 py-8 text-xs">
                {emptyText}
              </td>
            </tr>
          ) : (
            data.map((row) => (
              <tr
                key={keyFn(row)}
                className="border-b border-surface-border/50 hover:bg-surface-elevated/50 transition-colors"
              >
                {columns.map((col) => (
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
                    {col.render(row)}
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
