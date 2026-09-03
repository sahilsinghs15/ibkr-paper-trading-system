import axios from 'axios'
import type { AccountMarginListResponse, AccountMarginSnapshot } from '../types/margin'

const base = '/api/v1/margin'

export async function fetchAccountMargins(): Promise<AccountMarginListResponse> {
  const { data } = await axios.get<AccountMarginListResponse>(`${base}/accounts`)
  return data
}

export async function fetchAccountMargin(
  ibkrAccount: string,
): Promise<AccountMarginSnapshot> {
  const { data } = await axios.get<AccountMarginSnapshot>(
    `${base}/accounts/${encodeURIComponent(ibkrAccount)}`,
  )
  return data
}
