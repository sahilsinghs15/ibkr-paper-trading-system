import { useCallback, useEffect, useId, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  cancelManualOrder,
  fetchGatewayStatus,
  fetchManualOrders,
  previewManualOrder,
  searchCfdInstruments,
  submitManualOrder,
  type CfdCandidateContract,
  type GatewayStatusResponse,
  type ManualOrderPreviewResponse,
  type ManualOrderRead,
  type ManualOrderSubmitResponse,
} from '../api/manualTradingApi'
import { useActiveIbkrAccount } from '../hooks/useActiveIbkrAccount'
import { normalizeIbkrAccount } from '../utils/activeAccount'

export function ManualTradePage() {
  const { ibkrAccount } = useParams<{ ibkrAccount: string }>()
  const activeAccount = useActiveIbkrAccount()
  const cleanAccount = normalizeIbkrAccount(ibkrAccount || activeAccount)

  // ── Gateway Status State ──────────────────────────────────────────
  const [gatewayStatus, setGatewayStatus] = useState<GatewayStatusResponse | null>(null)
  const [gatewayLoading, setGatewayLoading] = useState(false)
  const [gatewayError, setGatewayError] = useState<string | null>(null)

  const loadGatewayStatus = useCallback(async () => {
    try {
      setGatewayLoading(true)
      setGatewayError(null)
      const data = await fetchGatewayStatus(cleanAccount)
      setGatewayStatus(data)
    } catch (err: unknown) {
      const ae = err as { response?: { data?: { detail?: string } } }
      setGatewayError(
        ae?.response?.data?.detail || (err instanceof Error ? err.message : 'Failed to query Gateway status')
      )
    } finally {
      setGatewayLoading(false)
    }
  }, [cleanAccount])

  useEffect(() => {
    void loadGatewayStatus()
  }, [loadGatewayStatus])

  // ── CFD Discovery State ───────────────────────────────────────────
  const [searchSymbol, setSearchSymbol] = useState('')
  const [searchExchange, setSearchExchange] = useState('SMART')
  const [searchCurrency, setSearchCurrency] = useState('USD')
  const [searchLoading, setSearchLoading] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  const [candidates, setCandidates] = useState<CfdCandidateContract[]>([])
  const [hasSearched, setHasSearched] = useState(false)

  // ── Explicit Contract Selection State ─────────────────────────────
  const [selectedContract, setSelectedContract] = useState<CfdCandidateContract | null>(null)
  const [selectionError, setSelectionError] = useState<string | null>(null)

  // ── Order Ticket Form State ───────────────────────────────────────
  const [side, setSide] = useState<'BUY' | 'SELL'>('BUY')
  const [orderType, setOrderType] = useState<'LIMIT' | 'MARKET'>('LIMIT')
  const [quantity, setQuantity] = useState('100')
  const [limitPrice, setLimitPrice] = useState('')
  const [tif, setTif] = useState('DAY')

  // ── Preview & Submission State (M1-B) ─────────────────────────────
  const uid = useId().replace(/[^a-zA-Z0-9]/g, '')
  const [clientSeq, setClientSeq] = useState(1)
  const idempotencyKey = `MAN_IDEM_${uid}_${clientSeq}`
  const [isPreviewOpen, setIsPreviewOpen] = useState(false)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewData, setPreviewData] = useState<ManualOrderPreviewResponse | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)

  const [submitLoading, setSubmitLoading] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [submitResult, setSubmitResult] = useState<ManualOrderSubmitResponse | null>(null)

  // ── Open Manual Orders State (M1-E) ──────────────────────────────
  const [orders, setOrders] = useState<ManualOrderRead[]>([])
  const [ordersLoading, setOrdersLoading] = useState(false)
  const [ordersError, setOrdersError] = useState<string | null>(null)
  const [cancellingOrderId, setCancellingOrderId] = useState<number | null>(null)
  const [cancelFeedback, setCancelFeedback] = useState<{ orderId: number; message: string; isError: boolean } | null>(null)

  const loadManualOrders = useCallback(async () => {
    if (!cleanAccount) return
    try {
      setOrdersLoading(true)
      setOrdersError(null)
      const res = await fetchManualOrders(cleanAccount)
      setOrders(res.orders)
    } catch (err: unknown) {
      const ae = err as { response?: { data?: { detail?: string } } }
      setOrdersError(
        ae?.response?.data?.detail || (err instanceof Error ? err.message : 'Failed to fetch manual orders')
      )
    } finally {
      setOrdersLoading(false)
    }
  }, [cleanAccount])

  useEffect(() => {
    void loadManualOrders()
    const timer = setInterval(() => {
      void loadManualOrders()
    }, 5000)
    return () => clearInterval(timer)
  }, [loadManualOrders])

  const handleCancelOrder = async (orderId: number) => {
    if (!cleanAccount || cancellingOrderId !== null) return
    setCancellingOrderId(orderId)
    setCancelFeedback(null)
    try {
      const res = await cancelManualOrder(cleanAccount, orderId)
      setCancelFeedback({
        orderId,
        message: res.message || 'Cancellation request sent.',
        isError: !res.success,
      })
      await loadManualOrders()
    } catch (err: unknown) {
      const ae = err as { response?: { data?: { detail?: string } } }
      setCancelFeedback({
        orderId,
        message: ae?.response?.data?.detail || (err instanceof Error ? err.message : 'Failed to cancel order'),
        isError: true,
      })
    } finally {
      setCancellingOrderId(null)
    }
  }

  const handleSearch = async (e?: React.FormEvent) => {
    if (e) e.preventDefault()
    const sym = searchSymbol.trim().toUpperCase()
    if (!sym) {
      setSearchError('Please enter a symbol to search')
      return
    }
    try {
      setSearchLoading(true)
      setSearchError(null)
      setCandidates([])
      setHasSearched(true)
      const res = await searchCfdInstruments(cleanAccount, {
        symbol: sym,
        exchange: searchExchange.trim() || 'SMART',
        currency: searchCurrency.trim() || 'USD',
      })
      setCandidates(res.candidates)
      if (res.candidates.length === 0) {
        setSearchError(res.message || `No CFD contracts found for ${sym}`)
      }
    } catch (err: unknown) {
      const ae = err as { response?: { data?: { detail?: string } } }
      setSearchError(
        ae?.response?.data?.detail || (err instanceof Error ? err.message : 'CFD contract discovery failed')
      )
    } finally {
      setSearchLoading(false)
    }
  }

  const handleSelectCandidate = (candidate: CfdCandidateContract) => {
    setSelectionError(null)
    if (candidate.sec_type !== 'CFD') {
      setSelectionError(`Contract rejected: secType must be CFD (received ${candidate.sec_type})`)
      return
    }
    if (!candidate.con_id || candidate.con_id <= 0) {
      setSelectionError(`Contract rejected: con_id must be a positive integer (received ${candidate.con_id})`)
      return
    }
    if (!candidate.exchange) {
      setSelectionError('Contract rejected: missing valid exchange specification')
      return
    }
    if (!candidate.currency) {
      setSelectionError('Contract rejected: missing valid currency specification')
      return
    }

    setSelectedContract(candidate)
    setClientSeq((s) => s + 1)
    setSubmitResult(null)
    setSubmitError(null)
  }

  const handleClearSelectedContract = () => {
    setSelectedContract(null)
    setSelectionError(null)
    setPreviewData(null)
    setSubmitResult(null)
  }

  // ── Handle Open Preview Modal ─────────────────────────────────────
  const handleOpenPreview = async () => {
    if (!selectedContract || !cleanAccount) return

    setIsPreviewOpen(true)
    setPreviewLoading(true)
    setPreviewError(null)
    setSubmitError(null)

    try {
      const data = await previewManualOrder(cleanAccount, {
        symbol: selectedContract.symbol,
        con_id: selectedContract.con_id,
        sec_type: 'CFD',
        exchange: selectedContract.exchange,
        currency: selectedContract.currency,
        side,
        quantity,
        order_type: orderType,
        limit_price: orderType === 'LIMIT' ? limitPrice : null,
        tif,
        outside_rth: false,
        min_tick: selectedContract.min_tick ?? null,
      })
      setPreviewData(data)
    } catch (err: unknown) {
      const ae = err as { response?: { data?: { detail?: string } } }
      setPreviewError(
        ae?.response?.data?.detail || (err instanceof Error ? err.message : 'Pre-trade preview failed')
      )
    } finally {
      setPreviewLoading(false)
    }
  }

  // ── Handle Confirm & Submit Order ─────────────────────────────────
  const handleConfirmSubmit = async () => {
    if (!selectedContract || !cleanAccount || !previewData?.valid || submitLoading) return

    setSubmitLoading(true)
    setSubmitError(null)

    try {
      const res = await submitManualOrder(cleanAccount, {
        idempotency_key: idempotencyKey,
        symbol: selectedContract.symbol,
        con_id: selectedContract.con_id,
        sec_type: 'CFD',
        exchange: selectedContract.exchange,
        currency: selectedContract.currency,
        side,
        quantity,
        order_type: orderType,
        limit_price: orderType === 'LIMIT' ? limitPrice : null,
        tif,
        outside_rth: false,
        min_tick: selectedContract.min_tick ?? null,
      })
      setSubmitResult(res)
      setIsPreviewOpen(false) // Close modal, display banner on ticket
      void loadManualOrders()
    } catch (err: unknown) {
      const ae = err as { response?: { data?: { detail?: string } } }
      setSubmitError(
        ae?.response?.data?.detail || (err instanceof Error ? err.message : 'Order submission failed')
      )
    } finally {
      setSubmitLoading(false)
    }
  }

  // ── Helper badges ─────────────────────────────────────────────────
  const renderEnvironmentBadge = (status: GatewayStatusResponse | null) => {
    if (!status) return null

    if (status.environment === 'VERIFIED_PAPER') {
      return (
        <span
          style={{
            padding: '4px 10px',
            borderRadius: '4px',
            background: '#064e3b',
            color: '#34d399',
            fontSize: '12px',
            fontWeight: 700,
            border: '1px solid #059669',
          }}
          title={status.message}
        >
          ● VERIFIED PAPER
        </span>
      )
    }

    if (status.environment === 'VERIFIED_LIVE') {
      return (
        <span
          style={{
            padding: '4px 10px',
            borderRadius: '4px',
            background: '#7f1d1d',
            color: '#f87171',
            fontSize: '12px',
            fontWeight: 700,
            border: '1px solid #b91c1c',
          }}
          title={status.message}
        >
          ▲ VERIFIED LIVE
        </span>
      )
    }

    return (
      <span
        style={{
          padding: '4px 10px',
          borderRadius: '4px',
          background: '#78350f',
          color: '#fbbf24',
          fontSize: '12px',
          fontWeight: 700,
          border: '1px solid #d97706',
        }}
        title={status.message}
      >
        ⚠ UNKNOWN MODE ({status.verification_state})
      </span>
    )
  }

  const isFormComplete =
    selectedContract !== null &&
    parseFloat(quantity) > 0 &&
    (orderType === 'MARKET' || (orderType === 'LIMIT' && parseFloat(limitPrice) > 0))

  return (
    <main className="page manual-trade-page" style={{ padding: '24px 32px', maxWidth: '1440px', margin: '0 auto' }}>
      {/* Header / Context Bar */}
      <div className="section-h" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '24px' }}>
        <div>
          <h1 style={{ fontSize: '24px', fontWeight: 700, margin: 0, color: '#f8fafc' }}>
            MANUAL TRADING · M1-B EXECUTION
          </h1>
          <span style={{ fontSize: '13px', color: '#94a3b8' }}>
            ACCOUNT: <strong style={{ color: '#38bdf8' }}>{cleanAccount || 'NONE'}</strong> · ENVIRONMENT-AGNOSTIC ORDER PLACEMENT
          </span>
        </div>
        <div style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
          <span
            style={{
              padding: '6px 12px',
              borderRadius: '4px',
              background: '#0c4a6e',
              color: '#38bdf8',
              fontSize: '12px',
              fontWeight: 600,
              border: '1px solid #0284c7',
            }}
          >
            M1-B: PREVIEW & SUBMIT ENABLED
          </span>
          <Link
            to={`/account/${cleanAccount}/manual-trade/positions`}
            style={{
              padding: '6px 14px',
              borderRadius: '4px',
              background: '#312e81',
              color: '#c7d2fe',
              fontSize: '12px',
              fontWeight: 600,
              textDecoration: 'none',
              border: '1px solid #4338ca',
            }}
          >
            VIEW MANUAL POSITIONS →
          </Link>
        </div>
      </div>

      {/* Grid of Sections */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(12, 1fr)', gap: '20px' }}>
        {/* Band 1: Gateway Status */}
        <section
          style={{
            gridColumn: 'span 12',
            background: '#0f172a',
            border: '1px solid #1e293b',
            borderRadius: '8px',
            padding: '16px 20px',
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '12px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '16px', flexWrap: 'wrap' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <span
                  style={{
                    width: '10px',
                    height: '10px',
                    borderRadius: '50%',
                    background: gatewayStatus?.connected ? '#10b981' : '#ef4444',
                    display: 'inline-block',
                  }}
                />
                <span style={{ fontWeight: 700, color: '#f1f5f9', fontSize: '14px' }}>
                  1. GATEWAY STATUS:
                </span>
                <span
                  style={{
                    fontWeight: 700,
                    fontSize: '13px',
                    color: gatewayStatus?.connected ? '#34d399' : '#f87171',
                  }}
                >
                  {gatewayStatus?.connected ? 'CONNECTED' : 'DISCONNECTED'}
                </span>
              </div>

              {renderEnvironmentBadge(gatewayStatus)}

              <div style={{ fontSize: '13px', color: '#94a3b8' }}>
                Gateway:{' '}
                <strong style={{ color: '#e2e8f0' }}>
                  {gatewayStatus ? `${gatewayStatus.host}:${gatewayStatus.port}` : '—'}
                </strong>{' '}
                (ClientID: {gatewayStatus?.client_id ?? '—'})
              </div>

              <div style={{ fontSize: '13px', color: '#94a3b8' }}>
                Account:{' '}
                <strong style={{ color: '#38bdf8' }}>
                  {gatewayStatus?.ibkr_account || cleanAccount || '—'}
                </strong>
                {gatewayStatus?.managed_accounts && gatewayStatus.managed_accounts.length > 0 && (
                  <span style={{ color: '#64748b', marginLeft: '6px', fontSize: '12px' }}>
                    [Broker Accounts: {gatewayStatus.managed_accounts.join(', ')}]
                  </span>
                )}
              </div>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              {gatewayLoading && (
                <span style={{ fontSize: '12px', color: '#38bdf8' }}>Refreshing...</span>
              )}
              <button
                type="button"
                onClick={() => void loadGatewayStatus()}
                disabled={gatewayLoading}
                style={{
                  padding: '4px 12px',
                  borderRadius: '4px',
                  background: '#1e293b',
                  color: '#e2e8f0',
                  border: '1px solid #334155',
                  fontSize: '12px',
                  cursor: 'pointer',
                }}
              >
                Refresh
              </button>
            </div>
          </div>

          {gatewayError && (
            <div style={{ marginTop: '12px', padding: '8px 12px', background: '#451a1a', border: '1px solid #7f1d1d', borderRadius: '4px', color: '#fca5a5', fontSize: '12px' }}>
              {gatewayError}
            </div>
          )}

          {gatewayStatus?.message && !gatewayError && (
            <div style={{ marginTop: '10px', fontSize: '12px', color: '#64748b' }}>
              {gatewayStatus.message}
            </div>
          )}
        </section>

        {/* Band 2: CFD Instrument Discovery & Candidates Table */}
        <section
          style={{
            gridColumn: 'span 5',
            background: '#0f172a',
            border: '1px solid #1e293b',
            borderRadius: '8px',
            padding: '20px',
            display: 'flex',
            flexDirection: 'column',
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <h2 style={{ fontSize: '14px', fontWeight: 600, color: '#94a3b8', margin: 0, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              2. CFD Instrument Discovery
            </h2>
            <span style={{ fontSize: '11px', color: '#64748b' }}>
              Single TWSClient · Rate Limited
            </span>
          </div>

          <form onSubmit={handleSearch} style={{ display: 'flex', flexDirection: 'column', gap: '12px', marginBottom: '16px' }}>
            <div>
              <label style={{ display: 'block', fontSize: '12px', color: '#94a3b8', marginBottom: '4px', fontWeight: 500 }}>
                Symbol (e.g. AAPL, MSFT, TSLA, SPY)
              </label>
              <div style={{ display: 'flex', gap: '8px' }}>
                <input
                  type="text"
                  value={searchSymbol}
                  onChange={(e) => setSearchSymbol(e.target.value.toUpperCase())}
                  placeholder="Enter Symbol"
                  style={{
                    flex: 1,
                    padding: '8px 12px',
                    background: '#1e293b',
                    border: '1px solid #334155',
                    borderRadius: '4px',
                    color: '#f8fafc',
                    fontSize: '14px',
                    fontWeight: 600,
                  }}
                />
                <button
                  type="submit"
                  disabled={searchLoading || !searchSymbol.trim()}
                  style={{
                    padding: '8px 16px',
                    borderRadius: '4px',
                    border: 'none',
                    background: searchLoading || !searchSymbol.trim() ? '#334155' : '#0284c7',
                    color: '#ffffff',
                    fontWeight: 600,
                    fontSize: '13px',
                    cursor: searchLoading || !searchSymbol.trim() ? 'not-allowed' : 'pointer',
                  }}
                >
                  {searchLoading ? 'Searching...' : 'Search CFDs'}
                </button>
              </div>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
              <div>
                <label style={{ display: 'block', fontSize: '11px', color: '#64748b', marginBottom: '2px' }}>Exchange</label>
                <input
                  type="text"
                  value={searchExchange}
                  onChange={(e) => setSearchExchange(e.target.value.toUpperCase())}
                  placeholder="SMART"
                  style={{
                    width: '100%',
                    padding: '6px 10px',
                    background: '#1e293b',
                    border: '1px solid #334155',
                    borderRadius: '4px',
                    color: '#f8fafc',
                    fontSize: '12px',
                  }}
                />
              </div>
              <div>
                <label style={{ display: 'block', fontSize: '11px', color: '#64748b', marginBottom: '2px' }}>Currency</label>
                <input
                  type="text"
                  value={searchCurrency}
                  onChange={(e) => setSearchCurrency(e.target.value.toUpperCase())}
                  placeholder="USD"
                  style={{
                    width: '100%',
                    padding: '6px 10px',
                    background: '#1e293b',
                    border: '1px solid #334155',
                    borderRadius: '4px',
                    color: '#f8fafc',
                    fontSize: '12px',
                  }}
                />
              </div>
            </div>
          </form>

          {searchError && (
            <div style={{ padding: '10px 12px', background: '#451a1a', border: '1px solid #7f1d1d', borderRadius: '4px', color: '#fca5a5', fontSize: '12px', marginBottom: '12px' }}>
              {searchError}
            </div>
          )}

          {selectionError && (
            <div style={{ padding: '10px 12px', background: '#451a1a', border: '1px solid #7f1d1d', borderRadius: '4px', color: '#fca5a5', fontSize: '12px', marginBottom: '12px' }}>
              {selectionError}
            </div>
          )}

          {/* Candidates List / Explicit Selection Table */}
          <div style={{ flex: 1, minHeight: '200px', overflowY: 'auto' }}>
            <div style={{ fontSize: '12px', fontWeight: 600, color: '#94a3b8', marginBottom: '8px', display: 'flex', justifyContent: 'space-between' }}>
              <span>DISCOVERED CFD CANDIDATES ({candidates.length})</span>
              {candidates.length > 1 && (
                <span style={{ color: '#38bdf8' }}>Multiple matches: select one</span>
              )}
            </div>

            {candidates.length === 0 && !searchLoading && !hasSearched && (
              <div style={{ padding: '32px 16px', textAlign: 'center', color: '#64748b', fontSize: '13px', background: '#020617', borderRadius: '4px', border: '1px dashed #1e293b' }}>
                Search for an equity/index symbol above to discover available IBKR CFD contracts.
              </div>
            )}

            {candidates.length === 0 && !searchLoading && hasSearched && !searchError && (
              <div style={{ padding: '32px 16px', textAlign: 'center', color: '#64748b', fontSize: '13px', background: '#020617', borderRadius: '4px', border: '1px dashed #1e293b' }}>
                No matching CFD contracts returned by IBKR.
              </div>
            )}

            {candidates.length > 0 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {candidates.map((c) => {
                  const isSelected = selectedContract?.con_id === c.con_id
                  return (
                    <div
                      key={c.con_id}
                      style={{
                        padding: '12px',
                        background: isSelected ? '#1e3a5f' : '#1e293b',
                        border: isSelected ? '1px solid #38bdf8' : '1px solid #334155',
                        borderRadius: '6px',
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                      }}
                    >
                      <div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
                          <strong style={{ color: '#f8fafc', fontSize: '14px' }}>{c.symbol}</strong>
                          <span style={{ fontSize: '11px', padding: '1px 6px', borderRadius: '3px', background: '#0369a1', color: '#bae6fd' }}>
                            {c.sec_type}
                          </span>
                          <span style={{ fontSize: '12px', color: '#94a3b8' }}>
                            conId: <strong style={{ color: '#38bdf8' }}>{c.con_id}</strong>
                          </span>
                        </div>
                        <div style={{ fontSize: '12px', color: '#64748b', display: 'flex', gap: '12px' }}>
                          <span>Exch: <strong style={{ color: '#cbd5e1' }}>{c.exchange}</strong></span>
                          <span>Curr: <strong style={{ color: '#cbd5e1' }}>{c.currency}</strong></span>
                          {c.min_tick !== null && c.min_tick !== undefined && (
                            <span>MinTick: <strong style={{ color: '#cbd5e1' }}>{c.min_tick}</strong></span>
                          )}
                          {c.trading_class && (
                            <span>Class: <strong style={{ color: '#cbd5e1' }}>{c.trading_class}</strong></span>
                          )}
                        </div>
                        {c.long_name && (
                          <div style={{ fontSize: '11px', color: '#94a3b8', marginTop: '2px' }}>
                            {c.long_name}
                          </div>
                        )}
                      </div>

                      <button
                        type="button"
                        onClick={() => handleSelectCandidate(c)}
                        style={{
                          padding: '6px 14px',
                          borderRadius: '4px',
                          border: 'none',
                          background: isSelected ? '#059669' : '#0284c7',
                          color: '#ffffff',
                          fontWeight: 600,
                          fontSize: '12px',
                          cursor: 'pointer',
                        }}
                      >
                        {isSelected ? '✓ Selected' : 'Select Contract'}
                      </button>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </section>

        {/* Band 3: Order Ticket (Discretionary Shell + Selected Contract + M1-B Preview) */}
        <section
          style={{
            gridColumn: 'span 4',
            background: '#0f172a',
            border: '1px solid #1e293b',
            borderRadius: '8px',
            padding: '20px',
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <h2 style={{ fontSize: '14px', fontWeight: 600, color: '#94a3b8', margin: 0, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              3. Order Ticket (Discretionary)
            </h2>
            <span style={{ fontSize: '11px', color: '#38bdf8', fontWeight: 600 }}>
              M1-B ACTIVE
            </span>
          </div>

          {/* Submission Banner (if completed) */}
          {submitResult && (
            <div
              style={{
                marginBottom: '16px',
                padding: '12px',
                background: '#064e3b',
                border: '1px solid #059669',
                borderRadius: '6px',
                color: '#d1fae5',
                fontSize: '12px',
              }}
            >
              <div style={{ fontWeight: 700, fontSize: '13px', marginBottom: '4px', color: '#34d399' }}>
                ✓ ORDER SUBMITTED TO IBKR
              </div>
              <div>Internal Order ID: <strong>{submitResult.order.internal_order_id}</strong></div>
              <div>Broker Order ID: <strong>{submitResult.order.broker_order_id ?? 'Allocated'}</strong></div>
              <div>Status: <strong>{submitResult.order.status}</strong></div>
              <div style={{ marginTop: '6px', fontSize: '11px', color: '#a7f3d0' }}>
                Note: Order is SUBMITTED. Working orders, execution fills, and cancel controls update automatically below.
              </div>
            </div>
          )}

          {submitError && (
            <div
              style={{
                marginBottom: '16px',
                padding: '12px',
                background: '#451a1a',
                border: '1px solid #7f1d1d',
                borderRadius: '6px',
                color: '#fca5a5',
                fontSize: '12px',
              }}
            >
              <strong style={{ display: 'block', marginBottom: '4px' }}>Submission Failed:</strong>
              {submitError}
            </div>
          )}

          {/* Selected Contract Card */}
          {selectedContract ? (
            <div
              style={{
                marginBottom: '16px',
                padding: '12px 14px',
                background: '#042f2e',
                border: '1px solid #0d9488',
                borderRadius: '6px',
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '6px' }}>
                <div>
                  <span style={{ fontSize: '11px', color: '#5eead4', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                    Selected Contract
                  </span>
                  <div style={{ fontSize: '16px', fontWeight: 700, color: '#f0fdfa' }}>
                    {selectedContract.symbol}{' '}
                    <span style={{ fontSize: '12px', color: '#2dd4bf' }}>({selectedContract.sec_type})</span>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={handleClearSelectedContract}
                  style={{
                    background: 'transparent',
                    border: '1px solid #14b8a6',
                    borderRadius: '4px',
                    color: '#5eead4',
                    fontSize: '11px',
                    padding: '2px 8px',
                    cursor: 'pointer',
                  }}
                >
                  Change
                </button>
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: '4px', fontSize: '12px', color: '#ccfbf1' }}>
                <div>conId: <strong>{selectedContract.con_id}</strong></div>
                <div>Exchange: <strong>{selectedContract.exchange}</strong></div>
                <div>Currency: <strong>{selectedContract.currency}</strong></div>
                <div>Min Tick: <strong>{selectedContract.min_tick ?? '—'}</strong></div>
                {selectedContract.local_symbol && (
                  <div style={{ gridColumn: 'span 2' }}>Local Symbol: <strong>{selectedContract.local_symbol}</strong></div>
                )}
              </div>
            </div>
          ) : (
            <div
              style={{
                marginBottom: '16px',
                padding: '12px',
                background: '#1e293b',
                border: '1px dashed #475569',
                borderRadius: '6px',
                textAlign: 'center',
                color: '#94a3b8',
                fontSize: '12px',
              }}
            >
              No contract selected. Please search and explicitly select a resolved CFD contract above.
            </div>
          )}

          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            <div style={{ display: 'flex', gap: '8px' }}>
              <button
                type="button"
                onClick={() => setSide('BUY')}
                style={{
                  flex: 1,
                  padding: '8px',
                  borderRadius: '4px',
                  border: 'none',
                  background: side === 'BUY' ? '#16a34a' : '#1e293b',
                  color: '#ffffff',
                  fontWeight: 600,
                  cursor: 'pointer',
                }}
              >
                BUY
              </button>
              <button
                type="button"
                onClick={() => setSide('SELL')}
                style={{
                  flex: 1,
                  padding: '8px',
                  borderRadius: '4px',
                  border: 'none',
                  background: side === 'SELL' ? '#dc2626' : '#1e293b',
                  color: '#ffffff',
                  fontWeight: 600,
                  cursor: 'pointer',
                }}
              >
                SELL
              </button>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
              <div>
                <label style={{ display: 'block', fontSize: '12px', color: '#64748b', marginBottom: '4px' }}>Order Type</label>
                <select
                  value={orderType}
                  onChange={(e) => setOrderType(e.target.value as 'LIMIT' | 'MARKET')}
                  style={{
                    width: '100%',
                    padding: '8px',
                    background: '#1e293b',
                    border: '1px solid #334155',
                    borderRadius: '4px',
                    color: '#f8fafc',
                  }}
                >
                  <option value="LIMIT">LIMIT</option>
                  <option value="MARKET">MARKET</option>
                </select>
              </div>
              <div>
                <label style={{ display: 'block', fontSize: '12px', color: '#64748b', marginBottom: '4px' }}>Quantity</label>
                <input
                  type="number"
                  value={quantity}
                  onChange={(e) => setQuantity(e.target.value)}
                  style={{
                    width: '100%',
                    padding: '8px',
                    background: '#1e293b',
                    border: '1px solid #334155',
                    borderRadius: '4px',
                    color: '#f8fafc',
                  }}
                />
              </div>
            </div>

            {orderType === 'LIMIT' && (
              <div>
                <label style={{ display: 'block', fontSize: '12px', color: '#64748b', marginBottom: '4px' }}>
                  Limit Price ({selectedContract?.currency || 'USD'})
                </label>
                <input
                  type="number"
                  step={selectedContract?.min_tick ? String(selectedContract.min_tick) : '0.01'}
                  placeholder="0.00"
                  value={limitPrice}
                  onChange={(e) => setLimitPrice(e.target.value)}
                  style={{
                    width: '100%',
                    padding: '8px',
                    background: '#1e293b',
                    border: '1px solid #334155',
                    borderRadius: '4px',
                    color: '#f8fafc',
                  }}
                />
              </div>
            )}

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
              <div>
                <label style={{ display: 'block', fontSize: '12px', color: '#64748b', marginBottom: '4px' }}>Time In Force</label>
                <select
                  value={tif}
                  onChange={(e) => setTif(e.target.value)}
                  style={{
                    width: '100%',
                    padding: '8px',
                    background: '#1e293b',
                    border: '1px solid #334155',
                    borderRadius: '4px',
                    color: '#f8fafc',
                  }}
                >
                  <option value="DAY">DAY</option>
                  <option value="GTC">GTC</option>
                  <option value="IOC">IOC</option>
                </select>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', marginTop: '18px' }}>
                <span style={{ fontSize: '12px', color: '#64748b' }}>
                  Outside RTH: <strong style={{ color: '#ef4444' }}>OFF (Enforced)</strong>
                </span>
              </div>
            </div>

            <button
              type="button"
              disabled={!isFormComplete || submitLoading}
              onClick={() => void handleOpenPreview()}
              style={{
                marginTop: '8px',
                padding: '12px',
                borderRadius: '4px',
                background: !isFormComplete || submitLoading ? '#334155' : '#0284c7',
                color: '#ffffff',
                border: 'none',
                fontWeight: 700,
                fontSize: '13px',
                cursor: !isFormComplete || submitLoading ? 'not-allowed' : 'pointer',
              }}
            >
              PREVIEW ORDER (PRE-TRADE CHECK) →
            </button>
          </div>
        </section>

        {/* Band 4: Risk / Margin Preview */}
        <section
          style={{
            gridColumn: 'span 3',
            background: '#0f172a',
            border: '1px solid #1e293b',
            borderRadius: '8px',
            padding: '20px',
          }}
        >
          <h2 style={{ fontSize: '14px', fontWeight: 600, color: '#94a3b8', margin: '0 0 16px 0', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
            4. Risk / Margin Preview
          </h2>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', color: '#cbd5e1', fontSize: '13px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', borderBottom: '1px solid #1e293b', paddingBottom: '8px' }}>
              <span style={{ color: '#64748b' }}>Estimated Notional:</span>
              <span style={{ fontWeight: 600 }}>
                {previewData?.notional
                  ? `${previewData.notional} ${selectedContract?.currency || 'USD'}`
                  : orderType === 'MARKET' ? 'Market Order' : '—'}
              </span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', borderBottom: '1px solid #1e293b', paddingBottom: '8px' }}>
              <span style={{ color: '#64748b' }}>Initial Margin Change:</span>
              <span style={{ fontWeight: 600 }}>
                {previewData?.init_margin_change ? `${previewData.init_margin_change} USD` : '—'}
              </span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', borderBottom: '1px solid #1e293b', paddingBottom: '8px' }}>
              <span style={{ color: '#64748b' }}>Maint Margin Change:</span>
              <span style={{ fontWeight: 600 }}>
                {previewData?.maint_margin_change ? `${previewData.maint_margin_change} USD` : '—'}
              </span>
            </div>
            <div style={{ padding: '8px', background: '#1e293b', borderRadius: '4px', fontSize: '11px', color: '#94a3b8' }}>
              Pre-trade what-if check queries IBKR Gateway via rate limiter priority P4 without order execution.
            </div>
          </div>
        </section>

        {/* Band 5: Open Manual Orders */}
        <section
          style={{
            gridColumn: 'span 12',
            background: '#0f172a',
            border: '1px solid #1e293b',
            borderRadius: '8px',
            padding: '20px',
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <h2 style={{ fontSize: '14px', fontWeight: 600, color: '#94a3b8', margin: 0, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              5. Open Manual Orders (source=manual)
            </h2>
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
              {ordersLoading && <span style={{ fontSize: '12px', color: '#38bdf8' }}>Updating...</span>}
              <button
                type="button"
                onClick={() => void loadManualOrders()}
                disabled={ordersLoading}
                style={{
                  padding: '4px 10px',
                  borderRadius: '4px',
                  background: '#1e293b',
                  color: '#e2e8f0',
                  fontSize: '12px',
                  fontWeight: 600,
                  cursor: 'pointer',
                  border: '1px solid #334155',
                }}
              >
                Refresh Orders
              </button>
            </div>
          </div>

          {cancelFeedback && (
            <div
              style={{
                padding: '8px 12px',
                borderRadius: '4px',
                marginBottom: '12px',
                fontSize: '12px',
                background: cancelFeedback.isError ? '#451a1a' : '#064e3b',
                color: cancelFeedback.isError ? '#fca5a5' : '#34d399',
                border: cancelFeedback.isError ? '1px solid #7f1d1d' : '1px solid #059669',
              }}
            >
              {cancelFeedback.message}
            </div>
          )}

          {ordersError && (
            <div style={{ padding: '8px 12px', borderRadius: '4px', marginBottom: '12px', fontSize: '12px', background: '#451a1a', color: '#fca5a5' }}>
              {ordersError}
            </div>
          )}

          <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: '13px' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid #334155', color: '#64748b' }}>
                <th style={{ padding: '8px' }}>TIME</th>
                <th style={{ padding: '8px' }}>INTERNAL ID</th>
                <th style={{ padding: '8px' }}>BROKER ID</th>
                <th style={{ padding: '8px' }}>PERM ID</th>
                <th style={{ padding: '8px' }}>SYMBOL</th>
                <th style={{ padding: '8px' }}>CON ID</th>
                <th style={{ padding: '8px' }}>SIDE</th>
                <th style={{ padding: '8px' }}>QTY</th>
                <th style={{ padding: '8px' }}>PRICE</th>
                <th style={{ padding: '8px' }}>STATUS</th>
                <th style={{ padding: '8px', textAlign: 'right' }}>ACTIONS</th>
              </tr>
            </thead>
            <tbody>
              {orders.length > 0 ? (
                orders.map((order) => {
                  const isCancellable =
                    order.status === 'SUBMITTED' ||
                    order.status === 'PARTIALLY_FILLED' ||
                    order.status === 'PENDING_SUBMIT'
                  const isCancellingThis = cancellingOrderId === order.id

                  let statusBg = '#075985'
                  let statusColor = '#38bdf8'
                  if (order.status === 'FILLED') {
                    statusBg = '#064e3b'
                    statusColor = '#34d399'
                  } else if (order.status === 'CANCELLED') {
                    statusBg = '#334155'
                    statusColor = '#94a3b8'
                  } else if (order.status === 'REJECTED' || order.status === 'ERROR') {
                    statusBg = '#451a1a'
                    statusColor = '#f87171'
                  } else if (order.status === 'PARTIALLY_FILLED') {
                    statusBg = '#78350f'
                    statusColor = '#fbbf24'
                  }

                  const timeStr = order.created_at ? new Date(order.created_at).toLocaleTimeString() : '—'

                  return (
                    <tr key={order.id} style={{ borderBottom: '1px solid #1e293b' }}>
                      <td style={{ padding: '8px', color: '#94a3b8' }}>{timeStr}</td>
                      <td style={{ padding: '8px', fontWeight: 600, color: '#38bdf8' }}>{order.internal_order_id}</td>
                      <td style={{ padding: '8px', color: '#94a3b8' }}>{order.broker_order_id ?? '—'}</td>
                      <td style={{ padding: '8px', color: '#64748b' }}>{order.perm_id ?? '—'}</td>
                      <td style={{ padding: '8px' }}>{order.symbol}</td>
                      <td style={{ padding: '8px', color: '#64748b' }}>{order.con_id}</td>
                      <td style={{ padding: '8px', color: order.side === 'BUY' ? '#34d399' : '#f87171', fontWeight: 600 }}>
                        {order.side}
                      </td>
                      <td style={{ padding: '8px' }}>
                        {order.filled_quantity !== undefined && Number(order.filled_quantity) > 0 ? (
                          <span>
                            <strong style={{ color: '#38bdf8' }}>{String(order.filled_quantity)}</strong> / {String(order.quantity)}
                          </span>
                        ) : (
                          String(order.quantity)
                        )}
                      </td>
                      <td style={{ padding: '8px' }}>{order.limit_price ? String(order.limit_price) : 'MKT'}</td>
                      <td style={{ padding: '8px' }}>
                        <span style={{ padding: '2px 8px', borderRadius: '4px', background: statusBg, color: statusColor, fontSize: '11px', fontWeight: 700 }}>
                          {order.status}
                        </span>
                      </td>
                      <td style={{ padding: '8px', textAlign: 'right' }}>
                        {isCancellable ? (
                          <button
                            type="button"
                            onClick={() => void handleCancelOrder(order.id)}
                            disabled={isCancellingThis || cancellingOrderId !== null}
                            style={{
                              padding: '3px 8px',
                              borderRadius: '4px',
                              background: isCancellingThis ? '#334155' : '#7f1d1d',
                              color: isCancellingThis ? '#94a3b8' : '#fca5a5',
                              fontSize: '11px',
                              fontWeight: 700,
                              cursor: isCancellingThis ? 'not-allowed' : 'pointer',
                              border: '1px solid #991b1b',
                            }}
                          >
                            {isCancellingThis ? 'Cancelling...' : 'Cancel'}
                          </button>
                        ) : (
                          <span style={{ color: '#475569', fontSize: '12px' }}>Final</span>
                        )}
                      </td>
                    </tr>
                  )
                })
              ) : (
                <tr>
                  <td colSpan={11} style={{ padding: '24px', textAlign: 'center', color: '#64748b' }}>
                    No manual orders found for this account. (Orders are isolated from engine orders).
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </section>

        {/* Band 6: Manual Executions */}
        <section
          style={{
            gridColumn: 'span 6',
            background: '#0f172a',
            border: '1px solid #1e293b',
            borderRadius: '8px',
            padding: '20px',
          }}
        >
          <h2 style={{ fontSize: '14px', fontWeight: 600, color: '#94a3b8', margin: '0 0 16px 0', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
            6. Manual Executions (manual_executions)
          </h2>
          <div style={{ padding: '24px', textAlign: 'center', color: '#94a3b8', fontSize: '13px' }}>
            Inbound executions are ingested via IBKR callbacks and deduplicated exactly-once on <code style={{ color: '#38bdf8' }}>exec_id UNIQUE</code>.
          </div>
        </section>

        {/* Band 7: Manual Positions Summary */}
        <section
          style={{
            gridColumn: 'span 6',
            background: '#0f172a',
            border: '1px solid #1e293b',
            borderRadius: '8px',
            padding: '20px',
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <h2 style={{ fontSize: '14px', fontWeight: 600, color: '#94a3b8', margin: 0, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              7. Manual Positions (manual_positions)
            </h2>
            <Link to={`/account/${cleanAccount}/manual-trade/positions`} style={{ color: '#38bdf8', fontSize: '12px', textDecoration: 'none' }}>
              Full Ledger →
            </Link>
          </div>
          <div style={{ padding: '24px', textAlign: 'center', color: '#94a3b8', fontSize: '13px' }}>
            Discretionary positions operate on a dedicated position ledger isolated from engine <code style={{ color: '#38bdf8' }}>PositionModel</code>.
          </div>
        </section>

        {/* Band 8: Manual Controls & Safety */}
        <section
          style={{
            gridColumn: 'span 12',
            background: '#0f172a',
            border: '1px solid #1e293b',
            borderRadius: '8px',
            padding: '20px',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
          }}
        >
          <div>
            <h2 style={{ fontSize: '14px', fontWeight: 600, color: '#94a3b8', margin: '0 0 4px 0', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              8. Manual Controls & Safety
            </h2>
            <span style={{ fontSize: '12px', color: '#64748b' }}>
              Account Halt State: <strong style={{ color: '#10b981' }}>NORMAL (Active)</strong> · Groundwork for M4
            </span>
          </div>
          <button
            type="button"
            disabled
            style={{
              padding: '8px 16px',
              borderRadius: '4px',
              background: '#334155',
              color: '#94a3b8',
              border: 'none',
              fontSize: '12px',
              fontWeight: 600,
              cursor: 'not-allowed',
            }}
          >
            MANUAL FLATTEN (DISABLED IN M1-B)
          </button>
        </section>
      </div>

      {/* ── M1-B PREVIEW & CONFIRMATION MODAL ────────────────────────── */}
      {isPreviewOpen && (
        <div
          style={{
            position: 'fixed',
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            background: 'rgba(0, 0, 0, 0.75)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 1000,
            padding: '20px',
          }}
        >
          <div
            style={{
              background: '#0f172a',
              border: '1px solid #334155',
              borderRadius: '8px',
              width: '100%',
              maxWidth: '560px',
              padding: '24px',
              boxShadow: '0 25px 50px -12px rgba(0, 0, 0, 0.5)',
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px' }}>
              <h2 style={{ fontSize: '18px', fontWeight: 700, margin: 0, color: '#f8fafc' }}>
                CONFIRM MANUAL ORDER
              </h2>
              <button
                type="button"
                onClick={() => setIsPreviewOpen(false)}
                disabled={submitLoading}
                style={{
                  background: 'transparent',
                  border: 'none',
                  color: '#94a3b8',
                  fontSize: '20px',
                  cursor: submitLoading ? 'not-allowed' : 'pointer',
                }}
              >
                ×
              </button>
            </div>

            {previewLoading && (
              <div style={{ padding: '40px', textAlign: 'center', color: '#38bdf8', fontSize: '14px' }}>
                Evaluating pre-trade safety gates & probing margin...
              </div>
            )}

            {previewError && (
              <div style={{ padding: '12px 16px', background: '#451a1a', border: '1px solid #7f1d1d', borderRadius: '6px', color: '#fca5a5', marginBottom: '16px', fontSize: '13px' }}>
                <strong>Preview Error:</strong> {previewError}
              </div>
            )}

            {previewData && !previewLoading && (
              <div>
                {/* Validation Status Notice */}
                {!previewData.valid ? (
                  <div style={{ padding: '12px', background: '#451a1a', border: '1px solid #ef4444', borderRadius: '6px', color: '#fca5a5', marginBottom: '16px', fontSize: '13px' }}>
                    <div style={{ fontWeight: 700, marginBottom: '6px' }}>ORDER REJECTED BY SAFETY GATES:</div>
                    <ul style={{ margin: 0, paddingLeft: '18px' }}>
                      {previewData.errors.map((e, idx) => (
                        <li key={idx}>{e}</li>
                      ))}
                    </ul>
                  </div>
                ) : (
                  <div style={{ padding: '10px 14px', background: '#064e3b', border: '1px solid #059669', borderRadius: '6px', color: '#34d399', marginBottom: '16px', fontSize: '12px', fontWeight: 600 }}>
                    ✓ ALL PRE-TRADE SAFETY GATES PASSED
                  </div>
                )}

                {/* Warnings (if any) */}
                {previewData.warnings.length > 0 && (
                  <div style={{ padding: '10px 12px', background: '#78350f', border: '1px solid #d97706', borderRadius: '6px', color: '#fde68a', marginBottom: '16px', fontSize: '12px' }}>
                    {previewData.warnings.map((w, idx) => (
                      <div key={idx}>⚠ {w}</div>
                    ))}
                  </div>
                )}

                {/* Order Summary Grid */}
                <div style={{ background: '#1e293b', borderRadius: '6px', padding: '14px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px', fontSize: '13px', marginBottom: '20px' }}>
                  <div>Account: <strong style={{ color: '#38bdf8' }}>{cleanAccount}</strong></div>
                  <div>Environment: <strong style={{ color: '#e2e8f0' }}>{previewData.environment}</strong></div>
                  <div>Contract: <strong style={{ color: '#f8fafc' }}>{previewData.symbol} ({previewData.sec_type})</strong></div>
                  <div>conId: <strong style={{ color: '#cbd5e1' }}>{previewData.con_id}</strong></div>
                  <div>
                    Side:{' '}
                    <strong style={{ color: previewData.side === 'BUY' ? '#34d399' : '#f87171' }}>
                      {previewData.side}
                    </strong>
                  </div>
                  <div>Quantity: <strong style={{ color: '#f8fafc' }}>{String(previewData.quantity)}</strong></div>
                  <div>Order Type: <strong style={{ color: '#f8fafc' }}>{previewData.order_type}</strong></div>
                  <div>Price: <strong style={{ color: '#f8fafc' }}>{previewData.limit_price ? String(previewData.limit_price) : 'MKT'}</strong></div>
                  <div style={{ gridColumn: 'span 2', borderTop: '1px solid #334155', paddingTop: '8px' }}>
                    Estimated Notional:{' '}
                    <strong style={{ color: '#38bdf8' }}>
                      {previewData.notional ? `${previewData.notional} ${previewData.currency}` : 'Market Order (no fixed notional)'}
                    </strong>
                  </div>
                  {previewData.init_margin_change && (
                    <div style={{ gridColumn: 'span 2' }}>
                      Est Margin Impact:{' '}
                      <strong style={{ color: '#cbd5e1' }}>
                        +{previewData.init_margin_change} {previewData.currency}
                      </strong>
                    </div>
                  )}
                </div>

                {/* Modal Action Buttons */}
                <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '12px' }}>
                  <button
                    type="button"
                    onClick={() => setIsPreviewOpen(false)}
                    disabled={submitLoading}
                    style={{
                      padding: '10px 18px',
                      borderRadius: '4px',
                      background: '#334155',
                      color: '#e2e8f0',
                      border: 'none',
                      fontWeight: 600,
                      cursor: submitLoading ? 'not-allowed' : 'pointer',
                    }}
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleConfirmSubmit()}
                    disabled={!previewData.valid || submitLoading}
                    style={{
                      padding: '10px 22px',
                      borderRadius: '4px',
                      background: !previewData.valid || submitLoading ? '#475569' : '#16a34a',
                      color: '#ffffff',
                      border: 'none',
                      fontWeight: 700,
                      fontSize: '13px',
                      cursor: !previewData.valid || submitLoading ? 'not-allowed' : 'pointer',
                    }}
                  >
                    {submitLoading ? 'Submitting to IBKR...' : 'Confirm & Place Order'}
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </main>
  )
}
