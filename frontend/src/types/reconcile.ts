export type ReconcileRunSummary = {
  id: number | null
  finished_at: string | null
  timed_out: boolean
  error: string | null
  broker_line_count: number
  match_count: number
  ghost_count: number
  orphan_count: number
  drift_count: number
  unmapped_account_count: number
}

export type BrokerPositionSnapshotRow = {
  ibkr_account: string
  con_id: number
  account_id: number | null
  symbol: string
  sec_type: string
  currency: string
  exchange: string
  signed_qty: number
  avg_cost: number
  as_of: string
}

export type LedgerPositionRow = {
  account_id: number
  ibkr_account: string | null
  trade_id: string
  strategy_id: string
  leg_a_symbol: string
  leg_a_signed_qty: number
  leg_a_instrument_type: string
  leg_b_symbol: string | null
  leg_b_signed_qty: number | null
  leg_b_instrument_type: string | null
  risk_state: string
}

export type ReconcileDiffKind =
  | 'MATCH'
  | 'LEDGER_GHOST'
  | 'BROKER_ORPHAN'
  | 'QTY_DRIFT'
  | 'UNMAPPED_ACCOUNT'

export type ReconcileDiffRow = {
  kind: ReconcileDiffKind | string
  ibkr_account: string | null
  account_id: number | null
  symbol: string
  sec_type: string
  con_id: number | null
  broker_qty: number | null
  ledger_qty: number | null
  in_flight: boolean
}

export type ReconcilePositionsResponse = {
  run: ReconcileRunSummary | null
  broker_positions: BrokerPositionSnapshotRow[]
  ledger_positions: LedgerPositionRow[]
  diffs: ReconcileDiffRow[]
}

export type FlattenBrokerPositionRequest = {
  ibkr_account: string
  symbol: string
  sec_type: string
  con_id: number
}

export type FlattenBrokerPositionResponse = {
  ibkr_account: string
  account_id: number | null
  symbol: string
  sec_type: string
  con_id: number
  side: string
  quantity: number
  status: string
  success: boolean
  message: string | null
}
