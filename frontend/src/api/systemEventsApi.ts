import axios from 'axios'
import type { SystemEventItem } from '../types/systemEvent'

export async function fetchSystemEvents(
  sinceId: number = 0,
  limit: number = 20,
): Promise<SystemEventItem[]> {
  try {
    const res = await axios.get<SystemEventItem[]>('/demo/system-events', {
      params: { since_id: sinceId, limit },
      headers: { 'Cache-Control': 'no-store' },
    })
    return Array.isArray(res.data) ? res.data : []
  } catch {
    return []
  }
}
