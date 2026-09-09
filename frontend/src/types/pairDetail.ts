export interface PairExecution {
  id: number
  exec_id: string
  symbol: string
  side: string
  quantity: number
  price: number
  commission?: number | null
  realized_pnl?: number | null
  executed_at?: string | null
}

export interface PairOrder {
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
  broker_order_id?: string | null
  is_compensation: boolean
  compensation_of_internal_order_id?: string | null
  filled_at?: string | null
  created_at?: string | null
  executions: PairExecution[]
}

export interface PairBasket {
  id: number
  action: string
  state: string
  intended_leg_count: number
  recovery_status?: string | null
  created_at?: string | null
  updated_at?: string | null
}

export interface PairEvent {
  id: number
  kind: string
  process?: string | null
  ts?: string | null
  detail: Record<string, unknown>
  order_id?: number | null
  basket_id?: number | null
  signal_id?: number | null
}

export interface PairPositionSummary {
  account_id: number
  ibkr_account: string
  account_name?: string | null
  trade_id: string
  strategy_id: string
  risk_state: string
  leg_a_symbol: string
  leg_a_signed_qty?: string | null
  leg_a_entry_mark?: string | null
  leg_a_instrument_type?: string | null
  leg_b_symbol?: string | null
  leg_b_signed_qty?: string | null
  leg_b_entry_mark?: string | null
  leg_b_instrument_type?: string | null
  live_pnl?: string | null
  realised_pnl?: string | null
  commission?: string | null
  opened_at?: string | null
  closed_at?: string | null
  exit_reason?: string | null
  entry_gross_notional?: string | null
}

export interface PairExits {
  target: string
  stop: string
  time_limit: number
  target_unit: string
  stop_unit: string
  exit_automation_enabled: boolean
  monitor_enabled?: boolean
  shadow_mode?: boolean
}

export interface PairDetail {
  position: PairPositionSummary
  exits: PairExits
  orders: PairOrder[]
  baskets: PairBasket[]
  events: PairEvent[]
}

export interface PatchPositionExitsPayload {
  target?: string
  stop?: string
  target_unit?: string
  stop_unit?: string
  exit_automation_enabled?: boolean
}

export interface PositionExitsResponse {
  account_id: number
  trade_id: string
  risk_state: string
  target: string
  stop: string
  time_limit: number
  target_unit: string
  stop_unit: string
  exit_automation_enabled: boolean
}
