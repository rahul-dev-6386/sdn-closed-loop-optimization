/**
 * Redesigned High-Density Link Utilisation Strip-Chart with Numbered Annotation Lanes
 * Round 4: Cleaner graded strokes, sharper threshold, fewer visual clutter,
 * better distinction between simulated/measured/no-data states.
 *
 * Visual grammar (aligned with topology graph):
 * - Amber is the SIGNAL colour. Used ONLY where something is live/loaded above threshold.
 * - "Healthy" is the ABSENCE of amber.
 * - Cyan for model predictions, green for normal measured, muted for idle/no-data.
 * - Hatched pattern for stale gaps. Grid is structural, carries real info (time divisions).
 * - Two data colours on traces: amber (>=50%), green (<50%).
 * - Ink for readout, muted for secondary text.
 */

import { useMemo, useState } from 'react'
import {
  Clock,
  Layers,
  AlertTriangle,
  Info,
} from 'lucide-react'
import { fmtTime, fmtLinkLoad, sourceTooltip, type SourceType } from '../utils/formatters'
import { CustomSelect, type SelectOption } from './CustomSelect'

export interface TracePoint {
  t: number
  util: number
  mbps?: number
  usedMbps?: number
  capacityMbps?: number
  isMissing?: boolean
  link?: string
  baselineMbps?: number
  spikeMbps?: number
}

export interface TimelineEvent {
  t: number
  num: number
  label: string
  detail: string
  type: 'spike' | 'predict' | 'reroute' | 'verify' | 'stall'
  color: string
}

export interface LinkLoadSummary {
  key: string
  label: string
  usedMbps: number
  capacityMbps: number
  util: number
  pct: number
  isOnActiveRoute: boolean
  isBottleneck: boolean
}

export interface Props {
  points: TracePoint[]
  threshold?: number
  linkCapacityMbps?: number
  isSimulated?: boolean
  isLive?: boolean
  isStale?: boolean
  staleAgeSec?: number
  linkName?: string
  allLinksSummary: LinkLoadSummary[]
  selectedLink: string
  onSelectLink: (link: string) => void
  events?: TimelineEvent[]
  isSpikeActive?: boolean
  spikeRateMbps?: number
  isLinksOpen?: boolean
  onToggleLinksOpen?: () => void
  hasLiveSamples?: boolean
}

const W = 620
const H = 250
const PAD_L = 76
const PAD_R = 24
const PAD_T = 28
const PAD_B = 34

export function Trace({
  points,
  threshold = 0.8,
  linkCapacityMbps = 10,
  isSimulated = false,
  isLive: _isLive = true,
  isStale = false,
  staleAgeSec = 0,
  linkName = 's1–s2',
  allLinksSummary,
  selectedLink,
  onSelectLink,
  events = [],
  isSpikeActive = false,
  spikeRateMbps = 14.0,
  isLinksOpen = true,
  onToggleLinksOpen,
  hasLiveSamples = true,
}: Props) {
  const capacity = Math.max(1, linkCapacityMbps ?? 10)
  const innerW = W - PAD_L - PAD_R
  const innerH = H - PAD_T - PAD_B

  const [overlayActiveRoute, setOverlayActiveRoute] = useState(false)

  // Single source of truth link metric
  const selectedSummary =
    allLinksSummary?.find((l) => l.key === selectedLink) ||
    allLinksSummary?.find((l) => l.isBottleneck) ||
    allLinksSummary?.[0]

  const currentUtil = selectedSummary?.util ?? 0.05
  const currentUsedMbps = selectedSummary?.usedMbps ?? Number((currentUtil * capacity).toFixed(1))
  const displayLink = selectedSummary?.label ?? linkName
  const isExceeded = currentUtil >= threshold

  // Calculate layout coordinates for sample points, gap regions, and threshold
  const {
    segments,
    gapBoxes,
    timeTicks,
    numberedMarkers,
    xScale,
    yScale,
  } = useMemo(() => {
    if (points.length < 2) {
      return {
        segments: [],
        gapBoxes: [],
        timeTicks: [],
        numberedMarkers: [],
        xScale: () => PAD_L,
        yScale: () => PAD_T,
      }
    }

    const t0 = points[0].t
    const t1 = points[points.length - 1].t
    const span = Math.max(t1 - t0, 1)

    const x = (t: number) => PAD_L + ((t - t0) / span) * innerW
    const ymax = 1.0
    const y = (v: number) => PAD_T + innerH - (Math.min(Math.max(v, 0), ymax) / ymax) * innerH

    // Detect stale gaps (> 3.5s missing sample gap)
    const gaps: { x1: number; x2: number; width: number }[] = []
    const lineSegments: { x1: number; y1: number; x2: number; y2: number; p1: TracePoint; p2: TracePoint }[] = []

    for (let i = 0; i < points.length - 1; i++) {
      const p1 = points[i]
      const p2 = points[i + 1]
      const dt = p2.t - p1.t
      if (dt > 3.5 || p2.isMissing) {
        gaps.push({
          x1: x(p1.t),
          x2: x(p2.t),
          width: Math.max(3, x(p2.t) - x(p1.t)),
        })
      }
      lineSegments.push({
        x1: x(p1.t),
        y1: y(p1.util),
        x2: x(p2.t),
        y2: y(p2.util),
        p1,
        p2,
      })
    }

    // Time ticks
    const ticks = [
      { x: PAD_L, label: fmtTime(t0) },
      { x: PAD_L + innerW / 2, label: fmtTime(Math.round((t0 + t1) / 2)) },
      { x: PAD_L + innerW, label: fmtTime(t1) },
    ]

    // Staggered numbered markers in upper lane (Section 4)
    const filteredEvs = events.filter((ev) => ev.t >= t0 && ev.t <= t1)
    const markers: Array<TimelineEvent & { x: number; yOffset: number }> = []

    for (let i = 0; i < filteredEvs.length; i++) {
      const ev = filteredEvs[i]
      const posX = x(ev.t)
      // Check collision with previous marker (closer than 24px)
      let yOffset = 10
      if (markers.length > 0) {
        const lastMarker = markers[markers.length - 1]
        if (Math.abs(posX - lastMarker.x) < 24) {
          yOffset = lastMarker.yOffset === 10 ? 22 : 10
        }
      }
      markers.push({ ...ev, x: posX, yOffset })
    }

    return {
      segments: lineSegments,
      gapBoxes: gaps,
      timeTicks: ticks,
      numberedMarkers: markers,
      xScale: x,
      yScale: y,
    }
  }, [points, innerW, innerH, events])

  const yOf = (v: number) => PAD_T + innerH - (Math.min(v, 1.0) / 1.0) * innerH

  // Single formatted string matching everywhere
  const formattedLegend = fmtLinkLoad(displayLink, currentUsedMbps, capacity)

  // Traffic breakdown: Offered vs Carried vs Dropped (Section 4)
  const isTargetActive = (allLinksSummary ?? []).find((l) => l.key === selectedLink)?.isOnActiveRoute ?? false

  let offered = currentUsedMbps
  let carried = currentUsedMbps
  let dropped = 0.0

  if (isSpikeActive) {
    if (isTargetActive) {
      if (currentUsedMbps >= capacity * 0.8) {
        // Congested under primary spike
        offered = spikeRateMbps
        carried = currentUsedMbps
        dropped = Math.max(0, Number((offered - carried).toFixed(1)))
      } else {
        // Cleanly carrying diverted / baseline flow within capacity
        offered = currentUsedMbps
        carried = currentUsedMbps
        dropped = 0.0
      }
    } else {
      // Diverted or idle link (e.g. bottleneck after detour engaged)
      offered = currentUsedMbps
      carried = currentUsedMbps
      dropped = 0.0
    }
  } else {
    offered = currentUsedMbps
    carried = currentUsedMbps
    dropped = 0.0
  }

  const sourceType: SourceType = isSimulated ? 'simulated' : 'measured'

  // Dropdown options for CustomSelect
  const linkSelectOptions: SelectOption[] = (allLinksSummary ?? []).map((l) => ({
    value: l.key,
    label: `${l.label} (${l.pct}%)`,
    shortLabel: `${l.label} (${l.pct}%)`,
    meta: `${l.usedMbps} / ${l.capacityMbps} Mbps${l.isBottleneck ? ' · Primary Bottleneck' : ''}`,
    badge: l.isBottleneck ? 'Bottleneck' : l.isOnActiveRoute ? 'Route' : undefined,
    badgeColor: l.isBottleneck ? 'danger' : l.isOnActiveRoute ? 'normal' : undefined,
  }))

  if (!allLinksSummary || allLinksSummary.length === 0) {
    return (
      <div className="trace-card-root">
        <div className="trace-empty-state">
          <svg viewBox={`0 0 ${W} ${H}`} width="100%" height="100%">
            <rect
              x={PAD_L}
              y={PAD_T}
              width={innerW}
              height={innerH}
              fill="rgba(15, 20, 25, 0.92)"
            />
            <text
              x={PAD_L + innerW / 2}
              y={PAD_T + innerH / 2 - 12}
              textAnchor="middle"
              fill="#f59e0b"
              fontFamily="var(--mono)"
              fontSize="12"
              fontWeight="700"
            >
              NO LINK DATA — Select a topology first
            </text>
            <text
              x={PAD_L + innerW / 2}
              y={PAD_T + innerH / 2 + 10}
              textAnchor="middle"
              fill="var(--ink-faint)"
              fontFamily="var(--mono)"
              fontSize="11"
            >
              Please select a network topology to display link utilisations
            </text>
          </svg>
        </div>
      </div>
    )
  }

  return (
    <div className="trace-card-root">
      {/* Chart Top Control Header */}
      <div className="trace-ctrl-row">
        {/* Link Selector CustomSelect */}
        <div className="trace-select-group">
          <label className="trace-select-label">Target Link:</label>
          <CustomSelect
            value={selectedLink}
            onChange={onSelectLink}
            options={linkSelectOptions}
            className="trace-custom-select"
            ariaLabel="Select target link to inspect"
          />
        </div>

        {/* Live / Stale / Simulated Indicators */}
        <div className="trace-badge-group">
          {isStale ? (
            <span
              className="badge badge--stale"
              title={`Source: Measured telemetry\nStalled: Last sample received ${staleAgeSec}s ago`}
            >
              <Clock size={11} /> STALE ({staleAgeSec}s ago)
            </span>
          ) : isSimulated ? (
            <span
              className="badge badge--simulated"
              title={sourceTooltip('simulated', 'Synthetic test traffic')}
            >
              SIMULATED
            </span>
          ) : (
            <span
              className="badge badge--live"
              title={sourceTooltip('measured', 'Live kernel datapath counters')}
            >
              LIVE TELEMETRY
            </span>
          )}

          <button
            className={`btn btn--quiet btn--xs ${overlayActiveRoute ? 'btn--active' : ''}`}
            onClick={() => setOverlayActiveRoute((prev) => !prev)}
            title="Overlay all links along the active route"
          >
            <Layers size={11} />
            <span>Overlay Route</span>
          </button>
        </div>
      </div>

      {/* Metrics Banner: Single Source of Truth + Flow Breakdown */}
      <div className="trace-metrics-banner">
        <div
          className={`trace-chip ${isExceeded ? 'trace-chip--danger' : 'trace-chip--normal'}`}
          title={
            isSimulated
              ? 'source = simulated · Synthetic dynamic flow conservation load'
              : !hasLiveSamples
              ? 'source = telemetry_csv — no live samples yet'
              : sourceTooltip(sourceType, `${currentUsedMbps} Mbps measured out of ${capacity} Mbps capacity`)
          }
        >
          <span className="trace-chip__dot" />
          <strong className="trace-chip__text">
            {!isSimulated && !hasLiveSamples ? `${displayLink} · NO DATA` : formattedLegend}
          </strong>
          {isExceeded && (
            <span className="trace-chip__alert">
              <AlertTriangle size={11} /> Exceeds 80% Threshold
            </span>
          )}
          <span
            className="trace-source-tag"
            title={isSimulated ? 'source = simulated' : 'source = telemetry_csv (port-stats logged by the controller)'}
          >
            [{isSimulated ? 'SIMULATED' : hasLiveSamples ? 'MEASURED' : 'NO DATA'}]
          </span>
        </div>

        {/* Breakdown: Offered vs Carried vs Dropped (Section 4) */}
        <div className="trace-flow-breakdown">
          <span className="flow-pill flow-pill--baseline" title={isSimulated ? 'source = simulated (Offered throughput demand)' : 'Offered throughput demand'}>
            Offered: {offered.toFixed(1)} Mbps
          </span>
          <span
            className={`flow-pill ${carried >= 8.0 ? 'flow-pill--spike' : 'flow-pill--baseline'}`}
            title={isSimulated ? 'source = simulated (Carried bandwidth)' : 'Carried bandwidth on target link'}
          >
            Carried: {carried.toFixed(1)} Mbps
          </span>
          {dropped > 0 ? (
            <span className="flow-pill flow-pill--dropped" title={isSimulated ? 'source = simulated (Dropped traffic)' : 'Dropped traffic due to link congestion'}>
              Dropped: {dropped.toFixed(1)} Mbps
            </span>
          ) : (
            <span className="flow-pill flow-pill--idle">Dropped: 0.0 Mbps</span>
          )}
        </div>
      </div>

      {/* SVG Chart Plot */}
      <div className="trace-canvas-wrap">
        <svg
          className="trace-svg"
          viewBox={`0 0 ${W} ${H}`}
          preserveAspectRatio="xMidYMid meet"
          role="img"
          aria-label="Link utilisation strip-chart with congestion threshold"
        >
          <defs>
            {/* Hatched pattern for missing/stalled samples */}
            <pattern id="staleHatch" width="8" height="8" patternTransform="rotate(45 0 0)" patternUnits="userSpaceOnUse">
              <line x1="0" y1="0" x2="0" y2="8" stroke="rgba(245, 158, 11, 0.4)" strokeWidth="2.5" />
            </pattern>
          </defs>

          {/* Explicit Empty State when in Live Mode with No Telemetry */}
          {!isSimulated && !hasLiveSamples && (
            <g className="trace-empty-state">
              <rect
                x={PAD_L}
                y={PAD_T}
                width={innerW}
                height={innerH}
                fill="rgba(15, 20, 25, 0.92)"
              />
              <text
                x={PAD_L + innerW / 2}
                y={PAD_T + innerH / 2 - 12}
                textAnchor="middle"
                fill="#f59e0b"
                fontFamily="var(--mono)"
                fontSize="12"
                fontWeight="700"
              >
                NO TELEMETRY YET — P5 NOT RUNNING
              </text>
              <text
                x={PAD_L + innerW / 2}
                y={PAD_T + innerH / 2 + 10}
                textAnchor="middle"
                fill="var(--ink-faint)"
                fontFamily="var(--mono)"
                fontSize="11"
              >
                Waiting for OpenFlow PortStats on /ws/live
              </text>
            </g>
          )}

          {/* Graticule Grid */}
          <g aria-hidden="true">
            {[0, 0.2, 0.4, 0.6, 0.8, 1.0].map((f) => (
              <line
                key={f}
                className={f === 0.8 ? 'trace__grid trace__grid--threshold' : 'trace__grid'}
                x1={PAD_L}
                x2={W - PAD_R}
                y1={yOf(f)}
                y2={yOf(f)}
              />
            ))}
          </g>

          {/* Stale Telemetry Gap Rectangles */}
          {gapBoxes.map((gap, idx) => (
            <g key={`gap-${idx}`}>
              <rect
                x={gap.x1}
                y={PAD_T}
                width={gap.width}
                height={innerH}
                fill="url(#staleHatch)"
                opacity={0.65}
              />
              <text
                x={gap.x1 + gap.width / 2}
                y={PAD_T + 14}
                textAnchor="middle"
                fill="#fbbf24"
                fontSize="9"
                fontFamily="var(--mono)"
              >
                STALL
              </text>
            </g>
          ))}

          {/* Single On-Chart Threshold Label on Right Edge */}
          <line
            className="trace__threshold"
            x1={PAD_L}
            x2={W - PAD_R}
            y1={yOf(threshold)}
            y2={yOf(threshold)}
          />
          <text
            className="trace__threshold-text"
            x={W - PAD_R - 4}
            y={yOf(threshold) - 5}
            textAnchor="end"
          >
            80% Congestion Limit ({Number((threshold * capacity).toFixed(0))} Mbps)
          </text>

          {/* Numbered Event Markers in Upper Lane (Section 4) */}
          {numberedMarkers.map((em) => (
            <g key={`marker-${em.num}`} className="trace__marker">
              {/* Vertical line through plot */}
              <line
                x1={em.x}
                x2={em.x}
                y1={PAD_T}
                y2={PAD_T + innerH}
                stroke={em.color}
                strokeWidth={1.5}
                strokeDasharray="3 3"
              />
              {/* Numbered Badge Circle in Upper Lane */}
              <circle
                cx={em.x}
                cy={em.yOffset}
                r={8.5}
                fill="#0f172a"
                stroke={em.color}
                strokeWidth={1.5}
              />
              <text
                x={em.x}
                y={em.yOffset + 3.5}
                textAnchor="middle"
                fill={em.color}
                fontSize="9"
                fontFamily="var(--mono)"
                fontWeight="700"
              >
                {em.num}
              </text>
            </g>
          ))}

          {/* Y-Axis: Reserved margin (PAD_L=76px) and >= 12px font */}
          {[1.0, 0.8, 0.6, 0.4, 0.2, 0.0].map((f) => {
            const mbpsVal = Number((f * capacity).toFixed(0))
            const isThr = f === 0.8
            return (
              <text
                key={f}
                className={`trace__axis-label ${isThr ? 'trace__axis-label--danger' : ''}`}
                x={PAD_L - 8}
                y={yOf(f) + 4}
                textAnchor="end"
              >
                {mbpsVal}M <tspan className="trace__axis-faint">({Math.round(f * 100)}%)</tspan>
              </text>
            )
          })}

          {/* Real Sample Segments & Points */}
          {segments.map((seg, idx) => {
            const isSegAbove = seg.p1.util >= threshold || seg.p2.util >= threshold
            return (
              <line
                key={`seg-${idx}`}
                x1={seg.x1}
                y1={seg.y1}
                x2={seg.x2}
                y2={seg.y2}
                stroke={isSegAbove ? '#ef4444' : '#0ea5e9'}
                strokeWidth={2.5}
                strokeLinecap="round"
              />
            )
          })}

          {/* Discrete Sample Points */}
          {points.map((p, idx) => {
            const px = xScale(p.t)
            const py = yScale(p.util)
            const isPointAbove = p.util >= threshold
            return (
              <circle
                key={`pt-${idx}`}
                cx={px}
                cy={py}
                r={3}
                fill={isPointAbove ? '#ef4444' : '#38bdf8'}
                stroke="#0f172a"
                strokeWidth={1.5}
              />
            )
          })}

          {/* Time X-Axis */}
          {timeTicks.map((t, idx) => (
            <text
              key={idx}
              className="trace__time-axis"
              x={t.x}
              y={PAD_T + innerH + 18}
              textAnchor={idx === 0 ? 'start' : idx === timeTicks.length - 1 ? 'end' : 'middle'}
            >
              {t.label}
            </text>
          ))}
        </svg>
      </div>

      {/* Numbered Event Annotations Legend List (Section 4) */}
      {numberedMarkers.length > 0 && (
        <div className="trace-event-legend">
          <div className="trace-legend-title">Timeline Events:</div>
          <div className="trace-legend-items">
            {numberedMarkers.map((em) => (
              <div key={`legend-${em.num}`} className="trace-legend-item">
                <span className="trace-legend-pill" style={{ borderColor: em.color, color: em.color }}>
                  {em.num}
                </span>
                <span className="trace-legend-text">
                  <strong>{em.label}</strong> ({fmtTime(em.t)}) · {em.detail}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Ranked List of ALL Links with Mini Bars */}
      <div className="ranked-links-section">
        <div
          className="ranked-links-header"
          onClick={onToggleLinksOpen}
          style={{ cursor: onToggleLinksOpen ? 'pointer' : 'default' }}
        >
          <span className="ranked-links-title">All Topology Links (Ranked by Load)</span>
          <span className="ranked-links-hint">
            <Info size={10} style={{ display: 'inline', marginRight: 3 }} />
            Click to plot link
          </span>
        </div>

        {isLinksOpen && (
          <div className="ranked-links-list">
            {allLinksSummary.map((l) => {
              const isSelected = selectedLink === l.key
              const isOver = l.util >= 0.8
              const isMedium = l.util >= 0.5 && l.util < 0.8
              const barClass = isOver ? 'mini-bar--danger' : isMedium ? 'mini-bar--warn' : 'mini-bar--normal'

              return (
                <div
                  key={l.key}
                  className={`ranked-link-item ${isSelected ? 'ranked-link-item--selected' : ''}`}
                  onClick={() => onSelectLink(l.key)}
                  role="button"
                  tabIndex={0}
                  title={`Click to plot link ${l.label} · ${l.usedMbps} / ${l.capacityMbps} Mbps`}
                >
                  <div className="ranked-link-meta">
                    <span className="ranked-link-name">
                      <span>{l.label}</span>
                      {l.isBottleneck && <span className="ranked-tag ranked-tag--hot">Bottleneck</span>}
                      {l.isOnActiveRoute && <span className="ranked-tag ranked-tag--active">Route</span>}
                    </span>
                    <span className="ranked-link-val">
                      {l.usedMbps.toFixed(1)} / {l.capacityMbps} Mbps ({l.pct}%)
                    </span>
                  </div>
                  <div className="ranked-mini-bar-track">
                    <div
                      className={`ranked-mini-bar-fill ${barClass}`}
                      style={{ width: `${Math.min(100, Math.max(2, l.pct))}%` }}
                    />
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}