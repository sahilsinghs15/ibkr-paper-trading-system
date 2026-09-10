import axios from 'axios'
import type { BrokerExecutionsResponse, OrderBookResponse, TradeBookPaginatedResponse } from '../types/brokerExecution'

export async function fetchBrokerExecutions(
  ibkrAccount: string,
): Promise<BrokerExecutionsResponse> {
  const { data } = await axios.get<BrokerExecutionsResponse>(
    '/api/v1/broker/executions',
    {
      params: { ibkr_account: ibkrAccount },
    },
  )
  return data
}

export async function fetchTradeBook(
  ibkrAccount: string,
  params: { page?: number; page_size?: number; symbol?: string; date_from?: string; date_to?: string } = {},
): Promise<TradeBookPaginatedResponse> {
  const { data } = await axios.get<TradeBookPaginatedResponse>('/api/v1/broker/trade-book', {
    params: { ibkr_account: ibkrAccount, ...params },
  })
  return data
}

export async function fetchOrderBook(
  ibkrAccount: string,
  params: { page?: number; page_size?: number; status?: string; symbol?: string; date_from?: string; date_to?: string } = {},
): Promise<OrderBookResponse> {
  const { data } = await axios.get<OrderBookResponse>('/api/v1/broker/order-book', {
    params: { ibkr_account: ibkrAccount, ...params },
  })
  return data
}
