import axios from 'axios'
import type { BrokerExecutionsResponse } from '../types/brokerExecution'

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
