import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  fetchManualOrders,
  fetchManualPositions,
  type ManualOrderRead,
  type ManualPositionApiRow,
} from '../../api/manualTradingApi'
import { usePnlStore } from '../../store/pnlStore'

export const ORDERS_PAGE_SIZE = 10
export const ACTIVE_ORDER_STATUSES = ['PENDING_SUBMIT', 'SUBMITTED', 'PARTIALLY_FILLED']

export function apiErrorMessage(err: unknown, fallback: string): string {
  const ae = err as { response?: { data?: { detail?: string } } }
  return ae?.response?.data?.detail || (err instanceof Error ? err.message : fallback)
}

/** Authoritative open manual positions (REST, manual_positions only — engine excluded). */
export function useManualPositions(account: string) {
  const [rows, setRows] = useState<ManualPositionApiRow[]>([])
  const [loading, setLoading] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const requestId = useRef(0)

  const reload = useCallback(async () => {
    if (!account) return
    const id = ++requestId.current
    setLoading(true)
    try {
      const res = await fetchManualPositions(account)
      if (id !== requestId.current) return
      setRows(res.positions)
      setError(null)
      setLoaded(true)
    } catch (err: unknown) {
      if (id !== requestId.current) return
      setError(apiErrorMessage(err, 'Failed to load manual positions'))
    } finally {
      if (id === requestId.current) setLoading(false)
    }
  }, [account])

  useEffect(() => {
    setRows([])
    setLoaded(false)
    void reload()
  }, [reload])

  return { rows, loading, loaded, error, reload }
}

/** Paginated manual orders with state-driven polling while any order is working. */
export function useManualOrders(account: string) {
  const [orders, setOrders] = useState<ManualOrderRead[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const requestId = useRef(0)

  const reload = useCallback(
    async (targetPage?: number) => {
      if (!account) return
      const p = targetPage ?? page
      const id = ++requestId.current
      setLoading(true)
      try {
        const res = await fetchManualOrders(account, undefined, ORDERS_PAGE_SIZE, (p - 1) * ORDERS_PAGE_SIZE)
        if (id !== requestId.current) return
        setOrders(res.orders)
        setTotal(res.total)
        setError(null)
        setLoaded(true)
        // Clamp page if total shrank.
        const totalPages = Math.max(1, Math.ceil(res.total / ORDERS_PAGE_SIZE))
        if (p > totalPages) setPage(totalPages)
      } catch (err: unknown) {
        if (id !== requestId.current) return
        setError(apiErrorMessage(err, 'Failed to fetch orders'))
      } finally {
        if (id === requestId.current) setLoading(false)
      }
    },
    [account, page],
  )

  useEffect(() => {
    setPage(1)
    setLoaded(false)
  }, [account])

  useEffect(() => {
    void reload(page)
  }, [reload, page])

  const hasActive = orders.some((o) => ACTIVE_ORDER_STATUSES.includes(o.status))
  useEffect(() => {
    if (!hasActive) return
    const t = setInterval(() => void reload(page), 5000)
    return () => clearInterval(t)
  }, [hasActive, reload, page])

  // Changes whenever an order's status or fill progresses.
  const fillSignature = useMemo(
    () => orders.map((o) => `${o.id}:${o.status}:${String(o.filled_quantity ?? 0)}`).join('|'),
    [orders],
  )

  return { orders, total, page, setPage, loading, loaded, error, reload, fillSignature }
}

export interface LiveManualMark {
  mark: number | null
  unrealized: number | null
  marketStatus: string | null
}

/** Live marks for this account's manual legs from the shared SSE P&L stream. */
export function useLiveManualMarks(account: string) {
  const activeLegs = usePnlStore((s) => s.active)
  return useMemo(() => {
    const byTrade = new Map<string, LiveManualMark>()
    const want = account.trim().toUpperCase()
    for (const leg of Object.values(activeLegs)) {
      if (String(leg.source || '').toLowerCase() !== 'manual') continue
      if (want && String(leg.ibkr_account || '').trim().toUpperCase() !== want) continue
      const tid = String(leg.trade_id || '')
      if (!tid) continue
      const markRaw = leg.mark_price ?? leg.last_price
      const mark = markRaw != null && markRaw !== '' ? Number(markRaw) : null
      const unreal = leg.unrealized_pnl != null && leg.unrealized_pnl !== '' ? Number(leg.unrealized_pnl) : null
      byTrade.set(tid, {
        mark: mark != null && Number.isFinite(mark) ? mark : null,
        unrealized: unreal != null && Number.isFinite(unreal) ? unreal : null,
        marketStatus: leg.market_data_status ?? null,
      })
    }
    const tradeSignature = Array.from(byTrade.keys()).sort().join('|')
    return { byTrade, tradeSignature }
  }, [activeLegs, account])
}
