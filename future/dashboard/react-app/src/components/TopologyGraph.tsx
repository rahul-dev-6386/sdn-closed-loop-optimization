/**
 * Redesigned Interactive Network Topology Graph
 * Round 4: Cleaner highway layout, clearer load signals, modernised aesthetics.
 * 
 * Visual grammar:
 * - Amber is the SIGNAL colour. Used ONLY where something is live or loaded.
 * - "Healthy" is the ABSENCE of amber, never a third hue.
 * - The graticule is a structural device, not decoration. Carries real information.
 * - Two data colours: amber (loaded/hazard) and cyan (model prediction).
 * - Ink for readout, muted for secondary text.
 */

import { useEffect, useMemo, useRef, useState, useCallback } from 'react'
import {
  Globe,
  Layers,
  Camera,
  RotateCw,
  Zap,
  Square,
  GitFork,
  CheckCircle2,
  AlertTriangle,
  Activity,
  Maximize2,
  Crosshair,
} from 'lucide-react'
import type { Topology } from '../api'
import { TOPOLOGIES, type TopologyDef } from '../topologies'
import { Topology3D } from './Topology3D'
import { CustomSelect, type SelectOption } from './CustomSelect'
import { sourceTooltip, canonicalLinkKey } from '../utils/formatters'

interface Props {
  topology: Topology | null
  activeLinks: Set<string>
  selected: string | null
  onSelect: (name: string | null) => void
  hasActiveRoute: boolean
  isCongested: boolean
  onToggleCongestion: () => void
  onStopCongestion?: () => void
  isDetourActive: boolean
  onToggleDetour: () => void
  detourBusy?: boolean
  detourError?: string | null
  pathState?: 'ok' | 'unknown' | 'unavailable'
  selectedTopoId: string
  onSelectTopoId: (id: string) => void
  bottleneckLoadText: string
  isOverThreshold: boolean
  verifyStatus?: { passed: boolean; message: string; status?: string }
  linkMetrics?: Record<string, { usedMbps: number; capacityMbps: number; util: number }>
  onFitRequested?: () => void
  isSimulated?: boolean
  hasLiveSamples?: boolean
  isLoading?: boolean
}

const DEFAULT_W = 800
const DEFAULT_H = 500

interface Vehicle {
  id: number
  progress: number
  speed: number
  color: string
}

function layoutTopology(topo: TopologyDef | Topology): Map<string, { x: number; y: number }> {
  const positions = new Map<string, { x: number; y: number }>()

  // 1. Layered deterministic coordinates from catalog
  const customCoords =
    (topo as TopologyDef).node_coordinates || topo.node_coordinates
  if (customCoords && Object.keys(customCoords).length > 0) {
    for (const [name, coord] of Object.entries(customCoords)) {
      positions.set(name, { x: coord.x, y: coord.y })
    }
    return positions
  }

  // 2. Deterministic Fallback if coordinates missing
  const switches = topo.switches || []
  const n = switches.length
  switches.forEach((s, idx) => {
    positions.set(s, {
      x: 160 + (idx / Math.max(1, n - 1)) * 480,
      y: 250 + (idx % 2 === 0 ? -60 : 60),
    })
  })

  topo.hosts?.forEach((h, idx) => {
    positions.set(h.name, {
      x: idx === 0 ? 60 : 740,
      y: 250,
    })
  })

  return positions
}

export function TopologyGraph({
  topology: _topology,
  selected,
  onSelect,
  hasActiveRoute,
  isCongested,
  onToggleCongestion,
  onStopCongestion,
  isDetourActive,
  onToggleDetour,
  detourBusy = false,
  detourError = null,
  pathState = 'unknown',
  selectedTopoId,
  onSelectTopoId,
  bottleneckLoadText,
  isOverThreshold,
  verifyStatus = { passed: true, status: 'IDLE', message: 'Direct routing active (no detour deployed)' },
  linkMetrics = {},
  isSimulated = false,
  hasLiveSamples = false,
}: Props) {
  // View mode: 2D blueprint (default, clearest) or 3D perspective
  const [viewMode, setViewMode] = useState<'3d' | '2d'>('2d')
  const [cameraPreset, setCameraPreset] = useState<'perspective' | 'top' | 'follow'>('perspective')
  const [autoRotate, setAutoRotate] = useState<boolean>(false)
  const [zoomTarget, setZoomTarget] = useState<string | null>(null)

  const containerRef = useRef<HTMLDivElement>(null)

  const currentDef = useMemo(() => {
    return TOPOLOGIES.find((t) => t.id === selectedTopoId) ?? TOPOLOGIES[0]
  }, [selectedTopoId])

  const positions = useMemo(() => {
    return layoutTopology(currentDef)
  }, [currentDef])

  // Fit to view dynamic bounding box with 48px padding
  const { viewBox } = useMemo(() => {
    if (positions.size === 0) return { viewBox: `0 0 ${DEFAULT_W} ${DEFAULT_H}` }

    let minX = Infinity,
      maxX = -Infinity,
      minY = Infinity,
      maxY = -Infinity

    // If focused on bottleneck link, calculate zoomed region
    if (zoomTarget === 'bottleneck' && currentDef.bottleneckLink) {
      const parts = currentDef.bottleneckLink.split('--')
      const p1 = positions.get(parts[0])
      const p2 = positions.get(parts[1])
      if (p1 && p2) {
        minX = Math.min(p1.x, p2.x) - 100
        maxX = Math.max(p1.x, p2.x) + 100
        minY = Math.min(p1.y, p2.y) - 80
        maxY = Math.max(p1.y, p2.y) + 80
        return {
          viewBox: `${Math.round(minX)} ${Math.round(minY)} ${Math.round(maxX - minX)} ${Math.round(maxY - minY)}`,
        }
      }
    }

    for (const p of positions.values()) {
      if (p.x < minX) minX = p.x
      if (p.x > maxX) maxX = p.x
      if (p.y < minY) minY = p.y
      if (p.y > maxY) maxY = p.y
    }

    // 48px margin ensuring all nodes, labels, and outer hosts are completely visible
    const pad = 48
    const vx = Math.round(minX - pad)
    const vy = Math.round(minY - pad)
    const vw = Math.round(maxX - minX + pad * 2)
    const vh = Math.round(maxY - minY + pad * 2)
    return { viewBox: `${vx} ${vy} ${vw} ${vh}` }
  }, [positions, zoomTarget, currentDef.bottleneckLink])

  // Fit to view action button handler
  const handleFitToView = useCallback(() => {
    setZoomTarget(null)
  }, [])

  // Focus bottleneck link action button handler
  const handleFocusBottleneck = useCallback(() => {
    setZoomTarget((prev) => (prev === 'bottleneck' ? null : 'bottleneck'))
  }, [])

  // Active path sequence
  const activePath = useMemo(() => {
    if (!hasActiveRoute) return []
    if (isDetourActive) return currentDef.alternatePath
    return currentDef.defaultPath
  }, [hasActiveRoute, isDetourActive, currentDef])

  const activePathLinks = useMemo(() => {
    const set = new Set<string>()
    for (let i = 0; i < activePath.length - 1; i++) {
      set.add(canonicalLinkKey(activePath[i], activePath[i + 1]))
    }
    return set
  }, [activePath])

  const [vehicles, setVehicles] = useState<Vehicle[]>(() => [
    { id: 1, progress: 0.1, speed: 0.0035, color: '#38bdf8' },
    { id: 2, progress: 0.45, speed: 0.003, color: '#34d399' },
    { id: 3, progress: 0.78, speed: 0.004, color: '#fbbf24' },
  ])
  const animRef = useRef<number | null>(null)

  useEffect(() => {
    if (!hasActiveRoute || activePath.length < 2) return

    const step = () => {
      setVehicles((prev) =>
        prev.map((v) => {
          let p = v.progress + v.speed
          if (p >= 1.0) p = 0.0
          return { ...v, progress: p }
        }),
      )
      animRef.current = requestAnimationFrame(step)
    }
    animRef.current = requestAnimationFrame(step)

    return () => {
      if (animRef.current) cancelAnimationFrame(animRef.current)
    }
  }, [hasActiveRoute, activePath])

  // Vehicle coordinate interpolator
  const getVehiclePos = (progress: number) => {
    if (activePath.length < 2) return null
    const numSegments = activePath.length - 1
    const scaled = progress * numSegments
    const segIndex = Math.min(Math.floor(scaled), numSegments - 1)
    const segT = scaled - segIndex

    const aName = activePath[segIndex]
    const bName = activePath[segIndex + 1]
    const aPos = positions.get(aName)
    const bPos = positions.get(bName)
    if (!aPos || !bPos) return null

    const x = aPos.x + (bPos.x - aPos.x) * segT
    const y = aPos.y + (bPos.y - aPos.y) * segT
    const angle = Math.atan2(bPos.y - aPos.y, bPos.x - aPos.x) * (180 / Math.PI)
    return { x, y, angle }
  }

  // Plain wording decision text
  const detourVerified = verifyStatus?.passed === true
  const detourFailing = verifyStatus?.status === 'FAIL'
  const decisionText = useMemo(() => {
    if (!hasActiveRoute) return 'Network Idle — No Active Forwarding'
    if (isDetourActive) {
      if (detourFailing) {
        return `Detour engaged but bottleneck NOT relieved — traffic still on ${currentDef.defaultPath.join('-')}`
      }
      if (detourVerified) {
        return `Detour via ${currentDef.alternatePath.join('-')} active (diverted around bottleneck)`
      }
      return `Detour requested via ${currentDef.alternatePath.join('-')} — verifying on the datapath…`
    }
    if (isCongested) {
      return `Primary route active (${currentDef.defaultPath.join('-')}) — Traverses congested bottleneck (${currentDef.bottleneckLink})`
    }
    return `Primary route active (${currentDef.defaultPath.join('-')})`
  }, [hasActiveRoute, isDetourActive, detourFailing, detourVerified, isCongested, currentDef])

  const decisionBadgeClass = !hasActiveRoute
    ? 'faint'
    : isDetourActive
    ? 'normal'
    : isCongested
    ? 'danger'
    : 'normal'

  // Options for Topology selector card grid
  const topoOptions: SelectOption[] = TOPOLOGIES.map((t) => ({
    value: t.id,
    label: t.name,
    shortLabel: t.name.split(' (')[0],
    meta: `${t.switches.length} switches, ${t.links.filter((l) => !l.host_link).length} links · ${t.category}`,
    badge: t.category,
  }))

  const cameraOptions: SelectOption[] = [
    { value: 'perspective', label: 'Perspective (35°)', shortLabel: 'Persp' },
    { value: 'top', label: 'Top-Down Plan', shortLabel: 'Top' },
    { value: 'follow', label: 'Follow Highway', shortLabel: 'Follow' },
  ]

  return (
    <div className="topology-view-root" ref={containerRef}>
      {/* Slim Toolbar (Section 3.5) */}
      <div className="topo-toolbar">
        {/* Left: View Mode Toggle (Neutral Styling) */}
        <div className="topo-toolbar__left">
          <div className="segmented-group" role="group" aria-label="Topology View Mode">
            <button
              type="button"
              className={`segmented-btn ${viewMode === '2d' ? 'segmented-btn--active' : ''}`}
              onClick={() => setViewMode('2d')}
              title="Switch to 2D Blueprint"
            >
              <Layers size={12} />
              <span>2D</span>
            </button>
            <button
              type="button"
              className={`segmented-btn ${viewMode === '3d' ? 'segmented-btn--active' : ''}`}
              onClick={() => setViewMode('3d')}
              title="Switch to 3D perspective"
            >
              <Globe size={12} />
              <span>3D</span>
            </button>
          </div>

          {/* Camera Preset Listbox */}
          {viewMode === '3d' && (
            <div className="segmented-group" role="group" aria-label="Camera Controls">
              <CustomSelect
                value={cameraPreset}
                onChange={(val) => setCameraPreset(val as 'perspective' | 'top' | 'follow')}
                options={cameraOptions}
                triggerPrefix={<Camera size={11} />}
                className="topo-select--camera"
                ariaLabel="Select 3D Camera Angle"
              />
              <button
                type="button"
                className={`segmented-btn ${autoRotate ? 'segmented-btn--active' : ''}`}
                onClick={() => setAutoRotate((prev) => !prev)}
                title={autoRotate ? 'Stop orbit auto-rotation' : 'Start orbit auto-rotation'}
                aria-label="Toggle camera auto-rotation"
              >
                <RotateCw size={11} className={autoRotate ? 'spin' : ''} />
              </button>
            </div>
          )}

          {/* Fit & Focus Bottleneck Buttons (Section 3.1) */}
          <div className="segmented-group" role="group" aria-label="Viewport Controls">
            <button
              type="button"
              className="segmented-btn"
              onClick={handleFitToView}
              title="Fit entire topology in view with 48px padding"
            >
              <Maximize2 size={11} />
              <span>Fit</span>
            </button>
            <button
              type="button"
              className={`segmented-btn ${zoomTarget === 'bottleneck' ? 'segmented-btn--active' : ''}`}
              onClick={handleFocusBottleneck}
              title="Focus viewport directly on congested bottleneck link"
            >
              <Crosshair size={11} />
              <span>Focus</span>
            </button>
          </div>

          {/* Topology Selector Card Grid (Section 2) */}
          <div className="toolbar-field">
            <CustomSelect
              value={selectedTopoId}
              onChange={onSelectTopoId}
              options={topoOptions}
              isCardGrid={true}
              className="topo-select--wide"
              ariaLabel="Select network topology architecture"
            />
          </div>
        </div>

        {/* Right Action Controls: Spike, Stop, Detour Toggle */}
        <div className="topo-toolbar__right">
          <button
            className={`topo-btn ${isCongested ? 'topo-btn--danger' : 'topo-btn--action'}`}
            onClick={onToggleCongestion}
            title={isCongested ? 'Spike active (14 Mbps injected)' : 'Inject 14 Mbps congestion test traffic'}
          >
            <Zap size={12} className={isCongested ? 'spin' : ''} />
            <span>{isCongested ? 'Spiking 14M' : 'Spike 14M'}</span>
          </button>

          {/* Stop Spike Button (Only visible while spike is active) (Section 3.5) */}
          {isCongested && (
            <button
              className="topo-btn topo-btn--stop"
              onClick={onStopCongestion || onToggleCongestion}
              title="Terminate active iperf3 spike traffic immediately"
            >
              <Square size={11} />
              <span>Stop</span>
            </button>
          )}

          {/* Detour Switch (ON / OFF) — triggers the real closed-loop fix */}
          <button
            className={`topo-btn topo-btn--detour ${isDetourActive ? 'topo-btn--detour-on' : 'topo-btn--detour-off'}`}
            onClick={onToggleDetour}
            disabled={detourBusy || pathState === 'unavailable'}
            title={pathState === 'unavailable'
              ? 'Controller state unavailable — start the fabric (Start Fabric) and retry'
              : detourBusy
                ? 'Agent is running the closed-loop fix — telemetry → pin → controller decision → verification…'
                : isDetourActive
                  ? 'Agent diverted the flow around the bottleneck — the pin is held for 30s. Click to release it.'
                  : 'Ask the agent to relieve the bottleneck — verified closed-loop detour'}
          >
            <GitFork size={12} className={detourBusy ? 'spin' : ''} />
            <span>{detourBusy ? 'Agent fixing…' : `Detour: ${isDetourActive ? 'ON' : 'OFF'}`}</span>
          </button>
          {detourError && (
            <span
              className="agent-save-msg text-red"
              title={detourError}
              style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis' }}
            >
              detour: {detourError}
            </span>
          )}
        </div>
      </div>

      {/* Unified Status Strip (Route, Bottleneck, Decision, Verification) */}
      <div className="topo-status-strip">
        <div className="status-strip__item">
          <span className="status-strip__label">Route:</span>
          <span className="status-strip__value" title={`Active path: ${activePath.join(' → ')}`}>
            {activePath.length > 0 ? (
              <span className="route-chip-list">
                {activePath.map((step, idx) => (
                  <span key={idx} className="route-step-chip">
                    {step}
                    {idx < activePath.length - 1 && <span className="chip-sep">→</span>}
                  </span>
                ))}
              </span>
            ) : (
              'None'
            )}
          </span>
        </div>

        <div className="status-strip__item">
          <span className="status-strip__label">Bottleneck:</span>
          <span
            className={`status-badge status-badge--${
              !hasActiveRoute ? 'faint' : isOverThreshold ? 'danger' : 'normal'
            }`}
            title={
              isSimulated
                ? 'source = simulated · Synthetic dynamic flow conservation load'
                : !hasLiveSamples
                ? 'source = telemetry_csv — no live samples yet'
                : sourceTooltip('measured')
            }
          >
            {isOverThreshold ? <AlertTriangle size={11} /> : <Activity size={11} />}
            <span>{bottleneckLoadText}</span>
          </span>
        </div>

        <div className="status-strip__item status-strip__item--decision">
          <span className="status-strip__label">Decision:</span>
          <span className={`status-badge status-badge--${decisionBadgeClass}`} title={decisionText}>
            {decisionBadgeClass === 'normal' && <CheckCircle2 size={11} />}
            {decisionBadgeClass === 'faint' && <GitFork size={11} />}
            {decisionBadgeClass === 'danger' && <AlertTriangle size={11} />}
            <span>{decisionText}</span>
          </span>
        </div>

        <div className="status-strip__item">
          <span className="status-strip__label">Verify:</span>
          <span
            className={`status-badge status-badge--${
              verifyStatus.status === 'PASS' || verifyStatus.status === 'IDLE' || verifyStatus.passed
                ? 'normal'
                : verifyStatus.status === 'PENDING'
                ? 'warning'
                : 'danger'
            }`}
            title={verifyStatus.message}
          >
            {verifyStatus.status === 'PASS' || verifyStatus.status === 'IDLE' || verifyStatus.passed ? (
              <CheckCircle2 size={11} />
            ) : verifyStatus.status === 'PENDING' ? (
              <Activity size={11} className="spin" />
            ) : (
              <AlertTriangle size={11} />
            )}
            <span>{verifyStatus.message}</span>
          </span>
        </div>
      </div>

      {/* Canvas Viewport: 3D or 2D (Section 3.1: Resizes dynamically with drawer) */}
      {viewMode === '3d' ? (
        <div className="topo-canvas-container" style={{ flex: 1, minHeight: '360px', position: 'relative' }}>
          <Topology3D
            topologyDef={currentDef}
            activePath={activePath}
            bottleneckLink={currentDef.bottleneckLink}
            isCongested={isCongested}
            selected={selected}
            onSelect={onSelect}
            cameraPreset={cameraPreset}
            autoRotate={autoRotate}
          />
        </div>
      ) : (
        <div className="topo-canvas-container" style={{ flex: 1, minHeight: '360px', position: 'relative' }}>
          <svg
            className="graph"
            viewBox={viewBox}
            role="img"
            aria-label="Network topology highway and traffic flow"
            preserveAspectRatio="xMidYMid meet"
          >
            {/* Background cyber grid */}
            <g aria-hidden="true">
              {Array.from({ length: 32 }, (_, i) => (
                <line
                  key={`v${i}`}
                  className="trace__grid"
                  x1={-120 + i * 36}
                  y1={-120}
                  x2={-120 + i * 36}
                  y2={DEFAULT_H + 120}
                />
              ))}
              {Array.from({ length: 24 }, (_, i) => (
                <line
                  key={`h${i}`}
                  className="trace__grid"
                  x1={-120}
                  y1={-120 + i * 36}
                  x2={DEFAULT_W + 120}
                  y2={-120 + i * 36}
                />
              ))}
            </g>

            {/* Links colored by load with thickness scaled by bandwidth */}
            <g>
              {currentDef.links.map((link, i) => {
                const from = positions.get(link.a)
                const to = positions.get(link.b)
                if (!from || !to) return null
                const key = canonicalLinkKey(link.a, link.b)
                const isActive = activePathLinks.has(key)
                const isBottleneck = key === canonicalLinkKey(currentDef.bottleneckLink.split('--')[0], currentDef.bottleneckLink.split('--')[1])

                const cap = link.bw_mbps ?? 10
                const m = linkMetrics[key]
                const util = m?.util ?? (isBottleneck && isCongested ? 0.88 : isActive ? 0.31 : 0.05)
                const usedMbps = m?.usedMbps ?? Number((util * cap).toFixed(1))

                // Color scale: amber >=80%, amber-dim 50-80%, green active, rule-bright idle
                const strokeColor =
                  util >= 0.8 ? '#ef4444' : util >= 0.5 ? '#f59e0b' : isActive ? '#10b981' : '#64748b'
                const strokeWidth = Math.max(3, Math.min(8.5, (usedMbps / cap) * 8.5))

                const midX = (from.x + to.x) / 2
                const midY = (from.y + to.y) / 2
                const angle = Math.atan2(to.y - from.y, to.x - from.x) * (180 / Math.PI)

                return (
                  <g key={`road-${key}-${i}`} opacity={isActive ? 1.0 : 0.65}>
                    {/* Road Asphalt Bed */}
                    <line
                      x1={from.x}
                      y1={from.y}
                      x2={to.x}
                      y2={to.y}
                      stroke={isActive ? '#1e293b' : '#0f172a'}
                      strokeWidth={strokeWidth + 4}
                      strokeLinecap="round"
                    />

                    {/* Colored Load Highway Band */}
                    <line
                      x1={from.x}
                      y1={from.y}
                      x2={to.x}
                      y2={to.y}
                      stroke={strokeColor}
                      strokeWidth={strokeWidth}
                      strokeLinecap="round"
                      strokeDasharray={isActive ? undefined : '4 3'}
                    />

                    {/* Direction Flow Chevrons for Active Route */}
                    {isActive && (
                      <g transform={`translate(${midX}, ${midY}) rotate(${angle})`}>
                        <polygon points="-5,-4 5,0 -5,4" fill="#ffffff" opacity={0.85} />
                      </g>
                    )}

                    {/* Section 3.3: Congestion Hazard Warning Pin at midpoint */}
                    {isBottleneck && isCongested && (
                      <g
                        transform={`translate(${midX}, ${midY - 24})`}
                        className="hazard-pin-group"
                        style={{ cursor: 'pointer' }}
                      >
                        <title>CONGESTION HAZARD: Link utilization exceeds 80% threshold</title>
                        <circle cx={0} cy={0} r={10} fill="#ef4444" className="hazard-pin-pulse" />
                        <text
                          y={3.5}
                          textAnchor="middle"
                          fill="#ffffff"
                          fontSize="9.5"
                          fontWeight="700"
                          fontFamily="var(--mono)"
                        >
                          !
                        </text>
                      </g>
                    )}

                    {/* Link Load Pill ("x/cap") */}
                    {!link.host_link && (
                      <g transform={`translate(${midX}, ${midY - 8})`}>
                        <title>
                          {isSimulated
                            ? `source = simulated (${key}: ${usedMbps.toFixed(1)} / ${cap} Mbps)`
                            : hasLiveSamples
                            ? `telemetry_csv · ${key}: ${usedMbps.toFixed(1)} / ${cap} Mbps`
                            : `telemetry_csv · no samples for ${key}`}
                        </title>
                        <rect
                          x={-22}
                          y={-8}
                          width={44}
                          height={15}
                          rx={3}
                          fill="rgba(15, 23, 42, 0.90)"
                          stroke={strokeColor}
                          strokeWidth={1}
                        />
                        <text
                          y={3}
                          textAnchor="middle"
                          fill="#f8fafc"
                          fontSize="8.5"
                          fontFamily="var(--mono)"
                          fontWeight="600"
                        >
                          {!isSimulated && !hasLiveSamples ? `--/${cap}` : `${usedMbps.toFixed(1)}/${cap}`}
                        </text>
                      </g>
                    )}
                  </g>
                )
              })}
            </g>

            {/* Dynamic Highway Vehicles */}
            {hasActiveRoute && (
              <g>
                {vehicles.map((v) => {
                  const pos = getVehiclePos(v.progress)
                  if (!pos) return null
                  return (
                    <g
                      key={`veh-${v.id}`}
                      transform={`translate(${pos.x}, ${pos.y}) rotate(${pos.angle})`}
                    >
                      <rect
                        x={-9}
                        y={-4.5}
                        width={18}
                        height={9}
                        rx={2.5}
                        fill={v.color}
                        stroke="#0f172a"
                        strokeWidth={1.5}
                      />
                      <circle cx={9} cy={-2.5} r={1.2} fill="#fef08a" />
                      <circle cx={9} cy={2.5} r={1.2} fill="#fef08a" />
                    </g>
                  )
                })}
              </g>
            )}

            {/* Nodes: Switches and Hosts */}
            <g>
              {currentDef.switches.map((name) => {
                const pos = positions.get(name)
                if (!pos) return null
                const isSelected = selected === name
                const isPathMember = activePath.includes(name)

                return (
                  <g
                    key={name}
                    className="graph__node"
                    data-selected={isSelected}
                    data-active={isPathMember}
                    transform={`translate(${pos.x}, ${pos.y})`}
                    onClick={() => onSelect(isSelected ? null : name)}
                    style={{ cursor: 'pointer' }}
                  >
                    <circle
                      r={18}
                      fill="var(--surface-raised)"
                      stroke={isSelected ? '#38bdf8' : isPathMember ? 'var(--amber)' : 'var(--rule-bright)'}
                      strokeWidth={isSelected ? 3 : 2}
                    />
                    <circle r={7} fill={isPathMember ? 'var(--amber)' : '#38bdf8'} />
                    <text
                      className="graph__label"
                      y={31}
                      textAnchor="middle"
                      fill={isSelected ? '#38bdf8' : 'var(--ink)'}
                      fontWeight="600"
                      fontSize="12"
                    >
                      {name}
                    </text>
                  </g>
                )
              })}

              {/* Hosts with Offset IP Under Host Box (Section 3.3) */}
              {currentDef.hosts.map((host) => {
                const pos = positions.get(host.name)
                if (!pos) return null
                const isSelected = selected === host.name
                const isPathMember = activePath.includes(host.name)

                return (
                  <g
                    key={host.name}
                    className="graph__node graph__node--host"
                    data-selected={isSelected}
                    transform={`translate(${pos.x}, ${pos.y})`}
                    onClick={() => onSelect(isSelected ? null : host.name)}
                    style={{ cursor: 'pointer' }}
                  >
                    <rect
                      x={-18}
                      y={-14}
                      width={36}
                      height={28}
                      rx={4}
                      fill="var(--surface)"
                      stroke={isSelected ? '#34d399' : isPathMember ? '#34d399' : 'var(--rule-bright)'}
                      strokeWidth={isSelected ? 2.5 : 1.5}
                    />
                    <text
                      className="graph__label"
                      y={4}
                      textAnchor="middle"
                      fill="#34d399"
                      fontWeight="600"
                      fontSize="12"
                    >
                      {host.name}
                    </text>
                    <text
                      className="graph__ip"
                      y={26}
                      textAnchor="middle"
                      fill="var(--ink-muted)"
                      fontSize="10"
                      fontFamily="var(--mono)"
                    >
                      {host.ip}
                    </text>
                  </g>
                )
              })}
            </g>
          </svg>
        </div>
      )}
    </div>
  )
}
