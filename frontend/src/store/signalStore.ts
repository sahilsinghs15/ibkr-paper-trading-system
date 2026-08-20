import { create } from 'zustand'
import { playSignalNotificationSound } from '../utils/audioNotification'

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
  account_id?: number | null
  ibkr_account?: string | null
  reject_reason?: string | null
  received_at?: string | null
  processed_at?: string | null
  raw_payload?: Record<string, unknown> | null
  orders?: SignalOrderLeg[]
  events?: SignalAuditEvent[]
}

interface SignalState {
  signals: SignalItem[]
  isLoading: boolean
  fetchSignals: () => Promise<void>
  handleSignalEvent: (evt: Record<string, unknown>) => void
}

// In-memory set of signal keys already seen during this browser session
const seenSignalKeys = new Set<string>()

export const useSignalStore = create<SignalState>((set) => ({
  signals: [],
  isLoading: false,

  fetchSignals: async () => {
    set({ isLoading: true })
    try {
      const res = await fetch('/demo/signals?limit=100')
      if (res.ok) {
        const data = await res.json()
        if (Array.isArray(data.signals)) {
          // Mark historical signal IDs as seen WITHOUT playing sound
          for (const s of data.signals) {
            if (s.signal_id) seenSignalKeys.add(String(s.signal_id))
            if (s.id) seenSignalKeys.add(String(s.id))
          }
          set({ signals: data.signals, isLoading: false })
          return
        }
      }
    } catch {
      /* ignore fetch error */
    }
    set({ isLoading: false })
  },

  handleSignalEvent: (evt) => {
    if (!evt || evt.event !== 'SIGNAL_RECEIVED' || !evt.signal_id) return
    const rawOrders = Array.isArray(evt.orders) ? (evt.orders as SignalOrderLeg[]) : []
    const rawEvents = Array.isArray(evt.events) ? (evt.events as SignalAuditEvent[]) : []
    const newSig: SignalItem = {
      id: Number(evt.id) || Date.now(),
      signal_id: String(evt.signal_id),
      trade_id: evt.trade_id ? String(evt.trade_id) : null,
      strategy_id: String(evt.strategy_id || 'model_blue'),
      action: String(evt.action || 'OPEN'),
      pair: String(evt.pair || '—'),
      side: String(evt.side || '—'),
      status: String(evt.status || 'NEW'),
      account_id: evt.account_id ? Number(evt.account_id) : null,
      ibkr_account: evt.ibkr_account ? String(evt.ibkr_account) : null,
      reject_reason: evt.reject_reason ? String(evt.reject_reason) : null,
      received_at: evt.received_at ? String(evt.received_at) : new Date().toISOString(),
      processed_at: evt.processed_at ? String(evt.processed_at) : null,
      raw_payload: (evt.raw_payload as Record<string, unknown>) || null,
      orders: rawOrders,
      events: rawEvents,
    }

    set((state) => {
      const isKnownBySignalId = seenSignalKeys.has(newSig.signal_id)
      const isKnownById = newSig.id > 0 && seenSignalKeys.has(String(newSig.id))
      const existingIdx = state.signals.findIndex(
        (s) => s.signal_id === newSig.signal_id || (newSig.id > 0 && s.id === newSig.id)
      )

      const isGenuinelyNew = !isKnownBySignalId && !isKnownById && existingIdx < 0

      // Mark keys as seen
      if (newSig.signal_id) seenSignalKeys.add(newSig.signal_id)
      if (newSig.id > 0) seenSignalKeys.add(String(newSig.id))

      if (existingIdx >= 0) {
        // Update existing signal in-place without triggering audio sound
        const updated = [...state.signals]
        updated[existingIdx] = {
          ...updated[existingIdx],
          ...newSig,
        }
        return { signals: updated }
      }

      // GENUINELY NEW SIGNAL ARRIVAL -> Trigger audio notification ONCE
      if (isGenuinelyNew) {
        playSignalNotificationSound(newSig.ibkr_account)
      }

      return { signals: [newSig, ...state.signals].slice(0, 100) }
    })
  },
}))
