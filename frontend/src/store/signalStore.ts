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

interface SignalPageResult {
  signals: SignalItem[]
  page: number
  pageSize: number
  total: number
  totalPages: number
  counts: SignalCounts
}

interface SignalState {
  // Shared source of truth: account-scoped, never status-filtered. Used by Signal Monitor.
  signals: SignalItem[]
  isLoading: boolean
  pageSize: number
  accountFilter: string
  counts: SignalCounts
  // Signal Tray has independent pagination + status filtering over the same API/live events.
  traySignals: SignalItem[]
  trayLoading: boolean
  trayPage: number
  trayPageSize: number
  trayTotal: number
  trayTotalPages: number
  trayStatusFilter: string
  fetchSignals: (opts?: { page?: number; status?: string; account?: string }) => Promise<void>
  fetchTraySignals: (opts?: { page?: number; status?: string; account?: string }) => Promise<void>
  setTrayPage: (page: number, account?: string) => Promise<void>
  setTrayStatusFilter: (status: string, account?: string) => Promise<void>
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

export function accountMatches(ibkrAccount: string | null | undefined, filter: string): boolean {
  if (!filter) return true
  const account = String(ibkrAccount || '').trim().toUpperCase()
  if (!account) return false
  return account === filter
}

function statusMatchesFilter(
  status: ReturnType<typeof getCanonicalStatus>,
  filter: string,
): boolean {
  if (!filter || filter === 'ALL') return true
  if (filter.toUpperCase() === 'REJECTED') return status === 'REJECTED' || status === 'SQUARE-OFF'
  return status === filter.toUpperCase()
}

function findSignalIndex(list: SignalItem[], evtOrSig: { signal_id?: unknown; id?: unknown }): number {
  const sid = String(evtOrSig.signal_id || '')
  const id = asNumber(evtOrSig.id, 0)
  return list.findIndex((s) => (sid && s.signal_id === sid) || (id > 0 && s.id === id))
}

const seenSignalKeys = new Set<string>()
let monitorFetchGen = 0
let trayFetchGen = 0
let resyncTimer: ReturnType<typeof setTimeout> | null = null

function rememberSignalKeys(signals: SignalItem[]): void {
  for (const s of signals) {
    if (s.signal_id) seenSignalKeys.add(String(s.signal_id))
    if (s.id) seenSignalKeys.add(String(s.id))
  }
}

function sortSignalsByReceived(signals: SignalItem[]): SignalItem[] {
  return [...signals].sort((a, b) => {
    const ta = a.received_at ? Date.parse(a.received_at) : 0
    const tb = b.received_at ? Date.parse(b.received_at) : 0
    return tb - ta
  })
}

async function fetchSignalPage(args: {
  page: number
  pageSize: number
  status: string
  account: string
}): Promise<SignalPageResult | null> {
  const accountParam = args.account
    ? `&ibkr_account=${encodeURIComponent(args.account)}`
    : ''
  const url = `/demo/signals?page=${args.page}&page_size=${args.pageSize}&status=${encodeURIComponent(args.status)}${accountParam}`
  const res = await fetch(url)
  if (!res.ok) return null
  const data = await res.json()
  if (!Array.isArray(data.signals)) return null
  rememberSignalKeys(data.signals)
  const serverTotal =
    typeof data.filtered_total === 'number'
      ? data.filtered_total
      : typeof data.total === 'number'
        ? data.total
        : data.signals.length
  return {
    signals: sortSignalsByReceived(data.signals as SignalItem[]),
    page: data.page || args.page,
    pageSize: data.page_size || args.pageSize,
    total: serverTotal,
    totalPages: data.total_pages || 1,
    counts: data.counts || EMPTY_COUNTS,
  }
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

function scheduleCountsResync(): void {
  if (resyncTimer) return
  resyncTimer = setTimeout(() => {
    resyncTimer = null
    const store = useSignalStore.getState()
    void store.fetchSignals()
    void store.fetchTraySignals()
  }, 3000)
}

const ACCOUNT_RESET = {
  signals: [] as SignalItem[],
  counts: { ...EMPTY_COUNTS },
  traySignals: [] as SignalItem[],
  trayPage: 1,
  trayTotal: 0,
  trayTotalPages: 1,
}

export const useSignalStore = create<SignalState>((set, get) => ({
  signals: [],
  isLoading: false,
  pageSize: 100,
  accountFilter: '',
  counts: { ...EMPTY_COUNTS },
  traySignals: [],
  trayLoading: false,
  trayPage: 1,
  trayPageSize: 100,
  trayTotal: 0,
  trayTotalPages: 1,
  trayStatusFilter: 'ALL',

  setTrayPage: async (page: number, account?: string) => {
    await get().fetchTraySignals({ page, account })
  },

  setTrayStatusFilter: async (status: string, account?: string) => {
    await get().fetchTraySignals({ page: 1, status, account })
  },

  fetchSignals: async (opts) => {
    const state = get()
    const targetAccount =
      opts?.account !== undefined ? (opts.account || '').trim().toUpperCase() : state.accountFilter
    const accountChanged = targetAccount !== state.accountFilter
    if (accountChanged) {
      seenSignalKeys.clear()
    }
    const gen = ++monitorFetchGen

    set({
      isLoading: true,
      accountFilter: targetAccount,
      ...(accountChanged ? ACCOUNT_RESET : {}),
    })

    try {
      const page = await fetchSignalPage({
        page: 1,
        pageSize: state.pageSize,
        status: 'ALL',
        account: targetAccount,
      })
      if (gen !== monitorFetchGen) return
      if (get().accountFilter !== targetAccount) return
      if (page) {
        set({
          signals: page.signals,
          pageSize: page.pageSize,
          accountFilter: targetAccount,
          counts: page.counts,
          isLoading: false,
        })
        return
      }
    } catch {
      /* ignore fetch error */
    }
    if (gen === monitorFetchGen) set({ isLoading: false })
  },

  fetchTraySignals: async (opts) => {
    const state = get()
    const targetPage = opts?.page ?? state.trayPage
    const targetStatus = opts?.status !== undefined ? opts.status : state.trayStatusFilter
    const targetAccount =
      opts?.account !== undefined ? (opts.account || '').trim().toUpperCase() : state.accountFilter
    const accountChanged = targetAccount !== state.accountFilter
    if (accountChanged) {
      seenSignalKeys.clear()
    }
    const gen = ++trayFetchGen

    set({
      trayLoading: true,
      trayStatusFilter: targetStatus,
      accountFilter: targetAccount,
      ...(accountChanged
        ? { traySignals: [], trayPage: 1, trayTotal: 0, trayTotalPages: 1 }
        : {}),
    })

    try {
      const page = await fetchSignalPage({
        page: targetPage,
        pageSize: state.trayPageSize,
        status: targetStatus,
        account: targetAccount,
      })
      if (gen !== trayFetchGen) return
      if (get().accountFilter !== targetAccount) return
      if (page) {
        set({
          traySignals: page.signals,
          trayPage: page.page,
          trayPageSize: page.pageSize,
          trayTotal: page.total,
          trayTotalPages: page.totalPages,
          trayStatusFilter: targetStatus,
          accountFilter: targetAccount,
          counts: page.counts,
          trayLoading: false,
        })
        return
      }
    } catch {
      /* ignore fetch error */
    }
    if (gen === trayFetchGen) set({ trayLoading: false })
  },

  handleSignalEvent: (evt) => {
    if (!evt || evt.event !== 'SIGNAL_RECEIVED' || !evt.signal_id) return

    set((state) => {
      const monitorIdx = findSignalIndex(state.signals, evt)
      const trayIdx = findSignalIndex(state.traySignals, evt)
      const existing =
        (monitorIdx >= 0 ? state.signals[monitorIdx] : undefined) ||
        (trayIdx >= 0 ? state.traySignals[trayIdx] : undefined)
      const incoming = eventToSignal(evt, existing)

      if (!accountMatches(incoming.ibkr_account, state.accountFilter)) {
        return state
      }

      const isKnownBySignalId = seenSignalKeys.has(incoming.signal_id)
      const isKnownById = incoming.id > 0 && seenSignalKeys.has(String(incoming.id))
      const isGenuinelyNew = !isKnownBySignalId && !isKnownById && monitorIdx < 0 && trayIdx < 0

      if (incoming.signal_id) seenSignalKeys.add(incoming.signal_id)
      if (incoming.id > 0) seenSignalKeys.add(String(incoming.id))

      const merged = existing ? { ...existing, ...incoming } : incoming
      const prevStatus = existing ? getCanonicalStatus(existing) : null
      const nextStatus = getCanonicalStatus(merged)

      if (isGenuinelyNew) {
        playSignalNotificationSound(merged.ibkr_account)
        scheduleCountsResync()
      } else if (prevStatus && prevStatus !== nextStatus) {
        scheduleCountsResync()
      }

      let signals = state.signals
      if (monitorIdx >= 0) {
        signals = [...state.signals]
        signals[monitorIdx] = merged
      } else if (isGenuinelyNew) {
        signals = [merged, ...state.signals].slice(0, state.pageSize)
      }

      const matchesTray = statusMatchesFilter(nextStatus, state.trayStatusFilter)
      const previouslyMatchedTray = prevStatus
        ? statusMatchesFilter(prevStatus, state.trayStatusFilter)
        : false
      let traySignals = state.traySignals
      if (trayIdx >= 0) {
        if (matchesTray) {
          traySignals = [...state.traySignals]
          traySignals[trayIdx] = merged
        } else {
          traySignals = state.traySignals.filter((_, i) => i !== trayIdx)
        }
      } else if (
        matchesTray &&
        state.trayPage === 1 &&
        (isGenuinelyNew || (monitorIdx >= 0 && !previouslyMatchedTray))
      ) {
        traySignals = [merged, ...state.traySignals].slice(0, state.trayPageSize)
      }

      if (signals === state.signals && traySignals === state.traySignals) {
        return state
      }
      return { signals, traySignals }
    })
  },
}))
