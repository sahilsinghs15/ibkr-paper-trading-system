import axios from 'axios'
import type {
  NotificationFeedResponse,
  SystemEventItem,
} from '../types/systemEvent'

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

export async function fetchNotificationFeed(
  limit: number = 30,
  offset: number = 0,
): Promise<NotificationFeedResponse> {
  try {
    const res = await axios.get<NotificationFeedResponse>('/demo/notifications', {
      params: { limit, offset },
      headers: { 'Cache-Control': 'no-store' },
    })
    return res.data && Array.isArray(res.data.items)
      ? res.data
      : { items: [], unread_count: 0, total: 0 }
  } catch {
    return { items: [], unread_count: 0, total: 0 }
  }
}

export async function markNotificationAsRead(
  eventId: number,
): Promise<{ ok: boolean; unread_count: number }> {
  try {
    const res = await axios.post<{ ok: boolean; event_id: number; unread_count: number }>(
      `/demo/notifications/${eventId}/read`,
      {},
      { headers: { 'Cache-Control': 'no-store' } },
    )
    return { ok: res.data?.ok ?? false, unread_count: res.data?.unread_count ?? 0 }
  } catch {
    return { ok: false, unread_count: 0 }
  }
}

export async function markAllNotificationsAsRead(): Promise<{
  ok: boolean
  unread_count: number
}> {
  try {
    const res = await axios.post<{ ok: boolean; last_read_id: number; unread_count: number }>(
      '/demo/notifications/mark-all-read',
      {},
      { headers: { 'Cache-Control': 'no-store' } },
    )
    return { ok: res.data?.ok ?? false, unread_count: res.data?.unread_count ?? 0 }
  } catch {
    return { ok: false, unread_count: 0 }
  }
}
