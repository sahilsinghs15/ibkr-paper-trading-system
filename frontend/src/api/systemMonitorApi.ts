import axios from 'axios'
import type { SystemMonitorResponse } from '../types/systemMonitor'

const base = '/api/v1/system-monitor'

export async function fetchSystemMonitor(): Promise<SystemMonitorResponse> {
  const { data } = await axios.get<SystemMonitorResponse>(base)
  return data
}
