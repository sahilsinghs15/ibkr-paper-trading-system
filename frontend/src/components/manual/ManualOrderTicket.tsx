import { forwardRef, useEffect, useState } from 'react'
import {
  fetchManualPositions,
  previewManualOrder,
  searchCfdInstruments,
  submitManualOrder,
  type CfdCandidateContract,
  type ManualOrderPreviewResponse,
  type ManualOrderSubmitResponse,
  type ManualPositionApiRow,
} from '../../api/manualTradingApi'
import { genManualIdemKey } from '../../utils/manualIdempotency'
import { apiErrorMessage } from './useManualWorkspace'

export interface CloseIntent {
  symbol: string
  side: 'BUY' | 'SELL'
  qty: string
  tradeId: string | null
}

interface Props {
  account: string
  closeIntent: CloseIntent | null
  positions: ManualPositionApiRow[]
  onExitClose: () => void
  onSubmitted: (res: ManualOrderSubmitResponse) => void
}

function contractFromPosition(pos: ManualPositionApiRow): CfdCandidateContract {
  return {
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
  }
}

/**
 * Manual order entry: CFD discovery → order → risk preview → confirm.
 * Submission goes only through the authoritative, audited backend endpoint
 * (POST /api/v1/manual/orders) after a server-side preview.
 */
export const ManualOrderTicket = forwardRef<HTMLDivElement, Props>(function ManualOrderTicket(
  { account, closeIntent, positions, onExitClose, onSubmitted },
  ref,
) {
  const isCloseMode = closeIntent !== null
  const closeTradeId = closeIntent?.tradeId ?? null

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

  // ── Order ─────────────────────────────────────────────────────────
  const [side, setSide] = useState<'BUY' | 'SELL'>('BUY')
  const [orderType, setOrderType] = useState<'LIMIT' | 'MARKET'>('LIMIT')
  const [quantity, setQuantity] = useState('100')
  const [limitPrice, setLimitPrice] = useState('')
  const [tif, setTif] = useState('DAY')

  // ── Close-mode prefill (from a position's Close action) ───────────
  useEffect(() => {
    if (!closeIntent) return
    setSearchSymbol(closeIntent.symbol.toUpperCase())
    setSide(closeIntent.side)
    setQuantity(closeIntent.qty)
    setOrderType('MARKET')
    setLimitPrice('')
    if (!closeIntent.tradeId || !account) return
    const known = positions.find((p) => p.trade_id === closeIntent.tradeId)
    if (known) {
      setSelectedContract(contractFromPosition(known))
      return
    }
    // Deep link before positions loaded: resolve the contract from the ledger.
    fetchManualPositions(account)
      .then((res) => {
        const pos = res.positions.find((p) => p.trade_id === closeIntent.tradeId)
        if (pos) setSelectedContract(contractFromPosition(pos))
      })
      .catch(() => {})
    // positions intentionally omitted: prefill once per close intent.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [closeIntent, account])

  // ── Preview / submit ──────────────────────────────────────────────
  // Idempotency: one key per intentional order; rotate on new intent or after a successful submit.
  const [idemKey, setIdemKey] = useState<string>(() => {
    try {
      return genManualIdemKey()
    } catch {
      return ''
    }
  })
  const [lastIntentSig, setLastIntentSig] = useState<string | null>(null)
  const currentIntentSig = `${selectedContract?.con_id ?? ''}|${selectedContract?.symbol ?? ''}|${side}|${quantity}|${orderType}|${limitPrice}|${tif}|${closeTradeId ?? ''}`
  const [isPreviewOpen, setIsPreviewOpen] = useState(false)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewData, setPreviewData] = useState<ManualOrderPreviewResponse | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [submitLoading, setSubmitLoading] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [submitResult, setSubmitResult] = useState<ManualOrderSubmitResponse | null>(null)

  const handleSearch = async (e?: React.FormEvent) => {
    if (e) e.preventDefault()
    const sym = searchSymbol.trim().toUpperCase()
    if (!sym) {
      setSearchError('Enter a symbol')
      return
    }
    try {
      setSearchLoading(true)
      setSearchError(null)
      setCandidates([])
      setHasSearched(true)
      const res = await searchCfdInstruments(account, {
        symbol: sym,
        exchange: searchExchange.trim() || 'SMART',
        currency: searchCurrency.trim() || 'USD',
      })
      setCandidates(res.candidates)
      if (res.candidates.length === 0) setSearchError(res.message || `No CFD contracts for ${sym}`)
    } catch (err: unknown) {
      setSearchError(apiErrorMessage(err, 'Discovery failed'))
    } finally {
      setSearchLoading(false)
    }
  }

  const handleSelectCandidate = (c: CfdCandidateContract) => {
    setSelectionError(null)
    if (c.sec_type !== 'CFD') {
      setSelectionError(`secType must be CFD (got ${c.sec_type})`)
      return
    }
    if (!c.con_id || c.con_id <= 0) {
      setSelectionError(`Invalid conId ${c.con_id}`)
      return
    }
    setSelectedContract(c)
    setSubmitResult(null)
    setSubmitError(null)
  }

  const handleClearContract = () => {
    setSelectedContract(null)
    setSelectionError(null)
    setPreviewData(null)
    setSubmitResult(null)
  }

  const handleOpenPreview = async () => {
    if (!selectedContract || !account) return
    // New intent → new idempotency key. Same intent + reopen → reuse key for retry correlation.
    if (currentIntentSig !== lastIntentSig) {
      try {
        setIdemKey(genManualIdemKey())
        setLastIntentSig(currentIntentSig)
      } catch (e) {
        setPreviewError((e as Error).message)
        return
      }
    } else if (!idemKey) {
      try {
        setIdemKey(genManualIdemKey())
      } catch (e) {
        setPreviewError((e as Error).message)
        return
      }
    }
    setIsPreviewOpen(true)
    setPreviewLoading(true)
    setPreviewError(null)
    setSubmitError(null)
    try {
      const data = await previewManualOrder(account, {
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
        trade_id: isCloseMode && closeTradeId ? closeTradeId : null,
      })
      setPreviewData(data)
    } catch (err: unknown) {
      setPreviewError(apiErrorMessage(err, 'Preview failed'))
    } finally {
      setPreviewLoading(false)
    }
  }

  const handleConfirmSubmit = async () => {
    if (!selectedContract || !account || !previewData?.valid || submitLoading) return
    // Ensure we have a key — handle race where preview just rotated but state not yet flushed.
    let submitKey = idemKey
    if (!submitKey || currentIntentSig !== lastIntentSig) {
      try {
        submitKey = genManualIdemKey()
        setIdemKey(submitKey)
        setLastIntentSig(currentIntentSig)
      } catch (e) {
        setSubmitError((e as Error).message)
        return
      }
    }
    setSubmitLoading(true)
    setSubmitError(null)
    try {
      const res = await submitManualOrder(account, {
        idempotency_key: submitKey,
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
        trade_id: isCloseMode && closeTradeId ? closeTradeId : null,
      })
      setSubmitResult(res)
      setIsPreviewOpen(false)
      onSubmitted(res)
      // Success → rotate key for the next intentional order.
      try {
        setIdemKey(genManualIdemKey())
      } catch {
        setIdemKey('')
      }
      setLastIntentSig(null)
    } catch (err: unknown) {
      const detail = apiErrorMessage(err, 'Submit failed')
      // Hide internal key from operator.
      setSubmitError(
        detail.includes('MAN_IDEM_') ? 'This order request has already been used. Please start a new order.' : detail,
      )
    } finally {
      setSubmitLoading(false)
    }
  }

  const isFormComplete =
    selectedContract !== null && parseFloat(quantity) > 0 && (orderType === 'MARKET' || parseFloat(limitPrice) > 0)

  return (
    <div className="mw-subsection" ref={ref} id="manual-ticket" aria-labelledby="mw-ticket-title">
      <div className="mw-subsection-head">
        <h3 id="mw-ticket-title">New order</h3>
        <span className="mw-hint">CFD · server-side preview and safety gates before placement</span>
      </div>

      {isCloseMode && closeIntent && (
        <div className="mw-note warn mw-close-banner">
          <span>
            <strong>CLOSE MODE</strong> · Closing {closeIntent.symbol} with {closeIntent.side} {closeIntent.qty}
            {closeTradeId ? ` (${closeTradeId})` : ''} — verify quantity and submit as {closeIntent.side} MARKET to flatten.
          </span>
          <button
            type="button"
            className="manual-btn"
            onClick={() => {
              onExitClose()
              setSelectedContract(null)
            }}
          >
            Exit close mode
          </button>
        </div>
      )}
      {submitResult && (
        <div className="mw-note ok">
          <strong>Order submitted</strong> · {submitResult.order.internal_order_id} · {submitResult.order.status} · Broker{' '}
          {submitResult.order.broker_order_id ?? '—'}
          <span className="dim"> {submitResult.message}</span>
        </div>
      )}
      {submitError && !isPreviewOpen && <div className="mw-note error">{submitError}</div>}

      <div className="manual-grid">
        {/* Contract */}
        <section className="manual-card" aria-label="Contract">
          <h3>
            Contract <span>· CFD discovery</span>
          </h3>
          <form onSubmit={handleSearch} style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            <div className="manual-field">
              <label htmlFor="mt-sym">Symbol</label>
              <div style={{ display: 'flex', gap: 6 }}>
                <input
                  id="mt-sym"
                  value={searchSymbol}
                  onChange={(e) => setSearchSymbol(e.target.value.toUpperCase())}
                  placeholder="AAPL"
                  aria-label="Symbol"
                />
                <button
                  type="submit"
                  className="manual-btn manual-btn-primary"
                  disabled={searchLoading || !searchSymbol.trim()}
                >
                  {searchLoading ? '…' : 'Search'}
                </button>
              </div>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
              <div className="manual-field">
                <label>Exchange</label>
                <input value={searchExchange} onChange={(e) => setSearchExchange(e.target.value.toUpperCase())} placeholder="SMART" />
              </div>
              <div className="manual-field">
                <label>Currency</label>
                <input value={searchCurrency} onChange={(e) => setSearchCurrency(e.target.value.toUpperCase())} placeholder="USD" />
              </div>
            </div>
          </form>

          {searchError && <div className="mw-note error">{searchError}</div>}
          {selectionError && <div className="mw-note error">{selectionError}</div>}

          {selectedContract ? (
            <div className="manual-contract-selected">
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <strong style={{ fontFamily: 'var(--mono)', fontSize: 13 }}>
                  {selectedContract.symbol} <span style={{ color: 'var(--muted)', fontWeight: 400 }}>· {selectedContract.sec_type}</span>
                </strong>
                <button type="button" className="manual-btn" onClick={handleClearContract}>
                  Change
                </button>
              </div>
              <div style={{ fontSize: 11, color: 'var(--muted)', display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                <span>{selectedContract.exchange}</span>
                <span>·</span>
                <span>{selectedContract.currency}</span>
                <span>·</span>
                <span className="mono">conId {selectedContract.con_id}</span>
                {selectedContract.min_tick != null && (
                  <>
                    <span>·</span>
                    <span>tick {selectedContract.min_tick}</span>
                  </>
                )}
              </div>
              <span style={{ fontSize: 10, color: 'var(--green)' }}>✓ IBKR contract resolved</span>
            </div>
          ) : (
            <div className="manual-contract-empty">Search and select a CFD contract to trade.</div>
          )}

          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 240, overflowY: 'auto' }}>
            {candidates.length > 0 && (
              <div style={{ fontSize: 10, color: 'var(--dim)', fontWeight: 700, letterSpacing: '0.06em' }}>
                {candidates.length} candidate{candidates.length > 1 ? 's' : ''} — select one
              </div>
            )}
            {candidates.map((c) => {
              const sel = selectedContract?.con_id === c.con_id
              return (
                <div key={c.con_id} className={`manual-candidate ${sel ? 'is-selected' : ''}`}>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontFamily: 'var(--mono)', fontSize: 12, fontWeight: 700 }}>
                      {c.symbol} <span style={{ color: 'var(--dim)', fontWeight: 400 }}>{c.con_id}</span>
                    </div>
                    <div style={{ fontSize: 10, color: 'var(--muted)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {c.exchange} · {c.currency}
                      {c.long_name ? ` · ${c.long_name}` : ''}
                    </div>
                  </div>
                  <button
                    type="button"
                    className={`manual-btn ${sel ? 'manual-btn-success' : ''}`}
                    onClick={() => handleSelectCandidate(c)}
                  >
                    {sel ? 'Selected' : 'Select'}
                  </button>
                </div>
              )
            })}
            {hasSearched && candidates.length === 0 && !searchLoading && !searchError && (
              <div className="manual-contract-empty">No contracts.</div>
            )}
          </div>
        </section>

        {/* Order */}
        <section
          className="manual-card"
          aria-label="Order"
          style={{ borderColor: selectedContract ? 'rgba(110,168,255,0.2)' : 'var(--line)' }}
        >
          <h3>
            Order <span>· {side} {selectedContract ? selectedContract.symbol : ''}</span>
          </h3>
          <div className="manual-side" role="group" aria-label="Side">
            <button type="button" className={side === 'BUY' ? 'on-buy' : ''} onClick={() => setSide('BUY')} aria-pressed={side === 'BUY'}>
              BUY
            </button>
            <button type="button" className={side === 'SELL' ? 'on-sell' : ''} onClick={() => setSide('SELL')} aria-pressed={side === 'SELL'}>
              SELL
            </button>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
            <div className="manual-field">
              <label>Quantity</label>
              <input type="number" min={1} value={quantity} onChange={(e) => setQuantity(e.target.value)} aria-label="Quantity" />
            </div>
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
              <input
                type="number"
                step={selectedContract?.min_tick ? String(selectedContract.min_tick) : '0.01'}
                value={limitPrice}
                onChange={(e) => setLimitPrice(e.target.value)}
                placeholder="0.00"
                aria-label="Limit price"
              />
            </div>
          )}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
            <div className="manual-field">
              <label>Time in force</label>
              <select value={tif} onChange={(e) => setTif(e.target.value)}>
                <option value="DAY">DAY</option>
                <option value="GTC">GTC</option>
                <option value="IOC">IOC</option>
              </select>
            </div>
            <div className="manual-field">
              <label>Outside RTH</label>
              <div style={{ padding: '7px 10px', background: '#0f141e', border: '1px solid var(--line)', borderRadius: 4, fontSize: 11, color: 'var(--dim)' }}>
                Off — enforced
              </div>
            </div>
          </div>
          <button
            type="button"
            className="manual-btn manual-btn-primary"
            style={{ width: '100%', padding: '10px', fontWeight: 700 }}
            onClick={() => void handleOpenPreview()}
            disabled={!isFormComplete}
          >
            Preview order
          </button>
          {!isFormComplete && (
            <span style={{ fontSize: 10, color: 'var(--dim)', textAlign: 'center' }}>
              Select contract and enter quantity{orderType === 'LIMIT' ? ' + limit price' : ''}
            </span>
          )}
        </section>

        {/* Risk */}
        <section className="manual-card" aria-label="Risk">
          <h3>
            Risk <span>· preview</span>
          </h3>
          {previewData ? (
            <div className="manual-risk-grid">
              <div className="manual-risk-row">
                <span>Notional</span>
                <strong>{previewData.notional ? `${previewData.notional} ${previewData.currency}` : orderType === 'MARKET' ? 'Market' : '—'}</strong>
              </div>
              <div className="manual-risk-row">
                <span>Initial margin</span>
                <strong>{previewData.init_margin_change ? `${previewData.init_margin_change} USD` : '—'}</strong>
              </div>
              <div className="manual-risk-row">
                <span>Maintenance</span>
                <strong>{previewData.maint_margin_change ? `${previewData.maint_margin_change} USD` : '—'}</strong>
              </div>
              {previewData.margin_status === 'AVAILABLE' && <div className="mw-note">Margin probe via IBKR what-if — check passed</div>}
              {previewData.margin_status === 'SKIPPED' && (
                <div className="mw-note">Margin check not run — disabled by configuration. Order permitted under existing safety policy.</div>
              )}
              {previewData.margin_status === 'UNAVAILABLE' && (
                <div className="mw-note warn">Margin check unavailable — order permitted under existing safety policy.</div>
              )}
              {!previewData.valid && <div className="mw-note error">{previewData.errors.join(' · ')}</div>}
              {previewData.warnings.length > 0 && previewData.margin_status !== 'SKIPPED' && (
                <div className="mw-note warn">{previewData.warnings.join(' · ')}</div>
              )}
            </div>
          ) : (
            <div style={{ fontSize: 11, color: 'var(--dim)', textAlign: 'center', padding: '18px 8px', border: '1px dashed var(--line)', borderRadius: 4 }}>
              Run preview to see notional and margin.
            </div>
          )}
          {previewData?.valid && previewData.margin_status === 'AVAILABLE' && <div style={{ fontSize: 10, color: 'var(--green)' }}>✓ Safety gates passed</div>}
          {previewData?.valid && previewData.margin_status === 'SKIPPED' && (
            <div style={{ fontSize: 10, color: 'var(--muted)' }}>✓ Safety gates passed · margin check not run</div>
          )}
          {previewData?.valid && previewData.margin_status === 'UNAVAILABLE' && (
            <div style={{ fontSize: 10, color: 'var(--amber)' }}>✓ Safety gates passed · margin check unavailable</div>
          )}
        </section>
      </div>

      {isPreviewOpen && (
        <div className="manual-modal-backdrop" onClick={() => !submitLoading && setIsPreviewOpen(false)} role="presentation">
          <div className="manual-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-labelledby="preview-title">
            <div className="manual-modal-head">
              <h2 id="preview-title">Confirm order</h2>
              <button type="button" className="manual-btn" onClick={() => setIsPreviewOpen(false)} disabled={submitLoading}>
                Close
              </button>
            </div>
            <div className="manual-modal-body">
              {previewLoading && <div style={{ padding: 20, textAlign: 'center', color: 'var(--muted)', fontSize: 12 }}>Checking safety gates and margin…</div>}
              {previewError && <div className="mw-note error">{previewError}</div>}
              {previewData && !previewLoading && (
                <>
                  {!previewData.valid ? (
                    <div className="mw-note error">
                      <strong style={{ display: 'block', marginBottom: 4 }}>Rejected by safety gates</strong>
                      <ul style={{ margin: 0, paddingLeft: 16 }}>
                        {previewData.errors.map((e, i) => (
                          <li key={i}>{e}</li>
                        ))}
                      </ul>
                    </div>
                  ) : (
                    <div className="mw-note ok">Safety gates passed</div>
                  )}
                  {previewData.warnings.length > 0 && <div className="mw-note warn">{previewData.warnings.join(' · ')}</div>}
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, fontSize: 12, background: '#0b0e14', border: '1px solid var(--line)', borderRadius: 4, padding: 12 }}>
                    <div>
                      <span className="dim">Account</span>
                      <br />
                      <strong className="mono">{account}</strong>
                    </div>
                    <div>
                      <span className="dim">Contract</span>
                      <br />
                      <strong>
                        {previewData.symbol} · {previewData.exchange}
                      </strong>
                    </div>
                    <div>
                      <span className="dim">Side</span>
                      <br />
                      <strong style={{ color: previewData.side === 'BUY' ? 'var(--green)' : 'var(--red)' }}>{previewData.side}</strong> ·{' '}
                      {String(previewData.quantity)}
                    </div>
                    <div>
                      <span className="dim">Type</span>
                      <br />
                      <strong>
                        {previewData.order_type} {previewData.limit_price ? `@ ${previewData.limit_price}` : ''}
                      </strong>{' '}
                      · {previewData.con_id}
                    </div>
                    <div style={{ gridColumn: 'span 2', borderTop: '1px solid var(--line)', paddingTop: 8, display: 'flex', justifyContent: 'space-between' }}>
                      <span className="dim">Notional</span>
                      <strong className="mono">{previewData.notional ? `${previewData.notional} ${previewData.currency}` : 'Market'}</strong>
                    </div>
                    {previewData.init_margin_change && (
                      <div style={{ gridColumn: 'span 2', display: 'flex', justifyContent: 'space-between' }}>
                        <span className="dim">Initial margin</span>
                        <strong>{previewData.init_margin_change} USD</strong>
                      </div>
                    )}
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                    <button type="button" className="manual-btn" onClick={() => setIsPreviewOpen(false)} disabled={submitLoading}>
                      Cancel
                    </button>
                    <button
                      type="button"
                      className="manual-btn manual-btn-primary"
                      onClick={() => void handleConfirmSubmit()}
                      disabled={!previewData.valid || submitLoading}
                      style={{ fontWeight: 700 }}
                    >
                      {submitLoading ? 'Submitting…' : 'Place order'}
                    </button>
                  </div>
                  {submitError && <div className="mw-note error">{submitError}</div>}
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
})
