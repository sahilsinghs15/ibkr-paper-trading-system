export type AllowedService = 'ibgateway' | 'trading-backend' | 'webhook-ingest' | 'demo-streaming'
export type SystemEventKind = 'SERVICE_STARTED' | 'SERVICE_STOPPED' | 'MARKET_CLOSED'

export interface SystemEventDetail {
  service?: string
  unit?: string
  action?: string
  date?: string
  reason?: string
  icon?: string
  title?: string
  message?: string
  friendly_name?: string
  [key: string]: unknown
}

export interface SystemEventItem {
  id: number
  ts: string | null
  kind: SystemEventKind
  service?: string | null
  unit?: string | null
  friendly_name?: string | null
  title?: string
  message?: string
  icon?: string
  detail: SystemEventDetail
}

export interface NotificationItem {
  id: number
  ts: string | null
  kind: SystemEventKind
  service?: string | null
  unit?: string | null
  friendly_name?: string | null
  title: string
  message: string
  icon: string
  is_read: boolean
  detail: SystemEventDetail
}

export interface NotificationFeedResponse {
  items: NotificationItem[]
  unread_count: number
  total: number
}

export interface ToastNotification {
  id: string
  eventId: number
  kind: SystemEventKind
  icon: string
  title: string
  message: string
  timeStr: string
}
