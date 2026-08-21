import axios from 'axios'
import type {
  AccountConfig,
  AccountDeleteCheck,
  AccountsConfigResponse,
  AllocationConfig,
  CreateAccountPayload,
  CreateAllocationPayload,
  ExecutionSettings,
  PatchAccountPayload,
  SymbolLimit,
} from '../types/config'

const base = '/api/v1/config'

export async function fetchAccountsConfig(): Promise<AccountsConfigResponse> {
  const { data } = await axios.get<AccountsConfigResponse>(`${base}/accounts`)
  return data
}

export async function fetchAccountByIdentifier(ibkrAccount: string): Promise<AccountConfig> {
  const { data } = await axios.get<AccountConfig>(
    `${base}/accounts/by-identifier/${encodeURIComponent(ibkrAccount)}`,
  )
  return data
}

export async function squareOffAccountPositions(
  accountId: number,
): Promise<{
  account_id: number
  ibkr_account: string
  squared_off_count: number
  trade_ids: string[]
  operation_id?: string
  status?: string
}> {
  const { data } = await axios.post<{
    account_id: number
    ibkr_account: string
    squared_off_count: number
    trade_ids: string[]
    operation_id?: string
    status?: string
  }>(`${base}/accounts/${accountId}/square-off`)
  return data
}

export async function createAccount(payload: CreateAccountPayload): Promise<AccountConfig> {
  const { data } = await axios.post<AccountConfig>(`${base}/accounts`, payload)
  return data
}

export async function patchAccount(
  accountId: number,
  body: PatchAccountPayload,
): Promise<AccountConfig> {
  const { data } = await axios.patch<AccountConfig>(`${base}/accounts/${accountId}`, body)
  return data
}

export async function createAllocation(
  accountId: number,
  payload: CreateAllocationPayload,
): Promise<AllocationConfig> {
  const { data } = await axios.post<AllocationConfig>(
    `${base}/accounts/${accountId}/allocations`,
    payload,
  )
  return data
}

export async function checkAccountDeletable(accountId: number): Promise<AccountDeleteCheck> {
  const { data } = await axios.get<AccountDeleteCheck>(`${base}/accounts/${accountId}/deletable`)
  return data
}

export async function deleteAccount(accountId: number): Promise<void> {
  await axios.delete(`${base}/accounts/${accountId}`)
}

export async function patchAllocation(
  allocationId: number,
  body: {
    alloc_pct?: string
    enabled?: boolean
    max_open_positions?: number
  },
): Promise<AllocationConfig> {
  const { data } = await axios.patch<AllocationConfig>(
    `${base}/allocations/${allocationId}`,
    body,
  )
  return data
}

export async function putSymbolLimit(
  accountId: number,
  symbol: string,
  money_limit: string,
): Promise<SymbolLimit> {
  const { data } = await axios.put<SymbolLimit>(
    `${base}/accounts/${accountId}/symbol-limits/${encodeURIComponent(symbol)}`,
    { money_limit },
  )
  return data
}

export async function deleteSymbolLimit(accountId: number, symbol: string): Promise<void> {
  await axios.delete(
    `${base}/accounts/${accountId}/symbol-limits/${encodeURIComponent(symbol)}`,
  )
}

export async function fetchExecutionSettings(): Promise<ExecutionSettings> {
  const { data } = await axios.get<ExecutionSettings>(`${base}/execution`)
  return data
}

export async function patchExecutionSettings(
  body: Partial<
    Pick<
      ExecutionSettings,
      | 'enabled'
      | 'square_off_after_sec'
      | 'max_retries'
      | 'retry_interval_sec'
      | 'retry_window_sec'
    >
  >,
): Promise<ExecutionSettings> {
  const { data } = await axios.patch<ExecutionSettings>(`${base}/execution`, body)
  return data
}
