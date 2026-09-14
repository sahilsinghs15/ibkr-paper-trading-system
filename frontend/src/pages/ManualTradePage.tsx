import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import {
  cancelManualOrder,
  fetchGatewayStatus,
  fetchManualOrders,
  fetchManualPositions,
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
import { genManualIdemKey } from '../utils/manualIdempotency'
import { usePnlStore, groupLegs } from '../store/pnlStore'
import { fmtPnl, pnlClass, num } from '../utils/format'

export function ManualTradePage() {
  const { ibkrAccount } = useParams<{ ibkrAccount: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const activeAccount = useActiveIbkrAccount()
  const cleanAccount = normalizeIbkrAccount(ibkrAccount || activeAccount)
  const closeSymbol = searchParams.get('close_symbol')
  const closeSide = searchParams.get('close_side') as 'BUY' | 'SELL' | null
  const closeQty = searchParams.get('close_qty')
  const closeTradeId = searchParams.get('close_trade_id')
  const isCloseMode = !!(closeSymbol && closeSide && closeQty)

  // ── Gateway status ────────────────────────────────────────────────
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
      setGatewayError(ae?.response?.data?.detail || (err instanceof Error ? err.message : 'Failed to query gateway'))
    } finally {
      setGatewayLoading(false)
    }
  }, [cleanAccount])

  useEffect(() => {
    void loadGatewayStatus()
  }, [loadGatewayStatus])

  // ── Close manual position prefill from Main Positions ─────────────
  useEffect(() => {
    if (!isCloseMode || !closeSymbol || !closeSide || !closeQty) return
    setSearchSymbol(closeSymbol.toUpperCase())
    setSide(closeSide)
    setQuantity(closeQty)
    setOrderType('MARKET')
    setLimitPrice('')
    // Try to resolve contract from manual positions for trade_id
    if (closeTradeId && cleanAccount) {
      fetchManualPositions(cleanAccount).then((res) => {
        const pos = res.positions.find((p) => p.trade_id === closeTradeId)
        if (pos) {
          setSelectedContract({
            con_id: pos.con_id,
            symbol: pos.symbol,
            sec_type: 'CFD',
            exchange: 'SMART',
            currency: pos.currency,
            local_symbol: null,
            trading_class: null,
            min_tick: null,
            primary_exchange: null,
            long_name: null,
          })
        }
      }).catch(() => {})
    }
  }, [isCloseMode, closeSymbol, closeSide, closeQty, closeTradeId, cleanAccount])

  // ── CFD discovery ─────────────────────────────────────────────────
  const [searchSymbol, setSearchSymbol] = useState('')
  const [searchExchange, setSearchExchange] = useState('SMART')
  const [searchCurrency, setSearchCurrency] = useState('USD')
  const [searchLoading, setSearchLoading] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  const [candidates, setCandidates] = useState<CfdCandidateContract[]>([])
  const [hasSearched, setHasSearched] = useState(false)
  const [selectedContract, setSelectedContract] = useState<CfdCandidateContract | null>(null)
  const [selectionError, setSelectionError] = useState<string | null>(null)

  // ── Order ticket ──────────────────────────────────────────────────
  const [side, setSide] = useState<'BUY' | 'SELL'>('BUY')
  const [orderType, setOrderType] = useState<'LIMIT' | 'MARKET'>('LIMIT')
  const [quantity, setQuantity] = useState('100')
  const [limitPrice, setLimitPrice] = useState('')
  const [tif, setTif] = useState('DAY')

  // ── Preview / submit ──────────────────────────────────────────────
  // Idempotency: one UUID per intentional order. New UUID only for new intent (contract/side/qty/type/price/tif change) or after successful submit.
  // Uses browser-compatible generator that works on HTTP (fallback to getRandomValues).
  const genIdemKey = genManualIdemKey
  const [idemKey, setIdemKey] = useState<string>(() => {
    try { return genIdemKey() } catch { return '' }
  })
  const [lastIntentSig, setLastIntentSig] = useState<string | null>(null)
  const currentIntentSig = `${selectedContract?.con_id ?? ''}|${selectedContract?.symbol ?? ''}|${side}|${quantity}|${orderType}|${limitPrice}|${tif}`
  const [isPreviewOpen, setIsPreviewOpen] = useState(false)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewData, setPreviewData] = useState<ManualOrderPreviewResponse | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [submitLoading, setSubmitLoading] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [submitResult, setSubmitResult] = useState<ManualOrderSubmitResponse | null>(null)

  // ── Manual positions live PnL via same demo stream as main Positions (not a different API) ──
  const activeLegs = usePnlStore((s) => s.active)
  const manualTrades = useMemo(() => {
    const filtered: Record<string, typeof activeLegs[string]> = {}
    const want = (cleanAccount || '').trim().toUpperCase()
    for (const [k, v] of Object.entries(activeLegs)) {
      if (String(v.source || '').toLowerCase() !== 'manual') continue
      if (want && String(v.ibkr_account || '').trim().toUpperCase() !== want) continue
      filtered[k] = v
    }
    return groupLegs(filtered)
  }, [activeLegs, cleanAccount])

  // ── Orders ────────────────────────────────────────────────────────
  const [orders, setOrders] = useState<ManualOrderRead[]>([])
  const [ordersLoading, setOrdersLoading] = useState(false)
  const [ordersError, setOrdersError] = useState<string | null>(null)
  const [cancellingOrderId, setCancellingOrderId] = useState<number | null>(null)
  const [cancelFeedback, setCancelFeedback] = useState<{ orderId: number; message: string; isError: boolean } | null>(null)
  const [cancelConfirm, setCancelConfirm] = useState<ManualOrderRead | null>(null)
  const [expandedOrderId, setExpandedOrderId] = useState<number | null>(null)

  const loadManualOrders = useCallback(async () => {
    if (!cleanAccount) return
    try {
      setOrdersLoading(true)
      setOrdersError(null)
      const res = await fetchManualOrders(cleanAccount)
      setOrders(res.orders)
    } catch (err: unknown) {
      const ae = err as { response?: { data?: { detail?: string } } }
      setOrdersError(ae?.response?.data?.detail || (err instanceof Error ? err.message : 'Failed to fetch orders'))
    } finally {
      setOrdersLoading(false)
    }
  }, [cleanAccount])

  // Initial load on mount / account change
  useEffect(() => {
    void loadManualOrders()
  }, [loadManualOrders])

  // State-driven polling: only while any order is non-terminal
  const hasActiveOrders = orders.some((o) => ["PENDING_SUBMIT", "SUBMITTED", "PARTIALLY_FILLED"].includes(o.status))
  useEffect(() => {
    if (!hasActiveOrders) return
    const t = setInterval(() => void loadManualOrders(), 5000)
    return () => clearInterval(t)
  }, [hasActiveOrders, loadManualOrders])

  const handleCancelOrder = async (order: ManualOrderRead) => {
    if (!cleanAccount || cancellingOrderId !== null) return
    setCancellingOrderId(order.id)
    setCancelConfirm(null)
    setCancelFeedback(null)
    try {
      const res = await cancelManualOrder(cleanAccount, order.id)
      setCancelFeedback({ orderId: order.id, message: res.message || 'Cancellation sent.', isError: !res.success })
      await loadManualOrders()
    } catch (err: unknown) {
      const ae = err as { response?: { data?: { detail?: string } } }
      setCancelFeedback({ orderId: order.id, message: ae?.response?.data?.detail || (err instanceof Error ? err.message : 'Cancel failed'), isError: true })
    } finally {
      setCancellingOrderId(null)
    }
  }

  const handleSearch = async (e?: React.FormEvent) => {
    if (e) e.preventDefault()
    const sym = searchSymbol.trim().toUpperCase()
    if (!sym) { setSearchError('Enter a symbol'); return }
    try {
      setSearchLoading(true); setSearchError(null); setCandidates([]); setHasSearched(true)
      const res = await searchCfdInstruments(cleanAccount, { symbol: sym, exchange: searchExchange.trim() || 'SMART', currency: searchCurrency.trim() || 'USD' })
      setCandidates(res.candidates)
      if (res.candidates.length === 0) setSearchError(res.message || `No CFD contracts for ${sym}`)
    } catch (err: unknown) {
      const ae = err as { response?: { data?: { detail?: string } } }
      setSearchError(ae?.response?.data?.detail || (err instanceof Error ? err.message : 'Discovery failed'))
    } finally { setSearchLoading(false) }
  }

  const handleSelectCandidate = (c: CfdCandidateContract) => {
    setSelectionError(null)
    if (c.sec_type !== 'CFD') { setSelectionError(`secType must be CFD (got ${c.sec_type})`); return }
    if (!c.con_id || c.con_id <= 0) { setSelectionError(`Invalid conId ${c.con_id}`); return }
    setSelectedContract(c); setSubmitResult(null); setSubmitError(null)
  }

  const handleClearContract = () => {
    setSelectedContract(null); setSelectionError(null); setPreviewData(null); setSubmitResult(null)
  }

  const handleOpenPreview = async () => {
    if (!selectedContract || !cleanAccount) return
    // New intent → new idempotency key. Same intent + reopen → reuse key for retry correlation.
    if (currentIntentSig !== lastIntentSig) {
      try {
        const newKey = genIdemKey()
        setIdemKey(newKey); setLastIntentSig(currentIntentSig)
      } catch (e) {
        setPreviewError((e as Error).message)
        return
      }
    } else if (!idemKey) {
      // No secure random at mount → try again
      try { setIdemKey(genIdemKey()) } catch (e) { setPreviewError((e as Error).message); return }
    }
    setIsPreviewOpen(true); setPreviewLoading(true); setPreviewError(null); setSubmitError(null)
    try {
      const data = await previewManualOrder(cleanAccount, {
        symbol: selectedContract.symbol, con_id: selectedContract.con_id, sec_type: 'CFD',
        exchange: selectedContract.exchange, currency: selectedContract.currency,
        side, quantity, order_type: orderType, limit_price: orderType === 'LIMIT' ? limitPrice : null,
        tif, outside_rth: false, min_tick: selectedContract.min_tick ?? null,
      })
      setPreviewData(data)
    } catch (err: unknown) {
      const ae = err as { response?: { data?: { detail?: string } } }
      setPreviewError(ae?.response?.data?.detail || (err instanceof Error ? err.message : 'Preview failed'))
    } finally { setPreviewLoading(false) }
  }

  const handleConfirmSubmit = async () => {
    if (!selectedContract || !cleanAccount || !previewData?.valid || submitLoading) return
    // Ensure we have a key — handle race where preview just rotated but state not yet flushed
    let submitKey = idemKey
    if (!submitKey || currentIntentSig !== lastIntentSig) {
      try { submitKey = genIdemKey(); setIdemKey(submitKey); setLastIntentSig(currentIntentSig) } catch (e) { setSubmitError((e as Error).message); return }
    }
    setSubmitLoading(true); setSubmitError(null)
    try {
      const res = await submitManualOrder(cleanAccount, {
        idempotency_key: submitKey, symbol: selectedContract.symbol, con_id: selectedContract.con_id, sec_type: 'CFD',
        exchange: selectedContract.exchange, currency: selectedContract.currency, side, quantity, order_type: orderType,
        limit_price: orderType === 'LIMIT' ? limitPrice : null, tif, outside_rth: false, min_tick: selectedContract.min_tick ?? null,
      })
      setSubmitResult(res); setIsPreviewOpen(false); void loadManualOrders()
      // Success → rotate key for next intentional order. Keep lastIntentSig so next preview with same intent still generates new key after this.
      try { setIdemKey(genIdemKey()); } catch { setIdemKey('') }
      setLastIntentSig(null)
      // Human-readable conflict handling: backend 409 detail already says conflict, surface it as generic.
      if (res.idempotent_replay) {
        // replay is success, no new key needed beyond rotation already done
      }
    } catch (err: unknown) {
      const ae = err as { response?: { data?: { detail?: string }; status?: number } }
      const detail = ae?.response?.data?.detail || (err instanceof Error ? err.message : 'Submit failed')
      // Hide internal key from operator
      if (detail.includes('MAN_IDEM_')) {
        setSubmitError('This order request has already been used. Please start a new order.')
      } else {
        setSubmitError(detail)
      }
    } finally { setSubmitLoading(false) }
  }

  const isFormComplete = selectedContract !== null && parseFloat(quantity) > 0 && (orderType === 'MARKET' || parseFloat(limitPrice) > 0)

  const gatewayDotClass = gatewayStatus?.connected ? 'ok' : gatewayError ? 'bad' : 'warn'
  const gatewayDotText = gatewayStatus?.connected ? 'Connected' : gatewayStatus ? 'Disconnected' : 'Unknown'
  const envBadge = (() => {
    if (!gatewayStatus) return null
    if (gatewayStatus.environment === 'VERIFIED_PAPER') return <span className="paper-pill">Paper</span>
    if (gatewayStatus.environment === 'VERIFIED_LIVE') return <span className="live-pill">Live</span>
    return <span style={{ color: 'var(--dim)', fontSize: 10, fontWeight: 600 }}>Unknown</span>
  })()

  const statusBadge = (s: string) => {
    if (s === 'FILLED') return <span className="badge b-filled">Filled</span>
    if (s === 'PARTIALLY_FILLED') return <span className="badge b-partial">Partial</span>
    if (s === 'CANCELLED') return <span className="badge b-closed">Cancelled</span>
    if (s === 'REJECTED' || s === 'ERROR') return <span className="badge b-rej">{s === 'ERROR' ? 'Error' : 'Rejected'}</span>
    if (s === 'PENDING_SUBMIT') return <span className="badge">Pending</span>
    return <span className="badge b-exec">Working</span>
  }

  return (
    <main className="manual-page">
      {/* Header */}
      <div className="manual-header">
        <div>
          <h1>Manual Trading</h1>
          <p>
            Account <strong style={{ color: 'var(--ink)', fontFamily: 'var(--mono)' }}>{cleanAccount || '—'}</strong>
            <span style={{ margin: '0 6px', color: 'var(--line)' }}>·</span>
            CFD · Discretionary · Isolated from engine
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <Link to={`/account/${cleanAccount}/manual-trade/positions`} className="manual-btn">Positions</Link>
          <button type="button" className="manual-btn" onClick={() => void loadGatewayStatus()} disabled={gatewayLoading}>
            {gatewayLoading ? 'Checking…' : 'Refresh'}
          </button>
        </div>
      </div>

      {isCloseMode && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, padding: '10px 12px', borderRadius: 6, background: 'var(--amber-bg, #2a2111)', border: '1px solid rgba(234,179,8,0.35)', color: 'var(--amber, #eab308)', fontSize: 12 }}>
          <span><strong>CLOSE MODE</strong> · Closing {closeSymbol} {closeSide} {closeQty} ({closeTradeId}) — verify quantity and submit as {closeSide} MARKET to flatten.</span>
          <button type="button" className="manual-btn" onClick={() => { setSearchParams({}, { replace: true }); setSelectedContract(null) }}>Exit Close Mode</button>
        </div>
      )}
      {/* Status bar */}
      <div className="manual-status-bar" role="status" aria-label="Connection status">
        <span className={`dot ${gatewayDotClass}`}><i />IBKR {gatewayDotText}</span>
        {gatewayStatus && <><span className="sep" />{envBadge}<span style={{ color: 'var(--dim)' }}>{gatewayStatus.host}:{gatewayStatus.port} · ID {gatewayStatus.client_id}</span></>}
        {gatewayStatus?.managed_accounts?.length ? <><span className="sep" /><span style={{ color: 'var(--muted)' }}>{gatewayStatus.managed_accounts.join(', ')}</span></> : null}
        <span className="sep" />
        <span style={{ color: 'var(--muted)' }}>{gatewayStatus?.message || gatewayError || '—'}</span>
      </div>
      {gatewayError && <div className="status-badge off" style={{ padding: '8px 12px', fontSize: 11 }}>{gatewayError}</div>}
      {submitResult && (
        <div style={{ padding: '10px 12px', borderRadius: 4, background: 'var(--green-bg)', border: '1px solid rgba(62,207,142,0.25)', color: 'var(--green)', fontSize: 11 }}>
          <strong>Order submitted</strong> · {submitResult.order.internal_order_id} · {submitResult.order.status} · Broker {submitResult.order.broker_order_id ?? '—'}
          <span style={{ marginLeft: 8, color: 'var(--muted)' }}>{submitResult.message}</span>
        </div>
      )}
      {submitError && <div style={{ padding: '10px 12px', borderRadius: 4, background: 'var(--red-bg)', border: '1px solid rgba(239,107,115,0.25)', color: 'var(--red)', fontSize: 11 }}>{submitError}</div>}
      {cancelFeedback && (
        <div style={{ padding: '8px 12px', borderRadius: 4, fontSize: 11, background: cancelFeedback.isError ? 'var(--red-bg)' : 'var(--green-bg)', border: `1px solid ${cancelFeedback.isError ? 'rgba(239,107,115,0.25)' : 'rgba(62,207,142,0.25)'}`, color: cancelFeedback.isError ? 'var(--red)' : 'var(--green)' }}>
          {cancelFeedback.message}
        </div>
      )}

      {/* Primary grid: Contract | Order | Risk */}
      <div className="manual-grid">
        {/* Contract */}
        <section className="manual-card" aria-label="Contract">
          <h3>Contract <span>· CFD discovery</span></h3>
          <form onSubmit={handleSearch} style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            <div className="manual-field">
              <label htmlFor="mt-sym">Symbol</label>
              <div style={{ display: 'flex', gap: 6 }}>
                <input id="mt-sym" value={searchSymbol} onChange={(e) => setSearchSymbol(e.target.value.toUpperCase())} placeholder="AAPL" aria-label="Symbol" />
                <button type="submit" className="manual-btn manual-btn-primary" disabled={searchLoading || !searchSymbol.trim()}>{searchLoading ? '…' : 'Search'}</button>
              </div>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
              <div className="manual-field"><label>Exchange</label><input value={searchExchange} onChange={(e) => setSearchExchange(e.target.value.toUpperCase())} placeholder="SMART" /></div>
              <div className="manual-field"><label>Currency</label><input value={searchCurrency} onChange={(e) => setSearchCurrency(e.target.value.toUpperCase())} placeholder="USD" /></div>
            </div>
          </form>

          {searchError && <div style={{ fontSize: 11, color: 'var(--red)', background: 'var(--red-bg)', border: '1px solid rgba(239,107,115,0.2)', padding: '6px 8px', borderRadius: 4 }}>{searchError}</div>}
          {selectionError && <div style={{ fontSize: 11, color: 'var(--red)', background: 'var(--red-bg)', border: '1px solid rgba(239,107,115,0.2)', padding: '6px 8px', borderRadius: 4 }}>{selectionError}</div>}

          {selectedContract ? (
            <div className="manual-contract-selected">
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <strong style={{ fontFamily: 'var(--mono)', fontSize: 13 }}>{selectedContract.symbol} <span style={{ color: 'var(--muted)', fontWeight: 400 }}>· {selectedContract.sec_type}</span></strong>
                <button type="button" className="manual-btn" onClick={handleClearContract}>Change</button>
              </div>
              <div style={{ fontSize: 11, color: 'var(--muted)', display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                <span>{selectedContract.exchange}</span><span>·</span><span>{selectedContract.currency}</span>
                {selectedContract.min_tick != null && <><span>·</span><span>tick {selectedContract.min_tick}</span></>}
              </div>
              <span style={{ fontSize: 10, color: 'var(--green)' }}>✓ IBKR contract resolved</span>
            </div>
          ) : (
            <div className="manual-contract-empty">Search and select a CFD contract to trade.</div>
          )}

          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 280, overflowY: 'auto' }}>
            {candidates.length > 0 && <div style={{ fontSize: 10, color: 'var(--dim)', fontWeight: 700, letterSpacing: '0.06em' }}>{candidates.length} candidate{candidates.length>1?'s':''} — select one</div>}
            {candidates.map((c) => {
              const sel = selectedContract?.con_id === c.con_id
              return (
                <div key={c.con_id} className={`manual-candidate ${sel ? 'is-selected' : ''}`}>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontFamily: 'var(--mono)', fontSize: 12, fontWeight: 700 }}>{c.symbol} <span style={{ color: 'var(--dim)', fontWeight: 400 }}>{c.con_id}</span></div>
                    <div style={{ fontSize: 10, color: 'var(--muted)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{c.exchange} · {c.currency}{c.long_name ? ` · ${c.long_name}` : ''}</div>
                  </div>
                  <button type="button" className={`manual-btn ${sel ? 'manual-btn-success' : ''}`} onClick={() => handleSelectCandidate(c)}>{sel ? 'Selected' : 'Select'}</button>
                </div>
              )
            })}
            {hasSearched && candidates.length === 0 && !searchLoading && !searchError && <div className="manual-contract-empty">No contracts.</div>}
          </div>
        </section>

        {/* Order ticket — primary workspace */}
        <section className="manual-card" aria-label="Order" style={{ borderColor: selectedContract ? 'rgba(110,168,255,0.2)' : 'var(--line)' }}>
          <h3>Order <span>· {side} {selectedContract ? selectedContract.symbol : ''}</span></h3>

          <div className="manual-side" role="group" aria-label="Side">
            <button type="button" className={side === 'BUY' ? 'on-buy' : ''} onClick={() => setSide('BUY')} aria-pressed={side === 'BUY'}>BUY</button>
            <button type="button" className={side === 'SELL' ? 'on-sell' : ''} onClick={() => setSide('SELL')} aria-pressed={side === 'SELL'}>SELL</button>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
            <div className="manual-field"><label>Quantity</label><input type="number" min={1} value={quantity} onChange={(e) => setQuantity(e.target.value)} aria-label="Quantity" /></div>
            <div className="manual-field">
              <label>Type</label>
              <select value={orderType} onChange={(e) => setOrderType(e.target.value as 'LIMIT' | 'MARKET')} aria-label="Order type">
                <option value="LIMIT">Limit</option>
                <option value="MARKET">Market</option>
              </select>
            </div>
          </div>

          {orderType === 'LIMIT' && (
            <div className="manual-field">
              <label>Limit price ({selectedContract?.currency || 'USD'})</label>
              <input type="number" step={selectedContract?.min_tick ? String(selectedContract.min_tick) : '0.01'} value={limitPrice} onChange={(e) => setLimitPrice(e.target.value)} placeholder="0.00" aria-label="Limit price" />
            </div>
          )}

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
            <div className="manual-field"><label>Time in force</label>
              <select value={tif} onChange={(e) => setTif(e.target.value)}><option value="DAY">DAY</option><option value="GTC">GTC</option><option value="IOC">IOC</option></select>
            </div>
            <div className="manual-field"><label>Outside RTH</label>
              <div style={{ padding: '7px 10px', background: '#0f141e', border: '1px solid var(--line)', borderRadius: 4, fontSize: 11, color: 'var(--dim)' }}>Off — enforced</div>
            </div>
          </div>

          <button type="button" className="manual-btn manual-btn-primary" style={{ width: '100%', padding: '10px', fontWeight: 700 }} onClick={() => void handleOpenPreview()} disabled={!isFormComplete}>
            Preview order
          </button>
          {!isFormComplete && <span style={{ fontSize: 10, color: 'var(--dim)', textAlign: 'center' }}>Select contract and enter quantity{orderType==='LIMIT' ? ' + limit price' : ''}</span>}
        </section>

        {/* Risk */}
        <section className="manual-card" aria-label="Risk">
          <h3>Risk <span>· preview</span></h3>
          {previewData ? (
            <div className="manual-risk-grid">
              <div className="manual-risk-row"><span>Notional</span><strong>{previewData.notional ? `${previewData.notional} ${previewData.currency}` : orderType==='MARKET' ? 'Market' : '—'}</strong></div>
              <div className="manual-risk-row"><span>Initial margin</span><strong>{previewData.init_margin_change ? `${previewData.init_margin_change} USD` : '—'}</strong></div>
              <div className="manual-risk-row"><span>Maintenance</span><strong>{previewData.maint_margin_change ? `${previewData.maint_margin_change} USD` : '—'}</strong></div>
              {previewData.margin_status === 'AVAILABLE' && <div style={{ fontSize: 10, color: 'var(--dim)', background: '#0b0e14', border: '1px solid var(--line)', borderRadius: 4, padding: '6px 8px' }}>Margin probe via IBKR what-if — check passed</div>}
              {previewData.margin_status === 'SKIPPED' && <div style={{ fontSize: 10, color: 'var(--muted)', background: '#0b0e14', border: '1px solid var(--line)', borderRadius: 4, padding: '6px 8px' }}>Margin check not run — disabled by configuration. Order permitted under existing safety policy.</div>}
              {previewData.margin_status === 'UNAVAILABLE' && <div style={{ fontSize: 10, color: 'var(--amber)', background: 'var(--amber-bg)', border: '1px solid rgba(224,179,76,0.2)', borderRadius: 4, padding: '6px 8px' }}>Margin check unavailable — order permitted under existing safety policy.</div>}
              {!previewData.valid && <div style={{ fontSize: 11, color: 'var(--red)', background: 'var(--red-bg)', border: '1px solid rgba(239,107,115,0.2)', padding: '6px 8px', borderRadius: 4 }}>{previewData.errors.join(' · ')}</div>}
              {previewData.warnings.length>0 && previewData.margin_status !== 'SKIPPED' && <div style={{ fontSize: 11, color: 'var(--amber)', background: 'var(--amber-bg)', border: '1px solid rgba(224,179,76,0.2)', padding: '6px 8px', borderRadius: 4 }}>{previewData.warnings.join(' · ')}</div>}
            </div>
          ) : (
            <div style={{ fontSize: 11, color: 'var(--dim)', textAlign: 'center', padding: '18px 8px', border: '1px dashed var(--line)', borderRadius: 4 }}>Run preview to see notional and margin.</div>
          )}
          {previewData?.valid && previewData.margin_status === 'AVAILABLE' && <div style={{ fontSize: 10, color: 'var(--green)' }}>✓ Safety gates passed</div>}
          {previewData?.valid && previewData.margin_status === 'SKIPPED' && <div style={{ fontSize: 10, color: 'var(--muted)' }}>✓ Safety gates passed · margin check not run</div>}
          {previewData?.valid && previewData.margin_status === 'UNAVAILABLE' && <div style={{ fontSize: 10, color: 'var(--amber)' }}>✓ Safety gates passed · margin check unavailable</div>}
        </section>
      </div>

      {/* Open orders */}
      <section className="board" style={{ overflow: 'hidden' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 12px', borderBottom: '1px solid var(--line)' }}>
          <h3 style={{ margin: 0, fontSize: 10, fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase', color: 'var(--dim)' }}>Open orders <span style={{ color: 'var(--muted)', fontWeight: 400 }}>{orders.length}</span></h3>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            {ordersLoading && <span style={{ fontSize: 11, color: 'var(--muted)' }}>Updating…</span>}
            <button type="button" className="manual-btn" onClick={() => void loadManualOrders()} disabled={ordersLoading}>Refresh</button>
          </div>
        </div>
        {ordersError && <div style={{ margin: 12, padding: '8px 12px', background: 'var(--red-bg)', color: 'var(--red)', fontSize: 11, borderRadius: 4 }}>{ordersError}</div>}
        {cancelFeedback && <div style={{ margin: '0 12px', padding: '6px 10px', fontSize: 11, borderRadius: 4, background: cancelFeedback.isError ? 'var(--red-bg)' : 'var(--green-bg)', color: cancelFeedback.isError ? 'var(--red)' : 'var(--green)' }}>{cancelFeedback.message}</div>}
        <div className="manual-table-wrap" style={{ border: 'none', borderRadius: 0 }}>
          <table className="manual-table">
            <thead>
              <tr>
                <th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Filled</th><th>Remaining</th><th>Price</th><th>Status</th><th>Action</th>
              </tr>
            </thead>
            <tbody>
              {orders.length === 0 ? (
                <tr><td colSpan={9} style={{ padding: 18, textAlign: 'center', color: 'var(--dim)', fontSize: 11 }}>No manual orders for {cleanAccount || '—'}. Isolated from engine.</td></tr>
              ) : orders.map((o) => {
                const isCancellable = o.status === 'SUBMITTED' || o.status === 'PARTIALLY_FILLED' || o.status === 'PENDING_SUBMIT'
                const filled = o.filled_quantity != null ? String(o.filled_quantity) : '0'
                const remaining = (() => { try { return String(Number(o.quantity) - Number(filled)); } catch { return '—' } })()
                const time = o.created_at ? new Date(o.created_at).toLocaleTimeString() : '—'
                const price = o.limit_price ? String(o.limit_price) : 'MKT'
                const expanded = expandedOrderId === o.id
                return (
                  <>
                    <tr key={o.id}>
                      <td style={{ color: 'var(--muted)', fontFamily: 'var(--mono)' }}>{time}</td>
                      <td style={{ fontWeight: 700, fontFamily: 'var(--mono)' }}>{o.symbol}</td>
                      <td><span className={o.side === 'BUY' ? 'side-buy' : 'side-sell'} style={{ fontWeight: 700 }}>{o.side}</span></td>
                      <td style={{ fontFamily: 'var(--mono)' }}>{String(o.quantity)}</td>
                      <td style={{ fontFamily: 'var(--mono)', color: Number(filled)>0 ? 'var(--ink)' : 'var(--dim)' }}>{filled}</td>
                      <td style={{ fontFamily: 'var(--mono)', color: 'var(--muted)' }}>{remaining}</td>
                      <td style={{ fontFamily: 'var(--mono)' }}>{price}</td>
                      <td>{statusBadge(o.status)}</td>
                      <td>
                        <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end', alignItems: 'center' }}>
                          <button type="button" className="manual-btn" style={{ padding: '3px 8px', fontSize: 10 }} onClick={() => setExpandedOrderId(expanded ? null : o.id)} aria-expanded={expanded}>{expanded ? 'Hide' : 'Details'}</button>
                          {isCancellable ? (
                            <button type="button" className="manual-btn manual-btn-danger" disabled={cancellingOrderId !== null} onClick={() => setCancelConfirm(o)} style={{ padding: '3px 8px', fontSize: 10 }}>
                              {cancellingOrderId === o.id ? '…' : 'Cancel'}
                            </button>
                          ) : <span style={{ fontSize: 10, color: 'var(--dim)' }}>—</span>}
                        </div>
                      </td>
                    </tr>
                    {expanded && (
                      <tr key={`${o.id}-details`}>
                        <td colSpan={9} style={{ background: '#0b0e14', padding: '10px 12px' }}>
                          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 10, fontSize: 11 }}>
                            <div><span style={{ color: 'var(--dim)' }}>Order</span><br /><span style={{ fontFamily: 'var(--mono)' }}>{o.internal_order_id}</span> <span style={{ color: 'var(--dim)' }}>· {o.trade_id}</span></div>
                            <div><span style={{ color: 'var(--dim)' }}>Broker</span><br /><span style={{ fontFamily: 'var(--mono)' }}>{o.broker_order_id ?? '—'}</span> <span style={{ color: 'var(--dim)' }}>· perm {o.perm_id ?? '—'}</span></div>
                            <div><span style={{ color: 'var(--dim)' }}>Contract</span><br /><span style={{ fontFamily: 'var(--mono)' }}>{o.con_id}</span> <span style={{ color: 'var(--muted)' }}>{o.exchange} · {o.currency}</span></div>
                            <div><span style={{ color: 'var(--dim)' }}>TIF</span><br /><span style={{ fontFamily: 'var(--mono)' }}>{o.tif}</span> · {o.order_type}</div>
                          </div>
                          {o.reject_reason && <div style={{ marginTop: 8, fontSize: 11, color: 'var(--red)' }}>{o.reject_reason}</div>}
                        </td>
                      </tr>
                    )}
                  </>
                )
              })}
            </tbody>
          </table>
        </div>
      </section>

      {/* Positions + Executions — manual positions use same demo stream/PnL as main Positions, not a different API */}
      <section className="board" style={{ padding: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
          <h3 style={{ margin: 0, fontSize: 10, fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase', color: 'var(--dim)' }}>Manual positions <span style={{ color: 'var(--muted)', fontWeight: 400 }}>· live PnL via demo stream</span></h3>
          <Link to={`/account/${cleanAccount}/manual-trade/positions`} style={{ fontSize: 11, color: 'var(--muted)', textDecoration: 'none' }}>Full ledger →</Link>
        </div>
        {manualTrades.size === 0 ? (
          <div style={{ fontSize: 11, color: 'var(--dim)', textAlign: 'center', padding: '14px', border: '1px dashed var(--line)', borderRadius: 4 }}>
            No open manual positions for {cleanAccount || '—'}. Isolated from engine.
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {Array.from(manualTrades.values()).map((legs) => {
              const head = legs[0]
              const qty = (head.quantity || head.filled_quantity || '0') as string
              const pnl = head.unrealized_pnl
              const pnlNum = pnl != null ? num(pnl) : null
              return (
                <div key={`${head.trade_id}-${head.symbol}`} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '8px 10px', background: '#0b0e14', border: '1px solid var(--line)', borderRadius: 4 }}>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    <span style={{ fontFamily: 'var(--mono)', fontWeight: 700, fontSize: 12 }}>{head.symbol}</span>
                    <span style={{ fontSize: 10, padding: '2px 6px', borderRadius: 3, background: '#3b2d54', color: '#d8b4fe', border: '1px solid #7c3aed', fontWeight: 600 }}>MANUAL</span>
                    <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--muted)' }}>{head.side} {qty}</span>
                  </div>
                  <div style={{ textAlign: 'right' }}>
                    <div style={{ fontFamily: 'var(--mono)', fontSize: 12, fontWeight: 600 }} className={pnlNum != null ? pnlClass(pnlNum) : ''}>
                      {pnlNum != null ? fmtPnl(pnlNum) : '—'}
                    </div>
                    <div style={{ fontSize: 10, color: 'var(--dim)' }}>{head.account_id} · {(head.trade_id || '').slice(0, 8)}</div>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </section>
      <section className="board" style={{ padding: 12 }}>
        <h3 style={{ margin: 0, fontSize: 10, fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase', color: 'var(--dim)', marginBottom: 8 }}>Executions</h3>
        <div style={{ fontSize: 11, color: 'var(--dim)', textAlign: 'center', padding: '14px', border: '1px dashed var(--line)', borderRadius: 4 }}>
          Fills ingested via IBKR callbacks — deduplicated on <span style={{ color: 'var(--muted)', fontFamily: 'var(--mono)' }}>exec_id</span>. Listed per order in Open orders → Details.
        </div>
      </section>



      {/* Preview modal */}
      {isPreviewOpen && (
        <div className="manual-modal-backdrop" onClick={() => !submitLoading && setIsPreviewOpen(false)} role="presentation">
          <div className="manual-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-labelledby="preview-title">
            <div className="manual-modal-head">
              <h2 id="preview-title">Confirm order</h2>
              <button type="button" className="manual-btn" onClick={() => setIsPreviewOpen(false)} disabled={submitLoading}>Close</button>
            </div>
            <div className="manual-modal-body">
              {previewLoading && <div style={{ padding: 20, textAlign: 'center', color: 'var(--muted)', fontSize: 12 }}>Checking safety gates and margin…</div>}
              {previewError && <div style={{ padding: '8px 12px', background: 'var(--red-bg)', border: '1px solid rgba(239,107,115,0.25)', color: 'var(--red)', fontSize: 11, borderRadius: 4 }}>{previewError}</div>}
              {previewData && !previewLoading && (
                <>
                  {!previewData.valid ? (
                    <div style={{ padding: '8px 12px', background: 'var(--red-bg)', border: '1px solid rgba(239,107,115,0.3)', color: 'var(--red)', fontSize: 11, borderRadius: 4 }}>
                      <strong style={{ display: 'block', marginBottom: 4 }}>Rejected by safety gates</strong>
                      <ul style={{ margin: 0, paddingLeft: 16 }}>{previewData.errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
                    </div>
                  ) : <div style={{ padding: '6px 10px', background: 'var(--green-bg)', border: '1px solid rgba(62,207,142,0.25)', color: 'var(--green)', fontSize: 11, borderRadius: 4 }}>Safety gates passed</div>}
                  {previewData.warnings.length>0 && <div style={{ padding: '6px 10px', background: 'var(--amber-bg)', border: '1px solid rgba(224,179,76,0.25)', color: 'var(--amber)', fontSize: 11, borderRadius: 4 }}>{previewData.warnings.join(' · ')}</div>}
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, fontSize: 12, background: '#0b0e14', border: '1px solid var(--line)', borderRadius: 4, padding: 12 }}>
                    <div><span style={{ color: 'var(--dim)' }}>Account</span><br /><strong style={{ fontFamily: 'var(--mono)' }}>{cleanAccount}</strong></div>
                    <div><span style={{ color: 'var(--dim)' }}>Contract</span><br /><strong>{previewData.symbol} · {previewData.exchange}</strong></div>
                    <div><span style={{ color: 'var(--dim)' }}>Side</span><br /><strong style={{ color: previewData.side==='BUY'?'var(--green)':'var(--red)' }}>{previewData.side}</strong> · {String(previewData.quantity)}</div>
                    <div><span style={{ color: 'var(--dim)' }}>Type</span><br /><strong>{previewData.order_type} {previewData.limit_price ? `@ ${previewData.limit_price}` : ''}</strong> · {previewData.con_id}</div>
                    <div style={{ gridColumn: 'span 2', borderTop: '1px solid var(--line)', paddingTop: 8, display: 'flex', justifyContent: 'space-between' }}>
                      <span style={{ color: 'var(--dim)' }}>Notional</span><strong style={{ fontFamily: 'var(--mono)' }}>{previewData.notional ? `${previewData.notional} ${previewData.currency}` : 'Market'}</strong>
                    </div>
                    {previewData.init_margin_change && <div style={{ gridColumn: 'span 2', display: 'flex', justifyContent: 'space-between' }}><span style={{ color: 'var(--dim)' }}>Initial margin</span><strong>{previewData.init_margin_change} USD</strong></div>}
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                    <button type="button" className="manual-btn" onClick={() => setIsPreviewOpen(false)} disabled={submitLoading}>Cancel</button>
                    <button type="button" className="manual-btn manual-btn-primary" onClick={() => void handleConfirmSubmit()} disabled={!previewData.valid || submitLoading} style={{ fontWeight: 700 }}>{submitLoading ? 'Submitting…' : 'Place order'}</button>
                  </div>
                  {submitError && <div style={{ fontSize: 11, color: 'var(--red)' }}>{submitError}</div>}
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Cancel confirm modal */}
      {cancelConfirm && (
        <div className="manual-modal-backdrop" onClick={() => setCancelConfirm(null)} role="presentation">
          <div className="manual-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-labelledby="cancel-title">
            <div className="manual-modal-head">
              <h2 id="cancel-title">Cancel order?</h2>
              <button type="button" className="manual-btn" onClick={() => setCancelConfirm(null)}>Close</button>
            </div>
            <div className="manual-modal-body">
              <div style={{ fontSize: 12 }}>
                <div><span style={{ color: 'var(--dim)' }}>Order</span> <strong style={{ fontFamily: 'var(--mono)' }}>{cancelConfirm.internal_order_id}</strong> · <span className={cancelConfirm.side==='BUY'?'side-buy':'side-sell'} style={{ fontWeight: 700 }}>{cancelConfirm.side}</span> {String(cancelConfirm.quantity)} {cancelConfirm.symbol}</div>
                <div style={{ marginTop: 6, fontSize: 11, color: 'var(--muted)' }}>Broker {cancelConfirm.broker_order_id ?? '—'} · Status {cancelConfirm.status} · Filled {String(cancelConfirm.filled_quantity ?? 0)} · Remaining {(() => { try { return String(Number(cancelConfirm.quantity)-Number(cancelConfirm.filled_quantity ?? 0)) } catch { return '—' } })()}</div>
              </div>
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                <button type="button" className="manual-btn" onClick={() => setCancelConfirm(null)}>Keep order</button>
                <button type="button" className="manual-btn manual-btn-danger" onClick={() => void handleCancelOrder(cancelConfirm)} disabled={cancellingOrderId!==null}>Cancel order</button>
              </div>
            </div>
          </div>
        </div>
      )}
    </main>
  )
}
