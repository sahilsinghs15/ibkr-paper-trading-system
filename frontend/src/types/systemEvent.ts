export type SystemEventKind = 'SERVICE_STARTED' | 'SERVICE_STOPPED' | 'MARKET_CLOSED'

export interface SystemEventDetail {
  service?: string
  unit?: string
  action?: string
  date?: string
  reason?: string
  icon?: string
  message?: string
  [key: string]: unknown
}

export interface SystemEventItem {
  id: number
  ts: string | null
  kind: SystemEventKind
  detail: SystemEventDetail
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
