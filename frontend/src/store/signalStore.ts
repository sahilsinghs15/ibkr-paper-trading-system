import { create } from 'zustand'
import { playSignalNotificationSound } from '../utils/audioNotification'
import { isTrueFlag } from '../utils/format'

export interface SignalExecution {
  id: number
  exec_id: string
  symbol: string
  side: string
  quantity: number
  price: number
  executed_at?: string | null
}

export interface SignalAuditEvent {
  id: number
  kind: string
  ts?: string | null
  detail?: Record<string, unknown> | null
}

export interface SignalOrderLeg {
  id: number
  internal_order_id?: string | null
  basket_id?: number | null
  leg: string
  symbol: string
  buy_sell: string
  quantity: number
  fill_qty: number
  fill_price?: number | null
  status: string
  is_compensation?: boolean
  compensation_of_internal_order_id?: string | null
  filled_at?: string | null
  executions?: SignalExecution[]
}

export interface SignalItem {
  id: number
  signal_id: string
  trade_id?: string | null
  strategy_id: string
  action: string
  pair: string
  side: string
  status: string
  canonical_status?: 'PROCESSING' | 'ACCEPTED' | 'REJECTED' | 'SQUARE-OFF' | string | null
  is_active_processing?: boolean
  reconciled_reason?: string | null
  processing_duration_sec?: number | null
  account_id?: number | null
  ibkr_account?: string | null
  reject_reason?: string | null
  received_at?: string | null
  processed_at?: string | null
  raw_payload?: Record<string, unknown> | null
  orders?: SignalOrderLeg[]
  events?: SignalAuditEvent[]
}

export function getCanonicalStatus(sig: SignalItem): 'PROCESSING' | 'ACCEPTED' | 'REJECTED' | 'SQUARE-OFF' {
  if (sig.canonical_status) {
    const s = String(sig.canonical_status).toUpperCase()
    if (s === 'PROCESSING' || s === 'ACCEPTED' || s === 'REJECTED' || s === 'SQUARE-OFF') {
      return s as 'PROCESSING' | 'ACCEPTED' | 'REJECTED' | 'SQUARE-OFF'
    }
    if (s === 'EXPIRED' || s === 'UNRECONCILED') {
      return 'REJECTED'
    }
  }

  // Client-side fallback reconciliation
  if (sig.reject_reason || String(sig.status).toUpperCase() === 'REJECTED') {
    return 'REJECTED'
  }

  const orders = sig.orders || []
  const compOrders = orders.filter((o) => o.is_compensation)
  const primaryOrders = orders.filter((o) => !o.is_compensation)

  if (compOrders.length > 0 || (sig.events || []).some((e) => e.kind === 'BASKET_UNWINDING' || e.kind === 'SQUARE_OFF')) {
    return 'SQUARE-OFF'
  }

  if (primaryOrders.length > 0) {
    const allFilled = primaryOrders.every((o) => o.status === 'FILLED' || (o.fill_qty >= o.quantity && o.quantity > 0))
    if (allFilled) return 'ACCEPTED'

    const isWorking = primaryOrders.some((o) => ['SUBMITTED', 'PRESUBMITTED', 'PENDING', 'PARTIALLY_FILLED', 'RETRYING'].includes(String(o.status).toUpperCase()))
    if (isWorking) return 'PROCESSING'
  }

  const raw = String(sig.status).toUpperCase()
  if (['PROCESSED', 'FILLED', 'SUCCESS'].includes(raw)) return 'ACCEPTED'

  return 'REJECTED'
}

export interface SignalCounts {
  total: number
  processing: number
  accepted: number
  rejected: number
  square_off: number
}

const EMPTY_COUNTS: SignalCounts = {
  total: 0,
  processing: 0,
  accepted: 0,
  rejected: 0,
  square_off: 0,
}

interface SignalState {
  signals: SignalItem[]
  isLoading: boolean
  page: number
  pageSize: number
  total: number
  totalPages: number
  statusFilter: string
  accountFilter: string
  counts: SignalCounts
  fetchSignals: (opts?: { page?: number; status?: string; account?: string }) => Promise<void>
  setPage: (page: number, account?: string) => Promise<void>
  setStatusFilter: (status: string, account?: string) => Promise<void>
  handleSignalEvent: (evt: Record<string, unknown>) => void
}

function parseMaybeJson(value: unknown): unknown {
  if (typeof value !== 'string') return value
  const s = value.trim()
  if (!s) return value
  const looksStructured =
    (s.startsWith('{') && s.endsWith('}')) ||
    (s.startsWith('[') && s.endsWith(']')) ||
    s === 'true' ||
    s === 'false' ||
    s === 'null' ||
    (s.startsWith('"') && s.endsWith('"'))
  if (!looksStructured && !/^-?\d+(\.\d+)?$/.test(s)) return value
  try {
    return JSON.parse(s)
  } catch {
    return value
  }
}

function asNumber(value: unknown, fallback = 0): number {
  if (typeof value === 'number' && !Number.isNaN(value)) return value
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

function accountMatches(ibkrAccount: string | null | undefined, filter: string): boolean {
  if (!filter) return true
  return String(ibkrAccount || '').trim().toUpperCase() === filter
}

function statusMatchesFilter(
  status: ReturnType<typeof getCanonicalStatus>,
  filter: string,
): boolean {
  if (!filter || filter === 'ALL') return true
  if (filter.toUpperCase() === 'REJECTED') return status === 'REJECTED' || status === 'SQUARE-OFF'
  return status === filter.toUpperCase()
}

function eventToSignal(evt: Record<string, unknown>, existing?: SignalItem): SignalItem {
  const ordersRaw = parseMaybeJson(evt.orders)
  const eventsRaw = parseMaybeJson(evt.events)
  const payloadRaw = parseMaybeJson(evt.raw_payload)
  const orders = Array.isArray(ordersRaw)
    ? (ordersRaw as SignalOrderLeg[])
    : existing?.orders || []
  const events = Array.isArray(eventsRaw)
    ? (eventsRaw as SignalAuditEvent[])
    : existing?.events || []
  const durationRaw = parseMaybeJson(evt.processing_duration_sec)
  const parsedId = asNumber(evt.id, 0)
  return {
    id: parsedId > 0 ? parsedId : existing?.id || Date.now(),
    signal_id: String(evt.signal_id || existing?.signal_id || ''),
    trade_id: evt.trade_id ? String(evt.trade_id) : existing?.trade_id || null,
    strategy_id: String(evt.strategy_id || existing?.strategy_id || 'model_blue'),
    action: String(evt.action || existing?.action || 'OPEN'),
    pair: String(evt.pair || existing?.pair || '—'),
    side: String(evt.side || existing?.side || '—'),
    status: String(evt.status || existing?.status || 'NEW'),
    canonical_status: evt.canonical_status
      ? String(evt.canonical_status)
      : existing?.canonical_status || null,
    is_active_processing:
      evt.is_active_processing !== undefined
        ? isTrueFlag(evt.is_active_processing)
        : existing?.is_active_processing,
    reconciled_reason: evt.reconciled_reason
      ? String(evt.reconciled_reason)
      : existing?.reconciled_reason || null,
    processing_duration_sec:
      typeof durationRaw === 'number'
        ? durationRaw
        : existing?.processing_duration_sec ?? null,
    account_id:
      evt.account_id != null && String(evt.account_id) !== ''
        ? asNumber(evt.account_id, 0) || null
        : existing?.account_id ?? null,
    ibkr_account: evt.ibkr_account
      ? String(evt.ibkr_account)
      : existing?.ibkr_account || null,
    reject_reason: evt.reject_reason
      ? String(evt.reject_reason)
      : existing?.reject_reason || null,
    received_at: evt.received_at
      ? String(evt.received_at)
      : existing?.received_at || new Date().toISOString(),
    processed_at: evt.processed_at
      ? String(evt.processed_at)
      : existing?.processed_at || null,
    raw_payload:
      payloadRaw && typeof payloadRaw === 'object' && !Array.isArray(payloadRaw)
        ? (payloadRaw as Record<string, unknown>)
        : existing?.raw_payload || null,
    orders,
    events,
  }
}

// In-memory set of signal keys already seen during this browser session
const seenSignalKeys = new Set<string>()
let fetchGeneration = 0
let resyncTimer: ReturnType<typeof setTimeout> | null = null

function scheduleCountsResync(): void {
  if (resyncTimer) return
  resyncTimer = setTimeout(() => {
    resyncTimer = null
    void useSignalStore.getState().fetchSignals()
  }, 3000)
}

export const useSignalStore = create<SignalState>((set, get) => ({
  signals: [],
  isLoading: false,
  page: 1,
  pageSize: 100,
  total: 0,
  totalPages: 1,
  statusFilter: 'ALL',
  accountFilter: '',
  counts: { ...EMPTY_COUNTS },

  setPage: async (page: number, account?: string) => {
    const { fetchSignals } = get()
    await fetchSignals({ page, account })
  },

  setStatusFilter: async (status: string, account?: string) => {
    const { fetchSignals } = get()
    await fetchSignals({ page: 1, status, account })
  },

  fetchSignals: async (opts) => {
    const state = get()
    const targetPage = opts?.page ?? state.page
    const targetStatus = opts?.status !== undefined ? opts.status : state.statusFilter
    const targetAccount =
      opts?.account !== undefined ? (opts.account || '').trim().toUpperCase() : state.accountFilter
    const gen = ++fetchGeneration
    const accountChanged = targetAccount !== state.accountFilter

    if (accountChanged) seenSignalKeys.clear()

    set({
      isLoading: true,
      accountFilter: targetAccount,
      ...(accountChanged
        ? { signals: [], counts: { ...EMPTY_COUNTS }, page: 1, total: 0, totalPages: 1 }
        : {}),
    })

    try {
      const accountParam = targetAccount
        ? `&ibkr_account=${encodeURIComponent(targetAccount)}`
        : ''
      const url = `/demo/signals?page=${targetPage}&page_size=${state.pageSize}&status=${targetStatus}${accountParam}`
      const res = await fetch(url)
      if (gen !== fetchGeneration) return
      if (res.ok) {
        const data = await res.json()
        if (Array.isArray(data.signals)) {
          for (const s of data.signals) {
            if (s.signal_id) seenSignalKeys.add(String(s.signal_id))
            if (s.id) seenSignalKeys.add(String(s.id))
          }

          const pageSignals = [...data.signals] as SignalItem[]
          pageSignals.sort((a, b) => {
            const ta = a.received_at ? Date.parse(a.received_at) : 0
            const tb = b.received_at ? Date.parse(b.received_at) : 0
            return tb - ta
          })

          const serverTotal =
            typeof data.filtered_total === 'number'
              ? data.filtered_total
              : typeof data.total === 'number'
                ? data.total
                : pageSignals.length

          set({
            signals: pageSignals,
            page: data.page || targetPage,
            pageSize: data.page_size || state.pageSize,
            total: serverTotal,
            totalPages: data.total_pages || 1,
            statusFilter: targetStatus,
            accountFilter: targetAccount,
            counts: data.counts || EMPTY_COUNTS,
            isLoading: false,
          })
          return
        }
      }
    } catch {
      /* ignore fetch error */
    }
    if (gen === fetchGeneration) set({ isLoading: false })
  },

  handleSignalEvent: (evt) => {
    if (!evt || evt.event !== 'SIGNAL_RECEIVED' || !evt.signal_id) return

    set((state) => {
      const existingIdx = state.signals.findIndex(
        (s) =>
          s.signal_id === String(evt.signal_id) ||
          (asNumber(evt.id, 0) > 0 && s.id === asNumber(evt.id, 0)),
      )
      const existing = existingIdx >= 0 ? state.signals[existingIdx] : undefined
      const newSig = eventToSignal(evt, existing)

      if (!accountMatches(newSig.ibkr_account, state.accountFilter)) {
        return state
      }

      const isKnownBySignalId = seenSignalKeys.has(newSig.signal_id)
      const isKnownById = newSig.id > 0 && seenSignalKeys.has(String(newSig.id))
      const isGenuinelyNew = !isKnownBySignalId && !isKnownById && existingIdx < 0

      if (newSig.signal_id) seenSignalKeys.add(newSig.signal_id)
      if (newSig.id > 0) seenSignalKeys.add(String(newSig.id))

      if (existingIdx >= 0 && existing) {
        const prevStatus = getCanonicalStatus(existing)
        const merged = { ...existing, ...newSig }
        const nextStatus = getCanonicalStatus(merged)
        const updated = [...state.signals]
        updated[existingIdx] = merged
        if (prevStatus !== nextStatus) scheduleCountsResync()
        return { signals: updated }
      }

      if (isGenuinelyNew) {
        playSignalNotificationSound(newSig.ibkr_account)
        scheduleCountsResync()
      }

      const nextStatus = getCanonicalStatus(newSig)
      const canShowOnPage =
        state.page === 1 && isGenuinelyNew && statusMatchesFilter(nextStatus, state.statusFilter)
      if (canShowOnPage) {
        return {
          signals: [newSig, ...state.signals].slice(0, state.pageSize),
        }
      }

      return state
    })
  },
}))
