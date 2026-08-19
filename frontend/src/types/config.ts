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

export interface ExecutionSettings {
  enabled: boolean
  square_off_after_sec: number
  max_retries: number
  retry_interval_sec: number
  retry_window_sec: number
  paper_retries_active: boolean
}

export interface CreateAccountPayload {
  name: string
  ibkr_account: string
  total_margin: number
  enabled?: boolean
}

export interface PatchAccountPayload {
  name?: string
  ibkr_account?: string
  total_margin?: number | string
  enabled?: boolean
}

export interface CreateAllocationPayload {
  strategy_id: string
  alloc_pct: number
  max_open_positions?: number
  target?: number
  stop?: number
  time_limit?: number
  enabled?: boolean
}

export interface AccountDeleteCheck {
  can_delete: boolean
  reason: string | null
  has_history: boolean
}

