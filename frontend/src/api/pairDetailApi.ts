import axios from 'axios'
import type {
  PairDetail,
  PatchPositionExitsPayload,
  PositionExitsResponse,
} from '../types/pairDetail'

export async function fetchPairDetail(
  accountId: number,
  tradeId: string,
): Promise<PairDetail> {
  const { data } = await axios.get<PairDetail>(
    `/demo/positions/${accountId}/${encodeURIComponent(tradeId)}`,
  )
  return data
}

export async function patchPositionExits(
  accountId: number,
  tradeId: string,
  body: PatchPositionExitsPayload,
): Promise<PositionExitsResponse> {
  const { data } = await axios.patch<PositionExitsResponse>(
    `/api/v1/config/accounts/${accountId}/positions/${encodeURIComponent(tradeId)}/exits`,
    body,
  )
  return data
}
