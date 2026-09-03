export interface AccountMarginSnapshot {
  ibkr_account: string
  currency: string | null
  as_of: string | null
  is_stale: boolean
  gate_basis: string
  net_liquidation: string | null
  available_funds: string | null
  excess_liquidity: string | null
  full_init_margin_req: string | null
  full_maint_margin_req: string | null
  buying_power: string | null
  gross_position_value: string | null
  total_cash_value: string | null
  cushion: string | null
  look_ahead_init_margin_req: string | null
  look_ahead_maint_margin_req: string | null
  look_ahead_available_funds: string | null
  look_ahead_excess_liquidity: string | null
  look_ahead_next_change: string | null
  free_margin: string | null
  effective_free_margin: string | null
  pending_commitments: string
  floor: string
  utilisation_pct: string | null
}

export interface AccountMarginListResponse {
  accounts: AccountMarginSnapshot[]
}

export interface MarginSettings {
  check_enabled: boolean
  gate_basis: 'available_funds' | 'excess_liquidity' | string
  min_free_buffer: string
  min_free_pct_of_netliq: string
  comfort_ratio: string
  confirm_borderline: boolean
  enforce_look_ahead: boolean
  reject_on_stale_snapshot: boolean
  default_rate: string
  rate_safety_multiplier: string
}
