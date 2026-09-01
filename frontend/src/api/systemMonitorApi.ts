import axios from 'axios'
import type {
  SystemMonitorResponse,
  ServiceKey,
  ActionKey,
  ServiceControlResponse,
} from '../types/systemMonitor'

const base = '/api/v1/system-monitor'

export async function fetchSystemMonitor(): Promise<SystemMonitorResponse> {
  const { data } = await axios.get<SystemMonitorResponse>(base)
  return data
}

export async function controlService(
  service: ServiceKey,
  action: ActionKey
): Promise<ServiceControlResponse> {
  const { data } = await axios.post<ServiceControlResponse>(
    `/api/v1/service-control/${service}/${action}`
  )
  return data
}

