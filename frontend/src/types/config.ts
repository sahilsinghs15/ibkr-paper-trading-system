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
  pair_max_allocation_pct: string
}

export interface AccountConfig {
  id: number
  name: string
  ibkr_account: string
  total_margin: string
  enabled: boolean
  default_symbol_limit?: string | number | null
  kill_switch_active?: boolean
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

export interface MarginSettings {
  check_enabled: boolean
  gate_basis: string
  min_free_buffer: string
  min_free_pct_of_netliq: string
  comfort_ratio: string
  confirm_borderline: boolean
  enforce_look_ahead: boolean
  reject_on_stale_snapshot: boolean
  default_rate: string
  rate_safety_multiplier: string
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
  pair_max_allocation_pct?: number
  enabled?: boolean
}

export interface AccountDeleteCheck {
  can_delete: boolean
  reason: string | null
  has_history: boolean
}

export interface KillSwitchClearResponse {
  account_id: number
  ibkr_account: string
  operations_cleared: number
  kill_switch_active: boolean
}

export interface KillSwitchStatusResponse {
  account_id: number
  kill_switch_active: boolean
}

export interface ClosePairResponse {
  account_id: number
  ibkr_account: string
  trade_id: string
  leg_a_symbol: string
  leg_b_symbol?: string | null
  status: string
  success: boolean
  message?: string | null
}



