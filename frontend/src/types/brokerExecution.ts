export interface BrokerExecutionLine {
  exec_id: string
  executed_at: string
  ibkr_account: string
  symbol: string
  sec_type: string
  currency: string
  exchange: string
  con_id: number
  side: string
  quantity: number
  price: number
  cum_qty: number
  avg_price: number
  broker_order_id: number | null
  perm_id: number | null
  client_id: number | null
  commission: number | null
  commission_currency: string | null
  realized_pnl: number | null
  order_status: string | null
}

export interface BrokerExecutionsResponse {
  ibkr_account: string
  as_of: string
  timed_out: boolean
  window: string
  executions: BrokerExecutionLine[]
}

export interface TradeBookPaginatedResponse {
  ibkr_account: string
  as_of: string
  last_synced_at: string | null
  timed_out: boolean
  total: number
  page: number
  page_size: number
  executions: BrokerExecutionLine[]
}

export interface OrderBookRow {
  internal_order_id: string
  broker_order_id: string | null
  perm_id: number | null
  ibkr_account: string
  symbol: string
  sec_type: string | null
  exchange: string | null
  currency: string | null
  ibkr_contract: string | null
  side: string
  quantity: number
  filled: number
  remaining: number
  order_type: string
  limit_price: number | null
  status: string
  avg_fill_price: number | null
  last_fill_price: number | null
  trade_id: string | null
  signal_id: string | null
  basket_id: number | null
  is_compensation: boolean
  compensation_of: string | null
  rejection_reason: string | null
  cancel_reason: string | null
  created_at: string | null
  updated_at: string | null
  filled_at: string | null
}

export interface OrderBookResponse {
  ibkr_account: string
  as_of: string
  total: number
  page: number
  page_size: number
  orders: OrderBookRow[]
}
