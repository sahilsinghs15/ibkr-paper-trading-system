import { create } from 'zustand'

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
}

interface SignalState {
  signals: SignalItem[]
  isLoading: boolean
  fetchSignals: () => Promise<void>
  handleSignalEvent: (evt: Record<string, unknown>) => void
}

export const useSignalStore = create<SignalState>((set) => ({
  signals: [],
  isLoading: false,

  fetchSignals: async () => {
    set({ isLoading: true })
    try {
      const res = await fetch('/demo/signals?limit=50')
      if (res.ok) {
        const data = await res.json()
        if (Array.isArray(data.signals)) {
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
    }

    set((state) => {
      const existingIdx = state.signals.findIndex(
        (s) => s.signal_id === newSig.signal_id || (newSig.id > 0 && s.id === newSig.id)
      )
      if (existingIdx >= 0) {
        const updated = [...state.signals]
        updated[existingIdx] = {
          ...updated[existingIdx],
          ...newSig,
        }
        return { signals: updated }
      }
      return { signals: [newSig, ...state.signals].slice(0, 100) }
    })
  },
}))
