import React from 'react'
import type { SortDirection } from '../utils/tableSort'

export interface SortableThProps {
  sortKey: string
  currentSortKey: string | null
  currentSortDir: SortDirection
  onSort: (key: string) => void
  children: React.ReactNode
  style?: React.CSSProperties
  className?: string
  align?: 'left' | 'center' | 'right'
}

export function SortableTh({
  sortKey,
  currentSortKey,
  currentSortDir,
  onSort,
  children,
  style,
  className = '',
  align = 'left',
}: SortableThProps) {
  const isActive = currentSortKey === sortKey && currentSortDir !== null
  const direction = isActive ? currentSortDir : null

  const ariaSort =
    direction === 'asc' ? 'ascending' : direction === 'desc' ? 'descending' : 'none'

  const nextDirText =
    direction === 'asc' ? 'descending' : direction === 'desc' ? 'default order' : 'ascending'

  const handleClick = () => {
    onSort(sortKey)
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      onSort(sortKey)
    }
  }

  const alignClass = align === 'right' ? 'sort-right' : align === 'center' ? 'sort-center' : 'sort-left'

  return (
    <th
      style={style}
      className={`sortable-th ${isActive ? 'active-sort' : ''} ${className}`}
      aria-sort={ariaSort}
      scope="col"
    >
      <button
        type="button"
        className={`sort-header-btn ${alignClass}`}
        onClick={handleClick}
        onKeyDown={handleKeyDown}
        aria-label={`Sort by ${typeof children === 'string' ? children : sortKey} (currently ${direction || 'unsorted'}, click to sort ${nextDirText})`}
        title={`Sort by ${typeof children === 'string' ? children : sortKey} (click to cycle: Ascending → Descending → Default)`}
      >
        <span className="sort-label">{children}</span>
        <span className={`sort-indicator ${direction ? 'active' : 'idle'}`} aria-hidden="true">
          {direction === 'asc' ? '↑' : direction === 'desc' ? '↓' : '↕'}
        </span>
      </button>
    </th>
  )
}
