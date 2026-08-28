export interface CriticalBasketLegRow {
  leg: string
  symbol: string
  sec_type: string
  con_id: number | null
  intended_qty: number
  filled_qty: number
  status: string
}

export interface CriticalBasketRow {
  basket_id: number
  account_id: number
  ibkr_account: string
  strategy_id: string
  trade_id: string
  action: string
  state: string
  recovery_status: string | null
  recovery_detail: string | null
  recovered_at: string | null
  intended_leg_count: number
  legs: CriticalBasketLegRow[]
  updated_at: string | null
}

export interface CriticalBasketsResponse {
  ibkr_account: string
  incidents: CriticalBasketRow[]
}
