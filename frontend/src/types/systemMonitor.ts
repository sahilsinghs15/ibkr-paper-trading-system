export interface MetricUsage {
  total_bytes: number
  used_bytes: number
  available_bytes: number
  percent: number
}

export interface CpuMetrics {
  usage_percent: number
  count: number
  load_avg_1m: number
  load_avg_5m: number
  load_avg_15m: number
}

export interface MemoryMetrics {
  ram: MetricUsage
  swap: MetricUsage
}

export interface StorageMetrics {
  mount: string
  filesystem: string
  usage: MetricUsage
  status: 'OK' | 'WARNING' | 'CRITICAL'
}

export interface ServiceStatus {
  name: string
  status: 'RUNNING' | 'DEGRADED' | 'STOPPED' | 'UNKNOWN'
  port: number
  health_detail: string
  latency_ms: number | null
}

export interface ServicesHealth {
  backend: ServiceStatus
  demo_stream: ServiceStatus
  ib_gateway: ServiceStatus
  postgresql: ServiceStatus
  redis: ServiceStatus
}

export interface ProcessInfo {
  pid: number
  name: string
  cpu_percent: number
  memory_percent: number
  status: string
}

export interface AlertItem {
  level: 'INFO' | 'WARNING' | 'CRITICAL'
  component: string
  message: string
}

export interface SystemInfoResponse {
  hostname: string
  operating_system: string
  os_version: string
  kernel_version: string
  architecture: string
  cpu_count: number
  total_memory_bytes: number
  system_uptime_seconds: number
  load_avg: number[]
  timezone: string
  instance_type: string
}

export interface SystemMonitorResponse {
  overall_status: 'HEALTHY' | 'DEGRADED' | 'CRITICAL'
  timestamp: string
  system: SystemInfoResponse
  cpu: CpuMetrics
  memory: MemoryMetrics
  storage: StorageMetrics[]
  services: ServicesHealth
  network: {
    hostname: string
    private_ip: string
    binding_loopback: string
    open_ports: number[]
  }
  alerts: AlertItem[]
  top_processes: ProcessInfo[]
}
