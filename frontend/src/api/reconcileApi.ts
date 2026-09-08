import axios from 'axios'
import type {
  AlignBrokerPositionRequest,
  AlignBrokerPositionResponse,
  FlattenBrokerPositionRequest,
  FlattenBrokerPositionResponse,
  ReconcilePositionsResponse,
} from '../types/reconcile'

const base = '/api/v1/reconcile/positions'

export async function fetchReconcilePositions(
  ibkrAccount?: string,
): Promise<ReconcilePositionsResponse> {
  const params = ibkrAccount ? { ibkr_account: ibkrAccount } : undefined
  const { data } = await axios.get<ReconcilePositionsResponse>(base, { params })
  return data
}

export async function flattenBrokerPositionLine(
  payload: FlattenBrokerPositionRequest,
): Promise<FlattenBrokerPositionResponse> {
  const { data } = await axios.post<FlattenBrokerPositionResponse>(
    `${base}/flatten`,
    payload,
  )
  return data
}

export async function alignBrokerPositionLine(
  payload: AlignBrokerPositionRequest,
): Promise<AlignBrokerPositionResponse> {
  const { data } = await axios.post<AlignBrokerPositionResponse>(`${base}/align`, payload)
  return data
}
