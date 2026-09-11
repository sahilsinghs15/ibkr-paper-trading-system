import axios from 'axios'

export interface ManualPositionApiRow {
  id: number
  account_id: number
  trade_id: string
  symbol: string
  con_id: number
  sec_type: string
  currency: string
  signed_qty: number | string
  avg_cost: number | string
  realized_pnl: number | string
  status: string
  source: 'manual'
  opened_at: string
  closed_at?: string | null
  created_at: string
  updated_at: string
}

export interface ManualPositionsListResponse {
  account_id: number
  ibkr_account: string
  source: 'manual'
  positions: ManualPositionApiRow[]
  total: number
}

export async function fetchManualPositions(
  ibkrAccount: string,
): Promise<ManualPositionsListResponse> {
  const { data } = await axios.get<ManualPositionsListResponse>(
    '/api/v1/manual/positions',
    {
      params: { ibkr_account: ibkrAccount },
    },
  )
  return data
}

// ── M1-A Gateway Status Types & API ─────────────────────────────────

export type GatewayEnvironmentMode = 'VERIFIED_PAPER' | 'VERIFIED_LIVE' | 'UNKNOWN'
export type GatewayModeVerificationState =
  | 'ACCOUNT_PREFIX_VERIFIED'
  | 'EXPECTED_PAPER_BY_CONFIGURATION'
  | 'EXPECTED_LIVE_BY_CONFIGURATION'
  | 'UNVERIFIED'

export interface GatewayStatusResponse {
  connected: boolean
  host: string
  port: number
  client_id: number
  ibkr_account?: string | null
  managed_accounts: string[]
  environment: GatewayEnvironmentMode
  verification_state: GatewayModeVerificationState
  server_version?: number | null
  connection_time?: string | null
  message: string
}

export async function fetchGatewayStatus(
  ibkrAccount?: string,
): Promise<GatewayStatusResponse> {
  const { data } = await axios.get<GatewayStatusResponse>(
    '/api/v1/manual/gateway-status',
    {
      params: ibkrAccount ? { ibkr_account: ibkrAccount } : undefined,
    },
  )
  return data
}

// ── M1-A CFD Instrument Discovery Types & API ───────────────────────

export interface CfdCandidateContract {
  con_id: number
  symbol: string
  sec_type: 'CFD'
  exchange: string
  currency: string
  local_symbol?: string | null
  trading_class?: string | null
  min_tick?: number | null
  primary_exchange?: string | null
  long_name?: string | null
}

export interface CfdSearchRequest {
  symbol: string
  exchange?: string
  currency?: string
}

export interface CfdSearchResponse {
  symbol: string
  candidates: CfdCandidateContract[]
  count: number
  message?: string | null
}

export async function searchCfdInstruments(
  ibkrAccount: string,
  payload: CfdSearchRequest,
): Promise<CfdSearchResponse> {
  const { data } = await axios.post<CfdSearchResponse>(
    '/api/v1/manual/instruments/search',
    payload,
    {
      params: { ibkr_account: ibkrAccount },
    },
  )
  return data
}

// ── M1-B Manual Order Preview & Submission Types & API ──────────────

export interface ManualOrderPreviewRequest {
  symbol: string
  con_id: number
  sec_type: 'CFD'
  exchange: string
  currency: string
  side: 'BUY' | 'SELL'
  quantity: number | string
  order_type: 'LIMIT' | 'MARKET' | 'STOP'
  limit_price?: number | string | null
  tif?: string
  outside_rth?: boolean
  min_tick?: number | null
  trade_id?: string | null
}

export interface ManualOrderPreviewResponse {
  valid: boolean
  symbol: string
  con_id: number
  sec_type: string
  exchange: string
  currency: string
  side: string
  quantity: number | string
  order_type: string
  limit_price?: number | string | null
  effective_price?: number | string | null
  notional?: number | string | null
  notional_status: 'EXACT' | 'NO_MARKET_PRICE'
  init_margin_change?: number | string | null
  maint_margin_change?: number | string | null
  margin_status: 'AVAILABLE' | 'UNAVAILABLE' | 'TIMEOUT' | 'SKIPPED'
  gateway_connected: boolean
  environment: GatewayEnvironmentMode
  account_enabled: boolean
  kill_switch_active: boolean
  trading_paused: boolean
  manual_halted: boolean
  warnings: string[]
  errors: string[]
}

export interface ManualOrderSubmitRequest {
  idempotency_key: string
  symbol: string
  con_id: number
  sec_type: 'CFD'
  exchange: string
  currency: string
  side: 'BUY' | 'SELL'
  quantity: number | string
  order_type: 'LIMIT' | 'MARKET' | 'STOP'
  limit_price?: number | string | null
  tif?: string
  outside_rth?: boolean
  min_tick?: number | null
  trade_id?: string | null
}

export interface ManualOrderRead {
  id: number
  account_id: number
  ibkr_account: string
  idempotency_key: string
  internal_order_id: string
  trade_id: string
  broker_order_id?: string | null
  perm_id?: number | null
  con_id: number
  symbol: string
  sec_type: string
  exchange: string
  currency: string
  side: string
  quantity: number | string
  filled_quantity?: number | string
  order_type: string
  limit_price?: number | string | null
  stop_price?: number | string | null
  tif: string
  outside_rth: boolean
  status: string
  source: 'manual'
  user_id?: number | null
  reject_reason?: string | null
  created_at: string
  updated_at: string
  submitted_at?: string | null
  completed_at?: string | null
}

export interface ManualOrdersListResponse {
  account_id: number
  ibkr_account: string
  source: 'manual'
  orders: ManualOrderRead[]
  total: number
}

export interface ManualOrderSubmitResponse {
  order: ManualOrderRead
  idempotent_replay: boolean
  message: string
}

export interface ManualOrderCancelResponse {
  order: ManualOrderRead
  success: boolean
  status: 'CANCELLED' | 'CANCELLED_LOCALLY' | 'CANCEL_REQUESTED' | 'ALREADY_CANCELLED'
  message: string
  broker_order_id?: number | string | null
  perm_id?: number | null
}

export async function previewManualOrder(
  ibkrAccount: string,
  payload: ManualOrderPreviewRequest,
): Promise<ManualOrderPreviewResponse> {
  const { data } = await axios.post<ManualOrderPreviewResponse>(
    '/api/v1/manual/orders/preview',
    payload,
    {
      params: { ibkr_account: ibkrAccount },
    },
  )
  return data
}

export async function submitManualOrder(
  ibkrAccount: string,
  payload: ManualOrderSubmitRequest,
): Promise<ManualOrderSubmitResponse> {
  const { data } = await axios.post<ManualOrderSubmitResponse>(
    '/api/v1/manual/orders',
    payload,
    {
      params: { ibkr_account: ibkrAccount },
    },
  )
  return data
}

export async function fetchManualOrders(
  ibkrAccount: string,
  status?: string,
  limit = 50,
  offset = 0,
): Promise<ManualOrdersListResponse> {
  const { data } = await axios.get<ManualOrdersListResponse>(
    '/api/v1/manual/orders',
    {
      params: {
        ibkr_account: ibkrAccount,
        status: status || undefined,
        limit,
        offset,
      },
    },
  )
  return data
}

export async function cancelManualOrder(
  ibkrAccount: string,
  orderId: number,
): Promise<ManualOrderCancelResponse> {
  const { data } = await axios.post<ManualOrderCancelResponse>(
    `/api/v1/manual/orders/${orderId}/cancel`,
    {},
    {
      params: { ibkr_account: ibkrAccount },
    },
  )
  return data
}

