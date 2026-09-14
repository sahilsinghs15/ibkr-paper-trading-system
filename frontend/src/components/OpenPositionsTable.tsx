import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchReconcilePositions } from '../api/reconcileApi'
import { ClosePairModal } from './ClosePairModal'
import { ManualPositionDetailModal } from './ManualPositionDetailModal'
import { PairDetailModal } from './PairDetailModal'
import { SortableTh } from './SortableTh'
import { groupLegs, usePnlStore } from '../store/pnlStore'
import type { ClosePairResponse } from '../types/config'
import type { PositionLeg } from '../types/position'
import type { ReconcileDiffRow } from '../types/reconcile'
import {
  calcAgeDays,
  calcRMultiple,
  fmtCompactCurrency,
  fmtFactoryDate,
  fmtPnl,
  fmtQty,
  num,
  pnlClass,
  streamHint,
} from '../utils/format'
import { sortRows, useTableSortState } from '../utils/tableSort'

function getEpochMs(isoStr?: string | null): number {
  if (!isoStr) return 0
  const ms = new Date(isoStr).getTime()
  return isNaN(ms) ? 0 : ms
}

function computeTradeTotalNotional(legs: PositionLeg[]): number {
  const legA = legs[0]
  const legB = legs[1] || legs[0]
  const legAQty = Math.abs(num(legA.filled_quantity ?? legA.quantity) || 0)
  const legAPrice = num(legA.mark_price || legA.entry_price || legA.last_price) || 0
  const legANotional = legAQty * legAPrice
  const legBQty = Math.abs(num(legB.filled_quantity ?? legB.quantity) || 0)
  const legBPrice = num(legB.mark_price || legB.entry_price || legB.last_price) || 0
  const legBNotional = legBQty * legBPrice
  return legANotional + legBNotional || 1
}

const OPEN_POSITIONS_SORT_EXTRACTORS: Record<string, (legs: PositionLeg[]) => unknown> = {
  entry: (legs) => {
    const head = legs[0]
    return getEpochMs(head.opened_at || head.timestamp)
  },
  age: (legs) => {
    const head = legs[0]
    const ms = getEpochMs(head.opened_at || head.timestamp)
    return ms > 0 ? Date.now() - ms : null
  },
  pair: (legs) => {
    const legA = legs[0]
    const legB = legs[1] || legs[0]
    return `${legA.symbol || ''}/${legB.symbol || ''}`
  },
  exposure: (legs) => {
    return computeTradeTotalNotional(legs)
  },
  pl: (legs) => {
    const head = legs[0]
    return head.unrealized_pnl !== null && head.unrealized_pnl !== undefined
      ? num(head.unrealized_pnl)
      : null
  },
  progress: (legs) => {
    const head = legs[0]
    const notional = computeTradeTotalNotional(legs)
    return head.unrealized_pnl !== null && head.unrealized_pnl !== undefined
      ? (num(head.unrealized_pnl) || 0) / notional
      : null
  },
}

interface PairToInspect {
  accountId: number
  tradeId: string
  legs: PositionLeg[]
}

interface PairToClose {
  accountId: number
  ibkrAccount: string
  tradeId: string
  legASymbol: string
  legBSymbol?: string | null
}

export function OpenPositionsTable({ accountFilter }: { accountFilter?: string }) {
  const active = usePnlStore((s) => s.active)
  const streamState = usePnlStore((s) => s.streamState)
  const displayTz = usePnlStore((s) => s.displayTz)
  const cleanFilter = (accountFilter || '').trim().toUpperCase()
  const [historyOrder, setHistoryOrder] = useState<'RECENT' | 'OLDER'>('RECENT')
  const navigate = useNavigate()
  const [pairToClose, setPairToClose] = useState<PairToClose | null>(null)
  const [pairToInspect, setPairToInspect] = useState<PairToInspect | null>(null)
  const [manualToInspect, setManualToInspect] = useState<PairToInspect | null>(null)
  const [closeMessage, setCloseMessage] = useState<string | null>(null)
  const [reconcileDiffs, setReconcileDiffs] = useState<ReconcileDiffRow[]>([])
  const { sortKey, sortDir, handleSort } = useTableSortState()

  useEffect(() => {
    let mounted = true
    const poll = async () => {
      try {
        const res = await fetchReconcilePositions(cleanFilter || undefined)
        if (mounted && res?.diffs) {
          setReconcileDiffs(res.diffs)
        }
      } catch {
        // Suppress background poll errors
      }
    }
    void poll()
    const timer = setInterval(() => {
      void poll()
    }, 30000)
    return () => {
      mounted = false
      clearInterval(timer)
    }
  }, [cleanFilter])

  const getPairRogueTypes = useCallback(
    (legs: PositionLeg[]): string[] => {
      if (!legs.length || !reconcileDiffs.length) return []
      const head = legs[0]
      const pairAcc = (head.ibkr_account || cleanFilter || '').trim().toUpperCase()
      const pairSymbols = new Set(
        legs.map((l) => (l.symbol || '').trim().toUpperCase()).filter(Boolean)
      )

      const types = new Set<string>()
      for (const diff of reconcileDiffs) {
        if (!['QTY_DRIFT', 'LEDGER_GHOST', 'BROKER_ORPHAN'].includes(diff.kind)) {
          continue
        }
        const diffAcc = (diff.ibkr_account || '').trim().toUpperCase()
        if (diffAcc && pairAcc && diffAcc !== pairAcc) {
          continue
        }
        if (diff.account_id !== null && head.account_id !== undefined && head.account_id !== null) {
          if (Number(diff.account_id) !== Number(head.account_id)) {
            continue
          }
        }
        const diffSym = (diff.symbol || '').trim().toUpperCase()
        if (pairSymbols.has(diffSym)) {
          if (diff.kind === 'QTY_DRIFT') types.add('QTY DIFF')
          else if (diff.kind === 'LEDGER_GHOST') types.add('LEDGER GHOST')
          else if (diff.kind === 'BROKER_ORPHAN') types.add('BROKER GHOST')
        }
      }
      return Array.from(types)
    },
    [reconcileDiffs, cleanFilter],
  )

  const filteredActive = useMemo(() => {
    if (!cleanFilter) return active
    const out: typeof active = {}
    for (const [k, v] of Object.entries(active)) {
      if (String(v.ibkr_account || '').trim().toUpperCase() === cleanFilter) {
        out[k] = v
      }
    }
    return out
  }, [active, cleanFilter])

  const defaultSort = useCallback(
    (a: PositionLeg[], b: PositionLeg[]) => {
      const headA = a[0]
      const headB = b[0]
      const openTsA = getEpochMs(headA.opened_at || headA.timestamp)
      const openTsB = getEpochMs(headB.opened_at || headB.timestamp)

      if (historyOrder === 'RECENT') {
        return openTsB - openTsA // Newest open positions first (DESC)
      }
      return openTsA - openTsB // Older open positions chronologically (ASC)
    },
    [historyOrder],
  )

  const trades = useMemo(() => {
    const list = [...groupLegs(filteredActive).values()]
    return sortRows(list, sortKey, sortDir, OPEN_POSITIONS_SORT_EXTRACTORS, defaultSort)
  }, [filteredActive, sortKey, sortDir, defaultSort])

  return (
    <section className="factory-panel-section">
      <div className="section-h factory-h">
        <div className="factory-title-block">
          <h2>FACTORY PANEL — OPEN POSITIONS ({trades.length})</h2>
          <span className="factory-subtitle">MODEL BLUE X-SERIES · V1.1</span>
        </div>

        <div className="header-controls-group">
          {/* Recent vs Older Filter Toggle */}
          <div className="history-filter-toggle">
            <button
              type="button"
              className={`history-filter-btn ${historyOrder === 'RECENT' ? 'active' : ''}`}
              onClick={() => setHistoryOrder('RECENT')}
              title="Show newest open positions first (Entry Time DESC)"
            >
              RECENT (NEWEST FIRST)
            </button>
            <button
              type="button"
              className={`history-filter-btn ${historyOrder === 'OLDER' ? 'active' : ''}`}
              onClick={() => setHistoryOrder('OLDER')}
              title="Show older open positions chronologically (Entry Time ASC)"
            >
              OLDER (CHRONOLOGICAL)
            </button>
          </div>
          <span className="muted">{streamHint(streamState)}</span>
        </div>
      </div>

      {closeMessage ? (
        <p className="settings-msg ok" style={{ margin: '8px 16px' }}>
          {closeMessage}
        </p>
      ) : null}

      <div className="board factory-board scrollable-table-container">
        <table className="factory-table">
          <thead>
            <tr>
              <th style={{ width: '4%' }}>SNO</th>
              <SortableTh sortKey="entry" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort} style={{ width: '12%' }}>ENTRY</SortableTh>
              <SortableTh sortKey="age" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort} style={{ width: '6%' }}>AGE</SortableTh>
              <SortableTh sortKey="pair" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort} style={{ width: '11%' }}>PAIR</SortableTh>
              <SortableTh sortKey="exposure" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort} style={{ width: '38%' }}>EXPOSURE BALANCE</SortableTh>
              <SortableTh sortKey="pl" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort} align="right" style={{ width: '10%', textAlign: 'right' }}>PL</SortableTh>
              <SortableTh sortKey="progress" currentSortKey={sortKey} currentSortDir={sortDir} onSort={handleSort} align="right" style={{ width: '10%', textAlign: 'right' }}>PROGRESS</SortableTh>
              <th style={{ width: '9%', textAlign: 'right' }}>ACTION</th>
            </tr>
          </thead>
          <tbody>
            {trades.length === 0 ? (
              <tr>
                <td colSpan={8} className="empty">
                  No active positions open.
                </td>
              </tr>
            ) : (
              trades.map((legs, idx) => {
                const head = legs[0]
                const legA = legs[0]
                const legB = legs[1] || legs[0]

                const legAQty = Math.abs(num(legA.filled_quantity ?? legA.quantity) || 0)
                const legAPrice = num(legA.mark_price || legA.entry_price || legA.last_price) || 0
                const legANotional = legAQty * legAPrice

                const legBQty = Math.abs(num(legB.filled_quantity ?? legB.quantity) || 0)
                const legBPrice = num(legB.mark_price || legB.entry_price || legB.last_price) || 0
                const legBNotional = legBQty * legBPrice

                const totalNotional = legANotional + legBNotional || 1
                const legAPct = Math.round((legANotional / totalNotional) * 100)
                const legBPct = Math.round((legBNotional / totalNotional) * 100)
                const imbalance = Math.abs(legBPct - legAPct)
                const imbalanceSide = legBPct >= legAPct ? 'short' : 'long'
                const imbalanceText = `+${imbalance}% more ${imbalanceSide}`

                const openTsRaw = head.opened_at || head.timestamp
                const age = calcAgeDays(openTsRaw)
                const entryStr = openTsRaw ? fmtFactoryDate(openTsRaw, displayTz) : '—'
                const r = calcRMultiple(head.unrealized_pnl, totalNotional)
                const tk = `${head.account_id}|${head.trade_id}`
                const rowSno = idx + 1

                const isManual = String(head.source || '').toLowerCase() === 'manual'
                const openDetail = () => {
                  if (head.account_id === undefined || head.account_id === null || !head.trade_id) return
                  if (isManual) {
                    setManualToInspect({
                      accountId: Number(head.account_id),
                      tradeId: head.trade_id,
                      legs,
                    })
                  } else {
                    setPairToInspect({
                      accountId: Number(head.account_id),
                      tradeId: head.trade_id,
                      legs,
                    })
                  }
                }

                const rogueTypes = getPairRogueTypes(legs)
                const isRogue = rogueTypes.length > 0

                return (
                  <tr
                    key={tk}
                    className={`factory-row factory-row-clickable ${isRogue ? 'factory-row-rogue' : ''}`}
                    role="button"
                    tabIndex={0}
                    onClick={openDetail}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        openDetail()
                      }
                    }}
                  >
                    {/* 1. SNO */}
                    <td className="mono dim sno">{rowSno}</td>

                    {/* 2. ENTRY */}
                    <td className="mono entry-cell">{entryStr}</td>

                    {/* 3. AGE */}
                    <td className="age-cell">
                      <div className="age-wrapper">
                        <div className="age-bars">
                          {[0, 1, 2, 3, 4].map((i) => (
                            <span
                              key={i}
                              className={`age-bar ${i <= age.days ? 'active' : ''}`}
                            />
                          ))}
                        </div>
                        <span className="mono age-txt">{age.text}</span>
                      </div>
                    </td>

                    {/* 4. PAIR — single-leg manual shows single badge */}
                    <td className="pair-cell">
                      <div className="pair-badges">
                        {head.source === 'manual' && legs.length === 1 ? (
                          <span className="badge-pair leg-a">{legA.symbol || '—'}</span>
                        ) : (
                          <>
                            <span className="badge-pair leg-a">{legA.symbol || '—'}</span>
                            <span className="badge-pair leg-b">{legB.symbol || '—'}</span>
                          </>
                        )}
                        {head.source === 'manual' ? (
                          <span
                            className="source-badge manual"
                            style={{
                              fontSize: '10px',
                              padding: '2px 6px',
                              borderRadius: '3px',
                              background: '#3b2d54',
                              color: '#d8b4fe',
                              border: '1px solid #7c3aed',
                              marginLeft: '6px',
                              fontWeight: 600,
                            }}
                          >
                            Manual
                          </span>
                        ) : (
                          <span
                            className="source-badge engine"
                            style={{
                              fontSize: '10px',
                              padding: '2px 6px',
                              borderRadius: '3px',
                              background: '#1e293b',
                              color: '#94a3b8',
                              border: '1px solid #334155',
                              marginLeft: '6px',
                              fontWeight: 500,
                            }}
                          >
                            Engine
                          </span>
                        )}
                      </div>
                      {isRogue && (
                        <div className="rogue-badges-box">
                          {rogueTypes.map((t) => {
                            const badgeCls =
                              t === 'QTY DIFF'
                                ? 'qty-diff'
                                : t === 'LEDGER GHOST'
                                ? 'ledger-ghost'
                                : 'broker-ghost'
                            return (
                              <span
                                key={t}
                                className={`rogue-badge ${badgeCls}`}
                                title={`Rogue trade condition: ${t}`}
                              >
                                {t}
                              </span>
                            )
                          })}
                        </div>
                      )}
                    </td>

                    {/* 5. EXPOSURE BALANCE — manual single-leg shows one line, no imbalance */}
                    <td className="exposure-cell">
                      {head.source === 'manual' && legs.length === 1 ? (
                        <div className="exposure-box">
                          <div className="exp-legs">
                            <div className="exp-leg leg-a">
                              <span className="sym">{legA.symbol}</span>
                              <div className="track">
                                <div className="fill" style={{ width: '100%' }} />
                              </div>
                              <span className="val">
                                {fmtQty(legAQty)} / {fmtCompactCurrency(legANotional)}
                              </span>
                            </div>
                          </div>
                          <span className="badge" style={{ fontSize: '9px', background: '#3b2d54', color: '#d8b4fe', border: '1px solid #7c3aed' }}>
                            {legAQty > 0 ? 'LONG' : 'SHORT'} {fmtQty(Math.abs(legAQty))}
                          </span>
                        </div>
                      ) : (
                        <div className="exposure-box">
                          <div className="exp-legs">
                            <div className="exp-leg leg-a">
                              <span className="sym">{legA.symbol}</span>
                              <div className="track">
                                <div className="fill" style={{ width: `${Math.max(15, legAPct)}%` }} />
                              </div>
                              <span className="val">
                                {fmtQty(legAQty)} / {fmtCompactCurrency(legANotional)}
                              </span>
                            </div>
                            <div className="exp-leg leg-b">
                              <span className="sym">{legB.symbol}</span>
                              <div className="track">
                                <div className="fill" style={{ width: `${Math.max(15, legBPct)}%` }} />
                              </div>
                              <span className="val">
                                {fmtQty(legBQty)} / {fmtCompactCurrency(legBNotional)}
                              </span>
                            </div>
                          </div>
                          <span className="imbalance-pill">{imbalanceText}</span>
                        </div>
                      )}
                    </td>

                    {/* 6. PL */}
                    <td className={`right pl-cell ${head.unrealized_pnl !== null && head.unrealized_pnl !== undefined ? pnlClass(head.unrealized_pnl) : 'dim-txt'}`}>
                      {head.unrealized_pnl !== null && head.unrealized_pnl !== undefined
                        && head.market_data_status !== 'UNAVAILABLE'
                        ? fmtPnl(head.unrealized_pnl)
                        : head.market_data_status === 'NO_LIVE_ENTITLEMENT_API_SUBSCRIPTION_REQUIRED' || head.market_data_status === 'NO_LIVE_ENTITLEMENT' || head.market_data_status === '10089'
                        ? 'ENTITLEMENT REQUIRED'
                        : head.market_data_status === 'UNRESOLVED_CONTRACT_SPEC' || head.market_data_status === 'CONTRACT_UNRESOLVED' || head.market_data_status === '200'
                        ? 'CONTRACT UNRESOLVED'
                        : head.market_data_status === 'STALE_TICK'
                        ? 'STALE DATA'
                        : head.market_data_status === 'SUBSCRIPTION_ERROR'
                        ? 'MARKET DATA ERROR'
                        : head.market_data_status === 'DELAYED' || head.market_data_status === 'DELAYED_ONLY' || head.market_data_status === 'NO_LIVE_ENTITLEMENT_DELAYED' || head.market_data_status === 'DELAYED_FALLBACK'
                        ? 'DELAYED'
                        : 'NO MARK'}
                    </td>

                    {/* 7. PROGRESS */}
                    <td className="right progress-cell">
                      <div className="r-pill-box">
                        <span className={`r-pill ${r.isPos ? 'pos' : 'neg'}`}>{r.text}</span>
                      </div>
                    </td>

                    {/* 8. ACTION — manual uses Manual Trading, not Close Pair */}
                    <td className="right action-cell">
                      {String(head.source || '').toLowerCase() === 'manual' ? (
                        <button
                          type="button"
                          className="btn"
                          style={{ padding: '4px 8px', fontSize: '10px', fontWeight: 600, background: '#3b2d54', color: '#d8b4fe', border: '1px solid #7c3aed' }}
                          onClick={(e) => {
                            e.stopPropagation()
                            if (!head.trade_id || head.account_id == null) return
                            const qty = Math.abs(num(head.quantity ?? (head as unknown as { filled_quantity?: unknown }).filled_quantity) || 0) || Math.abs(num(legA.quantity) || 0)
                            const side = String(head.side || legA.side || '').toUpperCase() === 'BUY' ? 'SELL' : 'BUY'
                            const sym = head.symbol || legA.symbol || ''
                            const ibkr = head.ibkr_account || cleanFilter || ''
                            // Navigate to Manual Trading with close prefill (symbol/side/qty)
                            navigate(`/account/${ibkr}/manual-trade?close_symbol=${encodeURIComponent(sym)}&close_side=${side}&close_qty=${qty}&close_trade_id=${encodeURIComponent(String(head.trade_id))}`)
                          }}
                        >
                          Close
                        </button>
                      ) : (
                        <button
                          type="button"
                          className="btn danger"
                          style={{ padding: '4px 8px', fontSize: '10px', fontWeight: 600 }}
                          onClick={(e) => {
                            e.stopPropagation()
                            if (head.account_id === undefined || head.account_id === null || !head.trade_id) return
                            setPairToClose({
                              accountId: Number(head.account_id),
                              ibkrAccount: head.ibkr_account || cleanFilter || 'Unknown',
                              tradeId: head.trade_id,
                              legASymbol: legA.symbol || '—',
                              legBSymbol: legs.length > 1 ? legB.symbol : null,
                            })
                          }}
                        >
                          Close Pair
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })
            )}
          </tbody>
        </table>
      </div>

      {pairToClose ? (
        <ClosePairModal
          isOpen={!!pairToClose}
          accountId={pairToClose.accountId}
          ibkrAccount={pairToClose.ibkrAccount}
          tradeId={pairToClose.tradeId}
          legASymbol={pairToClose.legASymbol}
          legBSymbol={pairToClose.legBSymbol}
          onClose={() => setPairToClose(null)}
          onSuccess={(res: ClosePairResponse) => {
            if (res.success) {
              setCloseMessage(`Pair ${res.trade_id} closed successfully.`)
            } else {
              setCloseMessage(`Pair ${res.trade_id} close status: ${res.status}. ${res.message || ''}`)
            }
          }}
        />
      ) : null}
      {pairToInspect ? (
        <PairDetailModal
          isOpen={!!pairToInspect}
          accountId={pairToInspect.accountId}
          tradeId={pairToInspect.tradeId}
          legs={pairToInspect.legs}
          onClose={() => setPairToInspect(null)}
        />
      ) : null}
      {manualToInspect ? (
        <ManualPositionDetailModal
          isOpen={!!manualToInspect}
          accountId={manualToInspect.accountId}
          tradeId={manualToInspect.tradeId}
          legs={manualToInspect.legs}
          onClose={() => setManualToInspect(null)}
          onCloseViaManual={(tradeId, symbol, qty, side) => {
            const head = manualToInspect.legs[0]
            const ibkr = head?.ibkr_account || cleanFilter || ''
            navigate(`/account/${ibkr}/manual-trade?close_symbol=${encodeURIComponent(symbol)}&close_side=${side}&close_qty=${qty}&close_trade_id=${encodeURIComponent(tradeId)}`)
          }}
        />
      ) : null}
    </section>
  )
}

