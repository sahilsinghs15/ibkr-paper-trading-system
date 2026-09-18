/** Machine/system event journal (event_log). Not the operator audit trail. */
export interface EventJournalItem {
  id: number
  ts: string
  process: string
  kind: string
  signal_id?: number | null
  order_id?: number | null
  basket_id?: number | null
  idempotency_key?: string | null
  detail: Record<string, unknown>
}

export interface EventJournalResponse {
  total: number
  limit: number
  offset: number
  events: EventJournalItem[]
}

export interface EventJournalQueryParams {
  category?: string
  process?: string
  kind?: string
  search?: string
  date_from?: string
  date_to?: string
  limit?: number
  offset?: number
}
