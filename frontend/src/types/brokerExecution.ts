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
}

export interface BrokerExecutionsResponse {
  ibkr_account: string
  as_of: string
  timed_out: boolean
  window: string
  executions: BrokerExecutionLine[]
}
