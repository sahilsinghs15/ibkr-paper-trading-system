import { useState, useEffect, useCallback } from 'react'
import { fetchSystemMonitor, controlService } from '../api/systemMonitorApi'
import type {
  SystemMonitorResponse,
  ServiceStatus,
  ServiceKey,
  ActionKey,
} from '../types/systemMonitor'

interface ServiceControlConfig {
  key: ServiceKey
  servicesKey: keyof SystemMonitorResponse['services']
  label: string
  unit: string
  sessionControlled: boolean
  startImpact: string
  stopImpact: string
  restartImpact: string
}

const CONTROL_CONFIG: ServiceControlConfig[] = [
  {
    key: 'ibgateway',
    servicesKey: 'ib_gateway',
    label: 'IB Gateway',
    unit: 'ibgateway.service',
    sessionControlled: true,
    startImpact: 'This will start ibgateway.service and initiate IB Gateway TWS socket connectivity.',
    stopImpact: 'This will stop ibgateway.service and disconnect the IB Gateway session.',
    restartImpact: 'This will restart ibgateway.service and reconnect the IB Gateway session.',
  },
  {
    key: 'backend',
    servicesKey: 'backend',
    label: 'Trading Backend',
    unit: 'trading-backend.service',
    sessionControlled: false,
    startImpact: 'This will start trading-backend.service and resume API & worker pool processing.',
    stopImpact:
      'This will stop trading-backend.service. The trading execution API will become unavailable until started again.',
    restartImpact:
      'This will restart trading-backend.service and re-initialize the execution worker pool and recovery services.',
  },
  {
    key: 'webhook',
    servicesKey: 'webhook',
    label: 'Webhook Ingest',
    unit: 'webhook-ingest.service',
    sessionControlled: true,
    startImpact: 'This will start webhook-ingest.service and accept TradingView alerts on port 8000.',
    stopImpact:
      'This will stop webhook-ingest.service. Incoming TradingView alerts will not be enqueued while stopped.',
    restartImpact: 'This will restart webhook-ingest.service on port 8000.',
  },
  {
    key: 'watchdog',
    servicesKey: 'watchdog',
    label: 'Watchdog',
    unit: 'watchdog.service',
    sessionControlled: false,
    startImpact: 'This will start watchdog.service to resume background monitoring.',
    stopImpact:
      'This will stop watchdog.service. Automated monitoring and Telegram notifications will be paused until restarted.',
    restartImpact: 'This will restart watchdog.service and re-trigger background monitoring probes.',
  },
]

export function SystemMonitorPage() {
  const [data, setData] = useState<SystemMonitorResponse | null>(null)
  const [loading, setLoading] = useState<boolean>(true)
  const [error, setError] = useState<string | null>(null)
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null)

  // Service control state
  const [controlFeedback, setControlFeedback] = useState<{
    type: 'success' | 'error'
    message: string
  } | null>(null)
  const [pendingAction, setPendingAction] = useState<
    Record<string, 'STARTING' | 'STOPPING' | 'RESTARTING' | null>
  >({})
  const [submitting, setSubmitting] = useState<boolean>(false)
  const [modalState, setModalState] = useState<{
    service: ServiceKey
    action: ActionKey
    label: string
    unit: string
    description: string
  } | null>(null)

  const loadData = useCallback(async () => {
    try {
      setError(null)
      const res = await fetchSystemMonitor()
      setData(res)
      setLastRefreshed(new Date())
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to connect to System Monitor API')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadData()
    // Auto refresh every 30 seconds
    const timer = setInterval(() => {
      loadData()
    }, 30000)
    return () => clearInterval(timer)
  }, [loadData])

  const formatBytes = (bytes: number) => {
    if (bytes === 0) return '0 B'
    const k = 1024
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
    const i = Math.floor(Math.log(bytes) / Math.log(k))
    return (bytes / Math.pow(k, i)).toFixed(1) + ' ' + sizes[i]
  }

  const formatUptime = (seconds: number) => {
    const days = Math.floor(seconds / 86400)
    const hours = Math.floor((seconds % 86400) / 3600)
    const mins = Math.floor((seconds % 3600) / 60)
    if (days > 0) return `${days}d ${hours}h ${mins}m`
    if (hours > 0) return `${hours}h ${mins}m`
    return `${mins}m`
  }

  const renderServiceBadge = (svc: ServiceStatus) => {
    let badgeClass = 'status-badge'
    let textLabel: string = svc.status
    if (svc.status === 'RUNNING') {
      badgeClass += ' on'
      textLabel = '● RUNNING'
    } else if (svc.status === 'DEGRADED') {
      badgeClass += ' idle'
      textLabel = '● DEGRADED'
    } else if (svc.status === 'MARKET_CLOSED') {
      badgeClass += ' idle'
      textLabel = '● MARKET CLOSED'
    } else {
      badgeClass += ' off'
      textLabel = '● STOPPED'
    }

    return (
      <span className={badgeClass} style={{ padding: '2px 8px', fontSize: '11px' }}>
        {textLabel}
      </span>
    )
  }

  const handleOpenModal = (cfg: ServiceControlConfig, action: ActionKey) => {
    let description = cfg.startImpact
    if (action === 'stop') description = cfg.stopImpact
    else if (action === 'restart') description = cfg.restartImpact

    setModalState({
      service: cfg.key,
      action,
      label: cfg.label,
      unit: cfg.unit,
      description,
    })
  }

  const handleConfirmAction = async () => {
    if (!modalState) return
    const { service, action, label } = modalState
    setSubmitting(true)
    setControlFeedback(null)

    // Mark as pending locally
    const pendingLabel =
      action === 'start' ? 'STARTING' : action === 'stop' ? 'STOPPING' : 'RESTARTING'
    setPendingAction((prev) => ({ ...prev, [service]: pendingLabel }))

    try {
      const res = await controlService(service, action)
      setControlFeedback({
        type: 'success',
        message: `✓ ${label} ${action} requested successfully (${res.unit})`,
      })
      setModalState(null)

      // Refresh real state from server after short delay
      setTimeout(() => {
        setPendingAction((prev) => ({ ...prev, [service]: null }))
        loadData()
      }, 2000)
    } catch (err: unknown) {
      let msg = `Failed to ${action} ${label}`
      if (typeof err === 'object' && err !== null && 'response' in err) {
        const res = (err as { response?: { data?: { detail?: string } } }).response
        if (res?.data?.detail) msg += `: ${res.data.detail}`
      } else if (err instanceof Error) {
        msg += `: ${err.message}`
      }
      setControlFeedback({ type: 'error', message: `✕ ${msg}` })
      setPendingAction((prev) => ({ ...prev, [service]: null }))
      setModalState(null)
    } finally {
      setSubmitting(false)
    }
  }

  if (loading && !data) {
    return (
      <div style={{ padding: '24px', maxWidth: '1200px', margin: '0 auto' }}>
        <div style={{ color: 'var(--muted)' }}>Loading System Monitor metrics...</div>
      </div>
    )
  }

  if (error && !data) {
    return (
      <div style={{ padding: '24px', maxWidth: '1200px', margin: '0 auto' }}>
        <div className="status-badge off" style={{ padding: '12px', fontSize: '13px', marginBottom: '16px' }}>
          SYSTEM MONITOR UNAVAILABLE: {error}
        </div>
        <button
          onClick={loadData}
          style={{
            padding: '6px 14px',
            background: 'var(--panel-2)',
            color: 'var(--ink)',
            border: '1px solid var(--line)',
            borderRadius: '4px',
            cursor: 'pointer',
          }}
        >
          Retry Connection
        </button>
      </div>
    )
  }

  const overall = data?.overall_status || 'UNKNOWN'
  const overallBadgeClass =
    overall === 'HEALTHY'
      ? 'status-badge on'
      : overall === 'DEGRADED' || overall === 'MARKET_CLOSED'
      ? 'status-badge idle'
      : 'status-badge off'

  return (
    <div style={{ padding: '20px 24px', maxWidth: '1280px', margin: '0 auto', display: 'flex', flexDirection: 'column', gap: '20px' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '12px', borderBottom: '1px solid var(--line)', paddingBottom: '16px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
          <h1 style={{ margin: 0, fontSize: '20px', fontWeight: 700, letterSpacing: '-0.02em' }}>System Monitor</h1>
          <span className={overallBadgeClass} style={{ fontSize: '12px', padding: '4px 10px' }}>
            ● OVERALL STATUS: {overall}
          </span>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '16px', fontSize: '12px', color: 'var(--muted)' }}>
          <span>Last Refreshed: {lastRefreshed ? lastRefreshed.toLocaleTimeString() : 'Never'}</span>
          <button
            onClick={loadData}
            style={{
              padding: '6px 14px',
              background: 'var(--panel-2)',
              color: 'var(--ink)',
              border: '1px solid var(--line)',
              borderRadius: '4px',
              cursor: 'pointer',
              fontWeight: 600,
              fontSize: '12px',
            }}
          >
            Refresh
          </button>
        </div>
      </div>

      {/* Alerts Section */}
      {data?.alerts && data.alerts.length > 0 ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          {data.alerts.map((al, idx) => (
            <div
              key={idx}
              className={`status-badge ${al.level === 'CRITICAL' ? 'off' : 'idle'}`}
              style={{ padding: '10px 14px', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}
            >
              <span>
                <strong>[{al.component}]</strong> {al.message}
              </span>
              <span style={{ fontSize: '10px', textTransform: 'uppercase', letterSpacing: '0.08em' }}>{al.level}</span>
            </div>
          ))}
        </div>
      ) : (
        <div style={{ padding: '10px 14px', background: 'var(--green-bg)', border: '1px solid rgba(62,207,142,0.2)', borderRadius: '4px', color: 'var(--green)', fontSize: '12px', fontWeight: 600 }}>
          ✓ No active system alerts — all components operating normally
        </div>
      )}

      {/* Top Cards: Hardware Resources */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: '16px' }}>
        {/* CPU */}
        <div style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: '6px', padding: '16px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', color: 'var(--muted)', fontSize: '12px', fontWeight: 600 }}>
            <span>CPU UTILIZATION</span>
            <span>{data?.cpu.count} vCPU</span>
          </div>
          <div style={{ fontSize: '24px', fontWeight: 700, fontFamily: 'var(--mono)' }}>
            {data?.cpu.usage_percent}%
          </div>
          <div style={{ background: 'var(--panel-2)', height: '6px', borderRadius: '3px', overflow: 'hidden' }}>
            <div
              style={{
                width: `${data?.cpu.usage_percent || 0}%`,
                height: '100%',
                background: (data?.cpu.usage_percent || 0) >= 90 ? 'var(--red)' : (data?.cpu.usage_percent || 0) >= 75 ? 'var(--amber)' : 'var(--blue)',
                transition: 'width 0.3s ease',
              }}
            />
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '11px', color: 'var(--dim)', fontFamily: 'var(--mono)' }}>
            <span>Load 1m: {data?.cpu.load_avg_1m}</span>
            <span>5m: {data?.cpu.load_avg_5m}</span>
            <span>15m: {data?.cpu.load_avg_15m}</span>
          </div>
        </div>

        {/* RAM */}
        <div style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: '6px', padding: '16px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', color: 'var(--muted)', fontSize: '12px', fontWeight: 600 }}>
            <span>RAM MEMORY</span>
            <span>{formatBytes(data?.memory.ram.used_bytes || 0)} / {formatBytes(data?.memory.ram.total_bytes || 0)}</span>
          </div>
          <div style={{ fontSize: '24px', fontWeight: 700, fontFamily: 'var(--mono)' }}>
            {data?.memory.ram.percent}%
          </div>
          <div style={{ background: 'var(--panel-2)', height: '6px', borderRadius: '3px', overflow: 'hidden' }}>
            <div
              style={{
                width: `${data?.memory.ram.percent || 0}%`,
                height: '100%',
                background: (data?.memory.ram.percent || 0) >= 90 ? 'var(--red)' : (data?.memory.ram.percent || 0) >= 75 ? 'var(--amber)' : 'var(--green)',
                transition: 'width 0.3s ease',
              }}
            />
          </div>
          <div style={{ fontSize: '11px', color: 'var(--dim)', fontFamily: 'var(--mono)' }}>
            Available: {formatBytes(data?.memory.ram.available_bytes || 0)}
          </div>
        </div>

        {/* SWAP */}
        <div style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: '6px', padding: '16px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', color: 'var(--muted)', fontSize: '12px', fontWeight: 600 }}>
            <span>SWAP MEMORY</span>
            <span>{formatBytes(data?.memory.swap.used_bytes || 0)} / {formatBytes(data?.memory.swap.total_bytes || 0)}</span>
          </div>
          <div style={{ fontSize: '24px', fontWeight: 700, fontFamily: 'var(--mono)' }}>
            {data?.memory.swap.percent}%
          </div>
          <div style={{ background: 'var(--panel-2)', height: '6px', borderRadius: '3px', overflow: 'hidden' }}>
            <div
              style={{
                width: `${data?.memory.swap.percent || 0}%`,
                height: '100%',
                background: (data?.memory.swap.percent || 0) >= 90 ? 'var(--red)' : 'var(--blue)',
                transition: 'width 0.3s ease',
              }}
            />
          </div>
          <div style={{ fontSize: '11px', color: 'var(--dim)', fontFamily: 'var(--mono)' }}>
            Free: {formatBytes(data?.memory.swap.available_bytes || 0)}
          </div>
        </div>

        {/* STORAGE */}
        <div style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: '6px', padding: '16px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', color: 'var(--muted)', fontSize: '12px', fontWeight: 600 }}>
            <span>STORAGE (ROOT /)</span>
            <span>{formatBytes(data?.storage[0]?.usage.used_bytes || 0)} / {formatBytes(data?.storage[0]?.usage.total_bytes || 0)}</span>
          </div>
          <div style={{ fontSize: '24px', fontWeight: 700, fontFamily: 'var(--mono)' }}>
            {data?.storage[0]?.usage.percent}%
          </div>
          <div style={{ background: 'var(--panel-2)', height: '6px', borderRadius: '3px', overflow: 'hidden' }}>
            <div
              style={{
                width: `${data?.storage[0]?.usage.percent || 0}%`,
                height: '100%',
                background: (data?.storage[0]?.usage.percent || 0) >= 90 ? 'var(--red)' : (data?.storage[0]?.usage.percent || 0) >= 75 ? 'var(--amber)' : 'var(--green)',
                transition: 'width 0.3s ease',
              }}
            />
          </div>
          <div style={{ fontSize: '11px', color: 'var(--dim)', fontFamily: 'var(--mono)' }}>
            Available: {formatBytes(data?.storage[0]?.usage.available_bytes || 0)}
          </div>
        </div>
      </div>

      {/* Services Health Section */}
      <div style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: '6px', overflow: 'hidden' }}>
        <div style={{ padding: '14px 16px', borderBottom: '1px solid var(--line)', fontWeight: 700, fontSize: '13px', color: 'var(--ink)' }}>
          Application Services & Infrastructure Health
        </div>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px', textAlign: 'left' }}>
            <thead>
              <tr style={{ background: 'var(--panel-2)', color: 'var(--muted)', borderBottom: '1px solid var(--line)' }}>
                <th style={{ padding: '10px 16px' }}>Service Name</th>
                <th style={{ padding: '10px 16px' }}>Port</th>
                <th style={{ padding: '10px 16px' }}>Status</th>
                <th style={{ padding: '10px 16px' }}>Health Detail</th>
                <th style={{ padding: '10px 16px', textAlign: 'right' }}>Latency</th>
              </tr>
            </thead>
            <tbody>
              {[
                data?.services.backend,
                data?.services.demo_stream,
                data?.services.ib_gateway,
                data?.services.webhook,
                data?.services.postgresql,
                data?.services.redis,
                data?.services.watchdog,
              ]
                .filter((s): s is ServiceStatus => Boolean(s))
                .map((svc) => (
                  <tr key={svc.name} style={{ borderBottom: '1px solid var(--line)' }}>
                    <td style={{ padding: '12px 16px', fontWeight: 600, color: 'var(--ink)' }}>{svc.name}</td>
                    <td style={{ padding: '12px 16px', fontFamily: 'var(--mono)', color: 'var(--muted)' }}>
                      {svc.port > 0 ? `:${svc.port}` : '—'}
                    </td>
                    <td style={{ padding: '12px 16px' }}>{renderServiceBadge(svc)}</td>
                    <td style={{ padding: '12px 16px', color: 'var(--muted)' }}>{svc.health_detail}</td>
                    <td style={{ padding: '12px 16px', textAlign: 'right', fontFamily: 'var(--mono)', color: 'var(--dim)' }}>
                      {svc.latency_ms !== null ? `${svc.latency_ms} ms` : '—'}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Service Controls Section */}
      <div style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: '6px', overflow: 'hidden' }}>
        <div style={{ padding: '14px 16px', borderBottom: '1px solid var(--line)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <div style={{ fontWeight: 700, fontSize: '13px', color: 'var(--ink)' }}>Service Controls</div>
            <div style={{ fontSize: '11px', color: 'var(--muted)', marginTop: '2px' }}>
              Manual administrative service controls (systemd allowlisted operations)
            </div>
          </div>
        </div>

        {controlFeedback ? (
          <div
            style={{
              padding: '10px 16px',
              background: controlFeedback.type === 'success' ? 'var(--green-bg)' : 'rgba(239, 68, 68, 0.1)',
              borderBottom: '1px solid var(--line)',
              color: controlFeedback.type === 'success' ? 'var(--green)' : 'var(--red)',
              fontSize: '12px',
              fontWeight: 600,
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
            }}
          >
            <span>{controlFeedback.message}</span>
            <button
              type="button"
              onClick={() => setControlFeedback(null)}
              style={{ background: 'none', border: 'none', color: 'inherit', cursor: 'pointer', fontSize: '14px' }}
            >
              ✕
            </button>
          </div>
        ) : null}

        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px', textAlign: 'left' }}>
            <thead>
              <tr style={{ background: 'var(--panel-2)', color: 'var(--muted)', borderBottom: '1px solid var(--line)' }}>
                <th style={{ padding: '10px 16px' }}>Service</th>
                <th style={{ padding: '10px 16px' }}>Systemd Unit</th>
                <th style={{ padding: '10px 16px' }}>Current State</th>
                <th style={{ padding: '10px 16px' }}>Timer / Session State</th>
                <th style={{ padding: '10px 16px', textAlign: 'right' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {CONTROL_CONFIG.map((cfg) => {
                const svcState = data?.services[cfg.servicesKey]
                const currentStatus = svcState?.status || 'UNKNOWN'
                const isPending = Boolean(pendingAction[cfg.key])
                const isRunning = currentStatus === 'RUNNING' || currentStatus === 'DEGRADED'
                const isStopped = currentStatus === 'STOPPED' || currentStatus === 'MARKET_CLOSED' || currentStatus === 'UNKNOWN'

                return (
                  <tr key={cfg.key} style={{ borderBottom: '1px solid var(--line)' }}>
                    <td style={{ padding: '12px 16px', fontWeight: 600, color: 'var(--ink)' }}>{cfg.label}</td>
                    <td style={{ padding: '12px 16px', fontFamily: 'var(--mono)', color: 'var(--muted)' }}>{cfg.unit}</td>
                    <td style={{ padding: '12px 16px' }}>
                      {isPending ? (
                        <span className="status-badge idle" style={{ padding: '2px 8px', fontSize: '11px' }}>
                          ● {pendingAction[cfg.key]}...
                        </span>
                      ) : svcState ? (
                        renderServiceBadge(svcState)
                      ) : (
                        <span className="status-badge off" style={{ padding: '2px 8px', fontSize: '11px' }}>
                          ● UNKNOWN
                        </span>
                      )}
                    </td>
                    <td style={{ padding: '12px 16px', color: 'var(--muted)', fontSize: '11px' }}>
                      {cfg.sessionControlled && currentStatus === 'MARKET_CLOSED' ? (
                        <span style={{ color: 'var(--amber)', fontWeight: 600 }}>Trading session closed</span>
                      ) : cfg.sessionControlled ? (
                        <span style={{ color: 'var(--muted)' }}>Trading-session controlled</span>
                      ) : (
                        <span style={{ color: 'var(--dim)' }}>24/7 / Continuous</span>
                      )}
                    </td>
                    <td style={{ padding: '12px 16px', textAlign: 'right' }}>
                      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px' }}>
                        <button
                          type="button"
                          disabled={isRunning || isPending}
                          onClick={() => handleOpenModal(cfg, 'start')}
                          style={{
                            padding: '4px 12px',
                            fontSize: '11px',
                            fontWeight: 600,
                            borderRadius: '4px',
                            border: '1px solid var(--line)',
                            background: isRunning || isPending ? 'var(--panel-2)' : 'var(--green-bg, #064e3b)',
                            color: isRunning || isPending ? 'var(--dim)' : 'var(--green, #3ecf8e)',
                            cursor: isRunning || isPending ? 'not-allowed' : 'pointer',
                          }}
                        >
                          START
                        </button>
                        <button
                          type="button"
                          disabled={isStopped || isPending}
                          onClick={() => handleOpenModal(cfg, 'restart')}
                          style={{
                            padding: '4px 12px',
                            fontSize: '11px',
                            fontWeight: 600,
                            borderRadius: '4px',
                            border: '1px solid var(--line)',
                            background: isStopped || isPending ? 'var(--panel-2)' : 'rgba(59, 130, 246, 0.15)',
                            color: isStopped || isPending ? 'var(--dim)' : 'var(--blue, #3b82f6)',
                            cursor: isStopped || isPending ? 'not-allowed' : 'pointer',
                          }}
                        >
                          RESTART
                        </button>
                        <button
                          type="button"
                          disabled={isStopped || isPending}
                          onClick={() => handleOpenModal(cfg, 'stop')}
                          style={{
                            padding: '4px 12px',
                            fontSize: '11px',
                            fontWeight: 600,
                            borderRadius: '4px',
                            border: '1px solid var(--line)',
                            background: isStopped || isPending ? 'var(--panel-2)' : 'rgba(239, 68, 68, 0.15)',
                            color: isStopped || isPending ? 'var(--dim)' : 'var(--red, #ef4444)',
                            cursor: isStopped || isPending ? 'not-allowed' : 'pointer',
                          }}
                        >
                          STOP
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* System & Network Info Grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(400px, 1fr))', gap: '16px' }}>
        {/* System Info */}
        <div style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: '6px', padding: '16px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
          <div style={{ fontWeight: 700, fontSize: '13px', borderBottom: '1px solid var(--line)', paddingBottom: '8px' }}>
            System Platform Information
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px', fontSize: '12px' }}>
            <div>
              <span style={{ color: 'var(--muted)' }}>Hostname:</span> <span style={{ fontFamily: 'var(--mono)' }}>{data?.system.hostname}</span>
            </div>
            <div>
              <span style={{ color: 'var(--muted)' }}>Instance:</span> <span>{data?.system.instance_type}</span>
            </div>
            <div>
              <span style={{ color: 'var(--muted)' }}>OS:</span> <span>{data?.system.operating_system}</span>
            </div>
            <div>
              <span style={{ color: 'var(--muted)' }}>Kernel:</span> <span style={{ fontFamily: 'var(--mono)' }}>{data?.system.kernel_version}</span>
            </div>
            <div>
              <span style={{ color: 'var(--muted)' }}>Architecture:</span> <span>{data?.system.architecture}</span>
            </div>
            <div>
              <span style={{ color: 'var(--muted)' }}>Uptime:</span> <span>{formatUptime(data?.system.system_uptime_seconds || 0)}</span>
            </div>
          </div>
        </div>

        {/* Network Info */}
        <div style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: '6px', padding: '16px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
          <div style={{ fontWeight: 700, fontSize: '13px', borderBottom: '1px solid var(--line)', paddingBottom: '8px' }}>
            Operational Network Topology
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px', fontSize: '12px' }}>
            <div>
              <span style={{ color: 'var(--muted)' }}>Private IP:</span> <span style={{ fontFamily: 'var(--mono)' }}>{data?.network.private_ip}</span>
            </div>
            <div>
              <span style={{ color: 'var(--muted)' }}>Loopback:</span> <span style={{ fontFamily: 'var(--mono)' }}>{data?.network.binding_loopback}</span>
            </div>
            <div style={{ gridColumn: '1 / -1' }}>
              <span style={{ color: 'var(--muted)' }}>Local Active Ports:</span>{' '}
              <span style={{ fontFamily: 'var(--mono)', color: 'var(--blue)' }}>
                {data?.network.open_ports.map((p) => `:${p}`).join('  ')}
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* Top Resource Processes */}
      {data?.top_processes && data.top_processes.length > 0 && (
        <div style={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: '6px', overflow: 'hidden' }}>
          <div style={{ padding: '14px 16px', borderBottom: '1px solid var(--line)', fontWeight: 700, fontSize: '13px', color: 'var(--ink)' }}>
            Top Resource-Consuming Processes
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px', textAlign: 'left' }}>
              <thead>
                <tr style={{ background: 'var(--panel-2)', color: 'var(--muted)', borderBottom: '1px solid var(--line)' }}>
                  <th style={{ padding: '8px 16px' }}>PID</th>
                  <th style={{ padding: '8px 16px' }}>Process Name</th>
                  <th style={{ padding: '8px 16px' }}>CPU %</th>
                  <th style={{ padding: '8px 16px' }}>RAM %</th>
                  <th style={{ padding: '8px 16px' }}>State</th>
                </tr>
              </thead>
              <tbody>
                {data.top_processes.map((proc) => (
                  <tr key={proc.pid} style={{ borderBottom: '1px solid var(--line)' }}>
                    <td style={{ padding: '8px 16px', fontFamily: 'var(--mono)', color: 'var(--dim)' }}>{proc.pid}</td>
                    <td style={{ padding: '8px 16px', fontWeight: 600, color: 'var(--ink)' }}>{proc.name}</td>
                    <td style={{ padding: '8px 16px', fontFamily: 'var(--mono)', color: proc.cpu_percent > 50 ? 'var(--amber)' : 'var(--ink)' }}>
                      {proc.cpu_percent}%
                    </td>
                    <td style={{ padding: '8px 16px', fontFamily: 'var(--mono)', color: proc.memory_percent > 30 ? 'var(--amber)' : 'var(--ink)' }}>
                      {proc.memory_percent}%
                    </td>
                    <td style={{ padding: '8px 16px', color: 'var(--muted)', textTransform: 'lowercase' }}>{proc.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Confirmation Modal */}
      {modalState ? (
        <div className="modal-overlay" onClick={() => setModalState(null)}>
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <div className={`modal-header ${modalState.action === 'stop' ? 'danger-header' : ''}`}>
              <h3>
                {modalState.action === 'stop'
                  ? '⚠️ STOP SERVICE'
                  : modalState.action === 'restart'
                  ? '🔄 RESTART SERVICE'
                  : '▶ START SERVICE'}{' '}
                — {modalState.label.toUpperCase()}
              </h3>
              <button type="button" className="modal-close" onClick={() => setModalState(null)}>
                ✕
              </button>
            </div>
            <div className="modal-body">
              <p style={{ fontSize: '13px', lineHeight: '1.5', margin: 0 }}>
                Are you sure you want to {modalState.action} <strong>{modalState.label}</strong> (
                {modalState.unit})?
              </p>
              <div
                style={{
                  marginTop: '12px',
                  padding: '10px 12px',
                  background: 'var(--panel-2)',
                  border: '1px solid var(--line)',
                  borderRadius: '4px',
                  fontSize: '11px',
                  color: 'var(--muted)',
                }}
              >
                {modalState.description}
              </div>
            </div>
            <div className="modal-footer">
              <button type="button" className="btn" onClick={() => setModalState(null)} disabled={submitting}>
                CANCEL
              </button>
              <button
                type="button"
                className={`btn ${modalState.action === 'stop' ? 'danger' : ''}`}
                onClick={handleConfirmAction}
                disabled={submitting}
              >
                {submitting
                  ? 'EXECUTING...'
                  : `${modalState.action.toUpperCase()} ${modalState.label.toUpperCase()}`}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
