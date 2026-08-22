import { create } from 'zustand'
import type {
  DisplayTimezone,
  PositionLeg,
  StreamState,
} from '../types/position'
import { TZ_NY } from '../types/position'
import { legKey, loadTimezone, tradeKey } from '../utils/format'

interface PnlState {
  active: Record<string, PositionLeg>
  closed: Record<string, PositionLeg>
  streamState: StreamState
  lastTs: string | null
  displayTz: DisplayTimezone
  apply: (row: PositionLeg) => void
  clearActive: () => void
  setStreamState: (state: StreamState) => void
  setDisplayTz: (tz: DisplayTimezone) => void
}

function isClosedEvent(row: PositionLeg): boolean {
  const event = String(row.event || '').toUpperCase()
  return event === 'POSITION_CLOSED' || String(row.status || '').toUpperCase() === 'CLOSED'
}

export const usePnlStore = create<PnlState>((set, get) => ({
  active: {},
  closed: {},
  streamState: 'CONNECTING',
  lastTs: null,
  displayTz: loadTimezone(),

  clearActive: () => set({ active: {} }),

  setStreamState: (streamState) => set({ streamState }),

  setDisplayTz: (displayTz) => set({ displayTz }),

  apply: (row) => {
    if (!row || row.event === 'hello' || row.event === 'stream_error') {
      return
    }
    if (!row.symbol && !row.trade_id) return

    const { active, closed } = get()
    const nextActive = { ...active }
    const nextClosed = { ...closed }
    // Header "Updated" is last receive time so it tracks the clock, not per-trade opened_at.
    const nextLast = new Date().toISOString()

    const key = row.symbol ? legKey(row) : tradeKey(row)

    if (isClosedEvent(row)) {
      if (row.symbol) {
        const prev = nextActive[key] || {}
        const merged: PositionLeg = {
          ...prev,
          ...row,
          opened_at: row.opened_at || prev.opened_at || row.timestamp,
          closed_at: row.closed_at || row.fill_timestamp || row.timestamp || new Date().toISOString(),
        }
        delete nextActive[key]
        nextClosed[key] = merged
      } else {
        for (const [k, v] of Object.entries(nextActive)) {
          if (
            String(v.account_id) === String(row.account_id) &&
            v.trade_id === row.trade_id
          ) {
            delete nextActive[k]
            nextClosed[k] = {
              ...v,
              ...row,
              opened_at: row.opened_at || v.opened_at || v.timestamp,
              closed_at: row.closed_at || row.fill_timestamp || row.timestamp || new Date().toISOString(),
            }
          }
        }
      }
    } else if (row.symbol) {
      delete nextClosed[key]
      const prev = nextActive[key] || {}
      nextActive[key] = {
        ...prev,
        ...row,
        opened_at: row.opened_at || prev.opened_at || row.timestamp,
      }
    }

    set({ active: nextActive, closed: nextClosed, lastTs: nextLast })
  },
}))

export function groupLegs(
  map: Record<string, PositionLeg>,
): Map<string, PositionLeg[]> {
  const out = new Map<string, PositionLeg[]>()
  for (const row of Object.values(map)) {
    const k = tradeKey(row)
    if (!out.has(k)) out.set(k, [])
    out.get(k)!.push(row)
  }
  for (const legs of out.values()) {
    legs.sort((a, b) =>
      String(a.symbol || '').localeCompare(String(b.symbol || '')),
    )
  }
  return out
}

export { TZ_NY }
