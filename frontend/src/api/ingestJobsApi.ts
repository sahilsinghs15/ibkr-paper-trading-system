import axios from 'axios'
import type { IngestJobsQueryParams, IngestJobsResponse } from '../types/ingestJob'

export async function fetchIngestJobs(
  params: IngestJobsQueryParams,
): Promise<IngestJobsResponse> {
  const { data } = await axios.get<IngestJobsResponse>('/demo/ingest-jobs', {
    params,
    headers: { 'Cache-Control': 'no-store' },
  })
  return data
}
