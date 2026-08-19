export interface SymbolLimit {
  symbol: string
  money_limit: string
}

export interface AllocationConfig {
  id: number
  strategy_id: string
  alloc_pct: string
  enabled: boolean
  max_open_positions: number
  target: string
  stop: string
  time_limit: number
}

export interface AccountConfig {
  id: number
  name: string
  ibkr_account: string
  total_margin: string
  enabled: boolean
  allocations: AllocationConfig[]
  symbol_limits: SymbolLimit[]
}

export interface AccountsConfigResponse {
  accounts: AccountConfig[]
}
