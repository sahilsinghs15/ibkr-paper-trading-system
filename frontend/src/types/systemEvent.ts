export type AllowedService = 'ibgateway' | 'trading-backend' | 'webhook-ingest' | 'demo-streaming' | 'server-machine' | 'ec2-instance'
export type SystemEventKind =
  | 'SERVICE_STARTED'
  | 'SERVICE_STOPPED'
  | 'MARKET_CLOSED'
  | 'STARTUP_AGGREGATION'
  | 'BROKER_LOST'
  | 'BROKER_RECONNECTED'
  | 'ROGUE_TRADE_DETECTED'
  | 'ROGUE_TRADE_RESOLVED'
  | 'LOSS_THRESHOLD_BREACHED'
  | 'LOSS_THRESHOLD_RESET'
  | string
export type ToastKind = SystemEventKind | 'SUCCESS' | 'ERROR' | 'INFO' | 'WARNING' | 'CRITICAL'

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
  kind: ToastKind
  icon: string
  title: string
  message: string
  timeStr: string
}
