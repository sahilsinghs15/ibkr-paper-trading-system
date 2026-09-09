/**
 * Shared sorting utilities and contract for table column sorting.
 *
 * Rules:
 * 1. 3-state cycling: null -> 'asc' -> 'desc' -> null
 * 2. Deterministic null/undefined/empty handling: nulls appear LAST in BOTH 'asc' and 'desc'
 * 3. Strict numeric sorting (not alphabetical text comparison)
 * 4. Chronological date/timestamp sorting (via epoch ms)
 * 5. Case-insensitive natural string sorting
 */

import { useCallback, useState } from 'react'

export type SortDirection = 'asc' | 'desc' | null

export function nextSortDirection(current: SortDirection): SortDirection {
  if (current === null) return 'asc'
  if (current === 'asc') return 'desc'
  return null
}

export function useTableSortState(initialKey: string | null = null, initialDir: SortDirection = null) {
  const [sortKey, setSortKey] = useState<string | null>(initialKey)
  const [sortDir, setSortDir] = useState<SortDirection>(initialDir)

  const handleSort = useCallback((key: string) => {
    if (sortKey !== key) {
      setSortKey(key)
      setSortDir('asc')
    } else {
      const nextDir = nextSortDirection(sortDir)
      setSortDir(nextDir)
      if (nextDir === null) {
        setSortKey(null)
      }
    }
  }, [sortKey, sortDir])

  return { sortKey, sortDir, handleSort, setSortKey, setSortDir }
}

export function isNullOrEmpty(val: unknown): boolean {
  if (val === null || val === undefined) return true
  if (typeof val === 'number' && Number.isNaN(val)) return true
  if (typeof val === 'string' && val.trim() === '') return true
  return false
}

export function compareValues(a: unknown, b: unknown, dir: 'asc' | 'desc'): number {
  const aNull = isNullOrEmpty(a)
  const bNull = isNullOrEmpty(b)

  // Terminal Standard: Nulls/empty values appear LAST in BOTH asc and desc
  if (aNull && bNull) return 0
  if (aNull) return 1
  if (bNull) return -1

  const multiplier = dir === 'asc' ? 1 : -1

  // 1. Numeric comparison (including numbers passed as numbers or numeric strings)
  if (typeof a === 'number' && typeof b === 'number') {
    return (a - b) * multiplier
  }

  // 2. Boolean comparison
  if (typeof a === 'boolean' && typeof b === 'boolean') {
    const diff = (a ? 1 : 0) - (b ? 1 : 0)
    return diff * multiplier
  }

  // 3. String comparison (case-insensitive, natural numeric collation)
  const strA = String(a)
  const strB = String(b)
  return strA.localeCompare(strB, undefined, { sensitivity: 'base', numeric: true }) * multiplier
}

export function sortRows<T>(
  rows: T[],
  sortKey: string | null,
  sortDir: SortDirection,
  extractors: Record<string, (row: T) => unknown>,
  defaultSortFn?: (a: T, b: T) => number,
): T[] {
  if (!rows || rows.length === 0) return []

  // Shallow copy to prevent in-place mutation of props or store state
  const copied = [...rows]

  if (!sortKey || !sortDir || !extractors[sortKey]) {
    if (defaultSortFn) {
      copied.sort(defaultSortFn)
    }
    return copied
  }

  const extractor = extractors[sortKey]
  copied.sort((a, b) => {
    const valA = extractor(a)
    const valB = extractor(b)
    const cmp = compareValues(valA, valB, sortDir)
    if (cmp !== 0) return cmp
    // Tie-breaker: fall back to defaultSortFn if available
    return defaultSortFn ? defaultSortFn(a, b) : 0
  })

  return copied
}
