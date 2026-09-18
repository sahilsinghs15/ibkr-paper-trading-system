import axios from 'axios'
import type { EventJournalQueryParams, EventJournalResponse } from '../types/eventJournal'

export async function fetchEventJournal(
  params: EventJournalQueryParams,
): Promise<EventJournalResponse> {
  const { data } = await axios.get<EventJournalResponse>('/demo/event-journal', {
    params,
    headers: { 'Cache-Control': 'no-store' },
  })
  return data
}
