/** Leg payload from GET /demo/positions and SSE /demo/stream. */

export type StreamState = 'CONNECTING' | 'LIVE' | 'RECONNECTING'

export interface PositionLeg {
  event?: string
  timestamp?: string | null
  opened_at?: string | null
  closed_at?: string | null
  account_id?: number | string | null
  ibkr_account?: string | null
  account_name?: string | null
  strategy_id?: string | null
  trade_id?: string | null
  symbol?: string | null
  instrument_type?: string | null
  side?: string | null
  quantity?: string | number | null
  filled_quantity?: string | number | null
  entry_price?: string | number | null
  last_price?: string | number | null
  mark_price?: string | number | null
  unrealized_pnl?: string | number | null
  realized_pnl?: string | number | null
  commission?: string | number | null
  status?: string | null
  basket_state?: string | null
  position_state?: string | null
  order_status?: string | null
  broker_order_id?: string | null
  fill_status?: string | null
  fill_timestamp?: string | null
  closing_order_status?: string | null
  closing_broker_order_id?: string | null
  market_data_status?: string | null
  connection_status?: string | null
  close_in_progress?: boolean
  redis_id?: string
}

export interface PositionsSnapshot {
  positions: PositionLeg[]
  market_data_status?: string
}

export const TZ_NY = 'America/New_York'
export const TZ_IN = 'Asia/Kolkata'
export const TZ_STORAGE = 'modelBlue.displayTimezone'

export type DisplayTimezone = typeof TZ_NY | typeof TZ_IN
