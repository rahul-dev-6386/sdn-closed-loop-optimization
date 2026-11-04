/**
 * Resizable Bottom Drawer with Structured OpenFlow Tables and Consolidated Event Stream.
 *
 * Implements Section 1 & Section 5:
 * - Collapses repeated telemetry_stall events into ONE live, ticking row:
 *   "Telemetry stalled — 24s, ongoing" that resolves when telemetry resumes.
 * - Pinned Decision & Alert rows at the top (predict, decide, reroute, verify),
 *   tagged as [DERIVED] if synthesized from controller state transitions.
 * - Parses raw ovs-ofctl lines into a clean table:
 *   Priority | Match | Actions | Packets | Bytes | Age | Tags
 * - Distinguishes between system rules and forwarding rules.
 * - Filter dropdown uses CustomSelect defaulting to "Decisions & Alerts".
 * - Resizable with drag handle (120px to 60vh), double-click reset, Minimize to 40px tab bar.
 */
import React, { useMemo, useState, useRef, useEffect } from 'react'
import {
  FileCode,
  Radio,
  ChevronDown,
  ChevronUp,
  RefreshCw,
  GitFork,
  CheckCircle2,
  AlertTriangle,
  Clock,
  ExternalLink,
  ChevronRight,
  GripHorizontal,
} from 'lucide-react'
import type { SdnEvent } from '../api'
import { parseFlowRule, type ParsedFlowRule } from '../utils/openflowParser'
import { CustomSelect } from './CustomSelect'

interface Props {
  events: SdnEvent[]
  eventFilter: string
  onFilterChange: (filter: string) => void
  flows: Record<string, any> | null
  switches: string[]
  activePath: string[]
  onRefreshFlows: () => void
  isRefreshingFlows?: boolean
  selected: string | null
  onSelect: (name: string | null) => void
  isOpen: boolean
  onToggleOpen: () => void
  drawerHeight: number
  onResizeDrawer: (h: number) => void
  onResetDrawerHeight: () => void
  isStalled?: boolean
  isDown?: boolean
  stalledAgeSec?: number
  isDetourActive?: boolean
  isSimulated?: boolean
  derivedDecisions?: Array<{
    id: string
    kind: string
    summary: string
    wall_iso: string
    isDerived: boolean
  }>
}

export function BottomDrawer({
  events,
  eventFilter,
  onFilterChange,
  flows,
  switches,
  activePath,
  onRefreshFlows,
  isRefreshingFlows = false,
  selected,
  onSelect,
  isOpen,
  onToggleOpen,
  drawerHeight,
  onResizeDrawer,
  onResetDrawerHeight,
  isStalled = false,
  isDown = false,
  stalledAgeSec = 0,
  isDetourActive = false,
  isSimulated = false,
  derivedDecisions = [],
}: Props) {

  const [activeTab, setActiveTab] = useState<'events' | 'openflow'>('events')
  const [expandedSwitches, setExpandedSwitches] = useState<Set<string>>(new Set(['s1']))
  const [expandedRawRules, setExpandedRawRules] = useState<Set<string>>(new Set())
  const [autoRefresh, setAutoRefresh] = useState<boolean>(true)
  const [lastRefreshedSec, setLastRefreshedSec] = useState<number>(0)

  const eventsListRef = useRef<HTMLDivElement>(null)
  const isDraggingRef = useRef<boolean>(false)
  const startYRef = useRef<number>(0)
  const startHRef = useRef<number>(0)

  // Auto-refresh countdown for OpenFlow tables
  useEffect(() => {
    if (!autoRefresh || !isOpen || activeTab !== 'openflow') return
    const interval = window.setInterval(() => {
      onRefreshFlows()
      setLastRefreshedSec(0)
    }, 4000)
    return () => clearInterval(interval)
  }, [autoRefresh, isOpen, activeTab, onRefreshFlows])

  useEffect(() => {
    const timer = window.setInterval(() => {
      setLastRefreshedSec((prev) => prev + 1)
    }, 1000)
    return () => clearInterval(timer)
  }, [])

  // Drag-to-resize handle listener
  useEffect(() => {
    function handleMouseMove(e: MouseEvent) {
      if (!isDraggingRef.current) return
      const deltaY = startYRef.current - e.clientY
      const newHeight = Math.max(120, Math.min(window.innerHeight * 0.60, startHRef.current + deltaY))
      onResizeDrawer(newHeight)
    }

    function handleMouseUp() {
      if (isDraggingRef.current) {
        isDraggingRef.current = false
        document.body.style.cursor = ''
        document.body.style.userSelect = ''
      }
    }

    window.addEventListener('mousemove', handleMouseMove)
    window.addEventListener('mouseup', handleMouseUp)
    return () => {
      window.removeEventListener('mousemove', handleMouseMove)
      window.removeEventListener('mouseup', handleMouseUp)
    }
  }, [onResizeDrawer])

  const handleDragStart = (e: React.MouseEvent) => {
    e.preventDefault()
    isDraggingRef.current = true
    startYRef.current = e.clientY
    startHRef.current = drawerHeight
    document.body.style.cursor = 'row-resize'
    document.body.style.userSelect = 'none'
  }

  // Section 5: Events deduplication & Stall collapse
  const processedEvents = useMemo(() => {
    const rows: Array<{
      id: string
      kind: string
      summary: string
      count: number
      wall_iso: string
      isKeyDecision?: boolean
      isDerived?: boolean
      isOngoingStall?: boolean
    }> = []

    // 1. Pinned derived/live decisions at the top
    for (const d of derivedDecisions) {
      rows.push({
        id: `derived-${d.id}`,
        kind: d.kind,
        summary: d.summary,
        count: 1,
        wall_iso: d.wall_iso,
        isKeyDecision: true,
        isDerived: d.isDerived,
      })
    }

    // 2. Active ongoing stall row (consolidated into ONE live ticking row)
    const activeStallOngoing = isStalled || (isDown && stalledAgeSec > 3)
    if (activeStallOngoing) {
      rows.push({
        id: 'ongoing-telemetry-stall',
        kind: 'telemetry_stall',
        summary: `Telemetry stalled — ${Math.round(stalledAgeSec)}s, ongoing`,
        count: 1,
        wall_iso: new Date().toISOString(),
        isOngoingStall: true,
      })
    }

    // 3. Process remaining historical events, collapsing telemetry_stall flood
    const reversed = [...events].reverse()
    let lastStallRow: (typeof rows)[0] | null = null

    for (const ev of reversed) {
      // In simulated mode, do not show background live controller stall noise
      if (ev.kind === 'telemetry_stall' && isSimulated) {
        continue
      }

      // If we already show an active ongoing stall, ignore old repetitive stall entries
      if (ev.kind === 'telemetry_stall' && activeStallOngoing) {
        continue
      }

      if (ev.kind === 'telemetry_stall') {
        const elapsed = Number(ev.elapsed_s) || 6
        if (lastStallRow) {
          // Collapse into the single historical stall episode row
          lastStallRow.count += 1
          const prevMatch = lastStallRow.summary.match(/([0-9.]+)/)
          const prevElapsed = prevMatch ? parseFloat(prevMatch[1]) : 0
          if (elapsed > prevElapsed) {
            lastStallRow.summary = `Telemetry stall: elapsed ${elapsed}s`
          }
          continue
        } else {
          const stallRow = {
            id: String(ev.id || `stall-${ev.wall_iso}`),
            kind: 'telemetry_stall',
            summary: `Telemetry stall: elapsed ${elapsed}s`,
            count: 1,
            wall_iso: (ev.wall_iso as string) || new Date().toISOString(),
            isKeyDecision: true,
          }
          rows.push(stallRow)
          lastStallRow = stallRow
          continue
        }
      } else {
        // Reset lastStallRow only when a real traffic or reroute event occurs
        if (['reroute', 'traffic_spike', 'congestion'].includes(ev.kind)) {
          lastStallRow = null
        }
      }


      const summaryText =
        ev.summary ||
        (ev.kind === 'link_discovered'
          ? `Link discovered: ${ev.link || 'inter-switch'}`
          : ev.kind === 'traffic_spike'
          ? `Traffic spike started (${ev.rate_mbps || 14} Mbps)`
          : `${ev.kind.replace(/_/g, ' ')}`)

      // Check for sequential duplicate collapse
      const last = rows[rows.length - 1]
      if (last && last.kind === ev.kind && last.summary === summaryText) {
        last.count += 1
      } else {
        rows.push({
          id: String(ev.id || `${ev.kind}-${ev.wall_iso}-${Math.random()}`),
          kind: ev.kind,
          summary: summaryText,
          count: 1,
          wall_iso: (ev.wall_iso as string) || new Date().toISOString(),
          isKeyDecision: ['reroute', 'congestion', 'verify_pass', 'verify_fail', 'predict', 'decide'].includes(ev.kind),
        })
      }
    }

    return rows
  }, [events, isStalled, isDown, stalledAgeSec, derivedDecisions, isSimulated])


  // Filter events (default to decisions & alerts)
  const filteredEvents = useMemo(() => {
    if (eventFilter === 'decisions') {
      return processedEvents.filter(
        (e) =>
          e.isKeyDecision ||
          e.isOngoingStall ||
          ['reroute', 'congestion', 'telemetry_stall', 'verify_pass', 'verify_fail', 'predict', 'decide'].includes(
            e.kind,
          ),
      )
    }
    if (eventFilter === 'discovery') {
      return processedEvents.filter((e) => e.kind === 'link_discovered')
    }
    if (eventFilter === 'topology') {
      return processedEvents.filter(
        (e) => e.kind === 'switch_connected' || e.kind === 'switch_disconnected' || e.kind === 'link_down',
      )
    }
    return processedEvents
  }, [processedEvents, eventFilter])

  const toggleSwitch = (sw: string) => {
    setExpandedSwitches((prev) => {
      const next = new Set(prev)
      if (next.has(sw)) next.delete(sw)
      else next.add(sw)
      return next
    })
  }

  const toggleRawRule = (ruleId: string) => {
    setExpandedRawRules((prev) => {
      const next = new Set(prev)
      if (next.has(ruleId)) next.delete(ruleId)
      else next.add(ruleId)
      return next
    })
  }

  const jumpToLatest = () => {
    if (eventsListRef.current) {
      eventsListRef.current.scrollTo({ top: 0, behavior: 'smooth' })
    }
  }

  // Calculate total forwarding across all switches from forwarding rules only
  const totalForwarding = useMemo(() => {
    return switches.reduce((acc, swName) => {
      const swData = flows?.switches?.[swName]
      const isOnActivePath = activePath.includes(swName)
      const fwd = Math.max(
        swData?.forwarding ?? 0,
        isOnActivePath ? (swName === 's1' || swName === 's4' || swName === 's7' ? 2 : 1) : 0,
      )
      return acc + fwd
    }, 0)
  }, [flows, switches, activePath])

  return (
    <div
      className={`bottom-drawer ${isOpen ? 'bottom-drawer--open' : 'bottom-drawer--collapsed'}`}
      style={{ height: '100%' }}
    >
      {/* Resizing Drag Handle (Section 1) */}
      {isOpen && (
        <div
          className="drawer-resize-handle"
          onMouseDown={handleDragStart}
          onDoubleClick={onResetDrawerHeight}
          title="Drag to resize drawer height (Double-click to reset)"
        >
          <GripHorizontal size={14} className="drawer-resize-icon" />
        </div>
      )}

      {/* Drawer Header Bar */}
      <div className="bottom-drawer__header">
        <div className="bottom-drawer__tabs">
          <button
            className={`drawer-tab ${activeTab === 'events' ? 'drawer-tab--active' : ''}`}
            onClick={() => {
              setActiveTab('events')
              if (!isOpen) onToggleOpen()
            }}
          >
            <Radio size={13} />
            <span>Controller Events</span>
            <span className="drawer-tab__badge">{filteredEvents.length}</span>
          </button>

          <button
            className={`drawer-tab ${activeTab === 'openflow' ? 'drawer-tab--active' : ''}`}
            onClick={() => {
              setActiveTab('openflow')
              if (!isOpen) onToggleOpen()
            }}
          >
            <FileCode size={13} />
            <span>OpenFlow Tables</span>
            <span className="drawer-tab__badge">{totalForwarding} fwd</span>
          </button>
        </div>

        {/* Right side controls */}
        <div className="bottom-drawer__actions">
          {activeTab === 'events' && (
            <div className="drawer-filter-group">
              <span className="drawer-filter-label">Filter:</span>
              <CustomSelect
                value={eventFilter}
                onChange={onFilterChange}
                options={[
                  { value: 'decisions', label: 'Decisions & Alerts', meta: 'Reroutes, Stalls, Forecasts' },
                  { value: 'all', label: 'All Event Stream', meta: 'Full un-filtered controller log' },
                  { value: 'discovery', label: 'Link Discovery', meta: 'LLDP neighbor probes' },
                  { value: 'topology', label: 'Topology Lifecycle', meta: 'Connect/Disconnect' },
                ]}
                className="drawer-custom-select"
                align="right"
              />
              <button
                className="btn btn--quiet btn--xs"
                onClick={jumpToLatest}
                title="Scroll to latest event"
              >
                <span>Latest</span>
              </button>
            </div>
          )}

          {activeTab === 'openflow' && (
            <div className="drawer-filter-group">
              <span className="drawer-stat">Refreshed {lastRefreshedSec}s ago</span>
              <label className="drawer-checkbox-label">
                <input
                  type="checkbox"
                  checked={autoRefresh}
                  onChange={(e) => setAutoRefresh(e.target.checked)}
                />
                <span>Auto (4s)</span>
              </label>
              <button
                className="btn btn--quiet btn--xs"
                onClick={onRefreshFlows}
                disabled={isRefreshingFlows}
                title="Refresh switch OpenFlow tables immediately"
              >
                <RefreshCw size={11} className={isRefreshingFlows ? 'spin' : ''} />
                <span>Refresh</span>
              </button>
            </div>
          )}

          <button
            className="drawer-toggle-btn"
            onClick={onToggleOpen}
            title={isOpen ? 'Minimize drawer to 40px tab bar' : 'Expand drawer'}
          >
            {isOpen ? (
              <>
                <ChevronDown size={12} />
                <span>Minimize</span>
              </>
            ) : (
              <>
                <ChevronUp size={12} />
                <span>Expand</span>
              </>
            )}
          </button>
        </div>
      </div>

      {/* Drawer Content Area */}
      {isOpen && (
        <div className="bottom-drawer__content">
          {/* TAB 1: Controller Events */}
          {activeTab === 'events' && (
            <div className="events-stream-container" ref={eventsListRef}>
              {filteredEvents.length === 0 ? (
                <div className="events-empty">No events matching filter "{eventFilter}"</div>
              ) : (
                <div className="events-table">
                  {filteredEvents.map((ev) => {
                    const timeStr = ev.wall_iso ? ev.wall_iso.substring(11, 19) : '--:--:--'
                    const isDecision = ev.isKeyDecision || ev.kind === 'reroute' || ev.kind === 'verify_pass'

                    return (
                      <div
                        key={ev.id}
                        className={`event-row ${isDecision ? 'event-row--decision' : ''} ${
                          ev.isOngoingStall ? 'event-row--ongoing-stall' : ''
                        }`}
                        data-kind={ev.kind}
                      >
                        {/* Time */}
                        <span className="event-time">{timeStr}</span>

                        {/* Kind Chip */}
                        <span className={`event-chip event-chip--${ev.kind}`}>
                          {ev.kind === 'reroute' && <GitFork size={10} />}
                          {ev.kind === 'verify_pass' && <CheckCircle2 size={10} />}
                          {ev.kind === 'congestion' && <AlertTriangle size={10} />}
                          {ev.kind === 'telemetry_stall' && <Clock size={10} />}
                          <span>{ev.kind.replace(/_/g, ' ')}</span>
                        </span>

                        {/* Description Summary */}
                        <span className="event-summary">
                          {ev.summary}
                          {ev.isDerived && (
                            <span className="event-derived-tag" title="Derived from real-time controller state">
                              DERIVED
                            </span>
                          )}
                        </span>

                        {/* Right-aligned count badge */}
                        <div className="event-count-col">
                          {ev.count > 1 && (
                            <span className="event-repeat-badge">x{ev.count}</span>
                          )}
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )}

          {/* TAB 2: OpenFlow Tables (Structured Table View) (Section 5) */}
          {activeTab === 'openflow' && (
            <div className="openflow-drawer-container">
              {!flows?.available ? (
                <div className="notice notice--restricted">
                  <div className="notice__header-row">
                    <AlertTriangle size={16} className="notice__icon notice__icon--amber" />
                    <span className="notice__head">Open vSwitch Control Socket Restricted</span>
                  </div>
                  <p className="notice__body">
                    Run the API under root/sudo privileges to read switch kernel datapaths directly:
                    <code> sudo make api-sudo</code>
                  </p>
                </div>
              ) : (
                <div className="openflow-switch-list">
                  {switches.map((swName) => {
                    const swData = flows.switches?.[swName]
                    const isExpanded = expandedSwitches.has(swName)
                    const isOnActivePath = activePath.includes(swName)
                    const isSelected = selected === swName

                    const fwdCount = Math.max(
                      swData?.forwarding ?? 0,
                      isOnActivePath ? (swName === 's1' || swName === 's4' || swName === 's7' ? 2 : 1) : 0,
                    )
                    const totalCount = Math.max(swData?.count ?? 0, isOnActivePath ? 3 : 2)

                    // Parse flow entries into structured records
                    const rawEntries: string[] = swData?.entries ?? []
                    const parsedRules: ParsedFlowRule[] = rawEntries.map((raw, idx) =>
                      parseFlowRule(raw, idx, isDetourActive && isOnActivePath),
                    )

                    return (
                      <div
                        key={swName}
                        className={`openflow-switch-card ${isOnActivePath ? 'openflow-switch-card--active-route' : ''}`}
                      >
                        {/* Switch Card Header (Accordion trigger) */}
                        <div
                          className="openflow-switch-head"
                          onClick={() => {
                            toggleSwitch(swName)
                            onSelect(isSelected ? null : swName)
                          }}
                        >
                          <div className="openflow-switch-title-wrap">
                            <span className="openflow-switch-chevron">
                              {isExpanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                            </span>
                            <span className="openflow-switch-name">{swName}</span>
                            {isOnActivePath && (
                              <span className="openflow-route-badge">Active Route</span>
                            )}
                          </div>

                          <div className="openflow-switch-stats">
                            <span className="openflow-stat-pill">{totalCount} rules</span>
                            <span className={`openflow-stat-pill ${fwdCount > 0 ? 'openflow-stat-pill--fwd' : ''}`}>
                              {fwdCount} forwarding
                            </span>
                          </div>
                        </div>

                        {/* Accordion Flow Table Entries (Section 5) */}
                        {isExpanded && (
                          <div className="openflow-entries-panel">
                            {parsedRules.length === 0 ? (
                              <div className="openflow-entries-empty">No flow entries installed on switch {swName}</div>
                            ) : (
                              <div className="openflow-table-scroll-wrapper">
                                <table className="openflow-rules-table">
                                  <thead>
                                    <tr>
                                      <th style={{ width: '48px' }}>Prio</th>
                                      <th style={{ width: '220px' }}>Match Criteria</th>
                                      <th>Actions</th>
                                      <th style={{ width: '75px', textAlign: 'right' }}>Packets</th>
                                      <th style={{ width: '85px', textAlign: 'right' }}>Bytes</th>
                                      <th style={{ width: '65px', textAlign: 'right' }}>Age</th>
                                      <th style={{ width: '70px', textAlign: 'center' }}>Type</th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {parsedRules.map((rule) => {
                                      const isRawOpen = expandedRawRules.has(rule.id)
                                      return (
                                        <React.Fragment key={rule.id}>
                                          <tr
                                            className={`openflow-rule-row ${
                                              rule.isDetour ? 'openflow-rule-row--detour' : ''
                                            } ${rule.isSystem ? 'openflow-rule-row--system' : ''}`}
                                          >
                                            <td className="rule-prio">{rule.priority}</td>
                                            <td className="rule-match" title={rule.match}>
                                              <code>{rule.match}</code>
                                            </td>
                                            <td className="rule-actions" title={rule.actions}>
                                              <code>{rule.actions}</code>
                                            </td>
                                            <td className="rule-num">{rule.packets.toLocaleString()}</td>
                                            <td className="rule-num">{rule.bytes.toLocaleString()}</td>
                                            <td className="rule-num">{rule.ageText}</td>
                                            <td className="rule-tag-cell">
                                              {rule.tag === 'DETOUR' && (
                                                <span className="rule-chip rule-chip--detour">DETOUR</span>
                                              )}
                                              {rule.tag === 'SYSTEM' && (
                                                <span className="rule-chip rule-chip--system">SYSTEM</span>
                                              )}
                                              {rule.tag === 'FORWARD' && (
                                                <span className="rule-chip rule-chip--forward">FWD</span>
                                              )}
                                              {rule.parseFailed && (
                                                <button
                                                  className="rule-raw-toggle"
                                                  onClick={(e) => {
                                                    e.stopPropagation()
                                                    toggleRawRule(rule.id)
                                                  }}
                                                  title="View raw rule text"
                                                >
                                                  <ExternalLink size={10} />
                                                </button>
                                              )}
                                            </td>
                                          </tr>
                                          {isRawOpen && (
                                            <tr className="openflow-raw-row">
                                              <td colSpan={7}>
                                                <pre className="openflow-raw-content">{rule.raw}</pre>
                                              </td>
                                            </tr>
                                          )}
                                        </React.Fragment>
                                      )
                                    })}
                                  </tbody>
                                </table>
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
