import axios from 'axios'
import type { CriticalBasketsResponse } from '../types/criticalBaskets'

const base = '/api/v1/baskets/critical'

export async function fetchCriticalBaskets(
  ibkrAccount: string,
): Promise<CriticalBasketsResponse> {
  const { data } = await axios.get<CriticalBasketsResponse>(base, {
    params: { ibkr_account: ibkrAccount },
  })
  return data
}
