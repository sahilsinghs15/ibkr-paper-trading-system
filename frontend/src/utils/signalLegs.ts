/**
 * Logical-leg grouping for engine signals.
 *
 * A retry does not create a new leg: the coordinator resubmits the *remaining*
 * quantity of the same leg as a fresh `orders` row that keeps the original
 * `leg` label (`L0`, `L1`, ...) and basket. Rendering one row per order
 * therefore shows a two-leg pair as three or more "legs".
 *
 * Grouping here mirrors `reconcile_signal_status` in
 * `backend/demo_streaming/snapshot.py` so the tray agrees with the canonical
 * status the backend computed: key on basket+leg, required qty is the largest
 * quantity seen for the leg (the original, not a retry remainder), and fills
 * accumulate across attempts.
 */
import type { SignalAuditEvent, SignalOrderLeg } from '../store/signalStore'

/** Event kind emitted by `BasketCoordinator._retry_incomplete` for a retry submission. */
export const RETRY_EVENT_KIND = 'AUTO_SQUARE_OFF_RETRY'
/** Event kind emitted when RMS blocks a retry attempt. */
export const RETRY_BLOCKED_EVENT_KIND = 'AUTO_SQUARE_OFF_RETRY_BLOCKED'

export interface LogicalLeg {
  key: string
  symbol: string
  side: string
  /** Original required quantity for the leg. */
  req: number
  /** Cumulative filled quantity across the original order and every retry. */
  fill: number
  /** Number of retry submissions for this leg (attempts beyond the first). */
  retries: number
  /** Orders that make up this leg, oldest first. */
  orders: SignalOrderLeg[]
  isFull: boolean
  isPartial: boolean
}

function legKeyOf(o: SignalOrderLeg): string {
  const leg = o.leg
  if (leg !== undefined && leg !== null && String(leg).trim() !== '') {
    return `basket:${o.basket_id}:leg:${leg}`
  }
  return `basket:${o.basket_id}:sym:${o.symbol}:${o.buy_sell}`
}

/** Collapse retry orders into the logical legs the signal actually has. */
export function groupLogicalLegs(orders: SignalOrderLeg[]): LogicalLeg[] {
  const map = new Map<string, LogicalLeg>()
  for (const o of orders) {
    const key = legKeyOf(o)
    const req = Number(o.quantity) || 0
    const fill = Number(o.fill_qty) || 0
    const existing = map.get(key)
    if (!existing) {
      map.set(key, {
        key,
        symbol: o.symbol,
        side: o.buy_sell,
        req,
        fill,
        retries: 0,
        orders: [o],
        isFull: false,
        isPartial: false,
      })
      continue
    }
    // A retry carries only the remaining qty, so the original is the larger one.
    existing.req = Math.max(existing.req, req)
    existing.fill += fill
    existing.retries += 1
    existing.orders.push(o)
  }

  for (const leg of map.values()) {
    leg.orders.sort((a, b) => (Number(a.id) || 0) - (Number(b.id) || 0))
    leg.isFull = leg.req > 0 && leg.fill + 1e-6 >= leg.req
    leg.isPartial = leg.fill > 0 && !leg.isFull
  }
  return [...map.values()]
}

/**
 * Highest retry attempt number reported by the backend, or null when the
 * signal never retried. Falls back to the per-leg retry count when the event
 * rows are unavailable (events are trimmed for the tray payload).
 */
export function latestRetryAttempt(
  events: SignalAuditEvent[] | null | undefined,
  legs: LogicalLeg[] = [],
): { attempt: number; max: number | null } | null {
  let latest = 0
  let max: number | null = null
  for (const ev of events || []) {
    if (String(ev.kind || '').toUpperCase() !== RETRY_EVENT_KIND) continue
    const detail = ev.detail || {}
    const attempt = Number(detail.retry)
    if (attempt > latest) latest = attempt
    const cap = Number(detail.max_retries)
    if (cap > 0) max = cap
  }
  if (latest === 0) {
    latest = Math.max(0, ...legs.map((l) => l.retries))
  }
  return latest > 0 ? { attempt: latest, max } : null
}

/** "Retry 2/3", or "Retry 2" when the configured cap is not in the payload. */
export function formatRetryLabel(info: { attempt: number; max: number | null }): string {
  return info.max ? `Retry ${info.attempt}/${info.max}` : `Retry ${info.attempt}`
}
