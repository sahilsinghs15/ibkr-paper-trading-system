import axios from 'axios'
import type { AuditLogsQueryParams, AuditLogsResponse } from '../types/auditLog'

export async function fetchAuditLogs(
  params: AuditLogsQueryParams,
): Promise<AuditLogsResponse> {
  const { data } = await axios.get<AuditLogsResponse>('/demo/audit-logs', {
    params,
    headers: { 'Cache-Control': 'no-store' },
  })
  return data
}
