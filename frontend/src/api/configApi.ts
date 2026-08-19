import axios from 'axios'
import type {
  AccountConfig,
  AccountsConfigResponse,
  AllocationConfig,
  SymbolLimit,
} from '../types/config'

const base = '/api/v1/config'

export async function fetchAccountsConfig(): Promise<AccountsConfigResponse> {
  const { data } = await axios.get<AccountsConfigResponse>(`${base}/accounts`)
  return data
}

export async function patchAccount(
  accountId: number,
  body: { total_margin?: string; enabled?: boolean },
): Promise<AccountConfig> {
  const { data } = await axios.patch<AccountConfig>(`${base}/accounts/${accountId}`, body)
  return data
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
