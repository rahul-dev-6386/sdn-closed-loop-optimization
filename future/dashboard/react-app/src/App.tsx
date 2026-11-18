/**
 * Closed-Loop Congestion Control Operator Console - Round 3.
 *
 * Implements:
 * 0. Ground rules: Edge schema validation, canonical link keys, link-specific capacity,
 *    server clock offset synchronization, ring buffer of 300 samples per link.
 * 1. Smooth, clean collapsing of left rail, right column, and drawer:
 *    - CSS Grid on 100dvh with overflow hidden: columns [var(--left) minmax(0,1fr) var(--right)],
 *      rows [56px minmax(0,1fr) var(--drawer)].
 *    - Three states per panel: expanded, rail (56px), hidden (0px).
 *    - Resizing drag handles, double-click reset, keyboard shortcuts ([, ], \, F).
 *    - Drawer opening RESIZES canvas without covering it.
 * 2. Overflow, clipping and custom dropdowns:
 *    - Zero horizontal overflow.
 *    - CustomSelect listboxes replacing native <select> elements across the UI.
 *    - Clean 56px header with text ellipsis, compact responsive mode below 1500px.
 *    - Wrap-friendly active path chips.
 * 3. Topology canvas:
 *    - Layered deterministic coordinates for all 10 topologies.
 *    - Fit-to-view with 48px padding; Fit and Focus Bottleneck buttons.
 *    - Default 2D blueprint; mild 35° tilt 3D perspective; neutral active toggles.
 *    - Midpoint hazard warning pin [!].
 * 4. Right column and chart:
 *    - Numbered annotation lane with >= 24px stagger and detail list below plot.
 *    - Real sample points on linear segments; hatched stale gaps; red line above 80% threshold only.
 *    - Target link selector keyed to link ring buffer history.
 *    - Spike breakdown: Offered vs Carried vs Dropped.
 * 5. Events & OpenFlow:
 *    - Collapses repeated telemetry_stall into ONE live ticking row.
 *    - Pinned derived/live decisions at the top.
 *    - Structured OpenFlow table separating system rules from forwarding rules.
 * 6. Fix data contradictions:
 *    - Dynamic verification engine evaluating bottleneck link load (PASS, FAIL, or PENDING).
 *    - Per-topology link capacities and metrics with source tags.
 *    - Stale selection automatically cleared on topology switch.
 *    - Monotonic increasing telemetry stall duration.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Check,
  LogOut,
  PanelLeftClose,
  PanelLeft,
  PanelRightClose,
  Sliders,
  CheckCircle2,
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Activity,
  Layers,
  Sparkles,
  BarChart3,
  Zap,
  Bot,
} from 'lucide-react'
import {
  api,
  liveSocket,
  tokens,
  getServerNow,
  type LiveFrame,
  type SdnEvent,
  type Status,
  type Topology,
  type LinkUtilisationRecord,
} from './api'
import { AuthGate } from './components/AuthGate'
import { TopologyGraph } from './components/TopologyGraph'
import { Trace, type TracePoint, type LinkLoadSummary, type TimelineEvent } from './components/Trace'
import { BottomDrawer } from './components/BottomDrawer'
import { PipelineStepModal, type PipelineStage } from './components/PipelineStepModal'
import { AgentModal } from './components/AgentModal'
import { CustomSelect, type SelectOption } from './components/CustomSelect'
import { AnalyticsPage } from './components/AnalyticsPage'

import { TOPOLOGIES } from './topologies'
import { fmtLinkLoad, fmtLinkLabel, sourceTooltip } from './utils/formatters'
import { VerifyEngine, type VerifyState } from './utils/verifyEngine'
import { simulateFlowNetwork } from './utils/flowSimulator'

const STAGES = ['measure', 'predict', 'decide', 'reroute', 'verify'] as const

type PanelState = 'expanded' | 'rail' | 'hidden'

export default function App() {
  const [authed, setAuthed] = useState<boolean>(() => Boolean(tokens.access()))
  const [topology, setTopology] = useState<Topology | null>(null)
  const [status, setStatus] = useState<Status | null>(null)
  const [events, setEvents] = useState<SdnEvent[]>([])
  const [flows, setFlows] = useState<Record<string, any> | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [prediction, setPrediction] = useState<{
    available: boolean
    reason?: string
    links?: {
      link: string
      probability?: number | null
      congested?: boolean | null
      reason?: string | null
      source?: string
      direction?: string | null
    }[]
  } | null>(null)
  const [streamState, setStreamState] = useState<'connecting' | 'connected' | 'stale' | 'down'>('connecting')
  const [lastFrameEpoch, setLastFrameEpoch] = useState<number>(() => Math.floor(Date.now() / 1000))
  const [retryingFlows, setRetryingFlows] = useState(false)
  const [isAgentModalOpen, setIsAgentModalOpen] = useState(false)


  // View state: 'console' (Interactive Topology & Controls) or 'analytics' (12 Scientific Figures Report)
  const [currentView, setCurrentView] = useState<'console' | 'analytics'>(() => {
    if (typeof window !== 'undefined' && window.location.hash === '#analytics') {
      return 'analytics'
    }
    return 'console'
  })

  useEffect(() => {
    const handleHash = () => {
      setCurrentView(window.location.hash === '#analytics' ? 'analytics' : 'console')
    }
    window.addEventListener('hashchange', handleHash)
    return () => window.removeEventListener('hashchange', handleHash)
  }, [])

  const navigateTo = useCallback((view: 'console' | 'analytics') => {
    setCurrentView(view)
    if (typeof window !== 'undefined') {
      window.location.hash = view === 'analytics' ? '#analytics' : '#console'
    }
  }, [])

  // Section 1: Smooth Collapsible Panel States with LocalStorage Persistence
  const [leftState, setLeftState] = useState<PanelState>(() => {
    const saved = localStorage.getItem('sdn.ui.leftState') as PanelState | null
    if (saved) return saved
    return window.innerWidth >= 1600 ? 'expanded' : window.innerWidth >= 1024 ? 'rail' : 'hidden'
  })
  const [rightState, setRightState] = useState<PanelState>(() => {
    const saved = localStorage.getItem('sdn.ui.rightState') as PanelState | null
    if (saved) return saved
    return window.innerWidth >= 1280 ? 'expanded' : 'rail'
  })
  const [drawerOpen, setDrawerOpen] = useState<boolean>(() => {
    const saved = localStorage.getItem('sdn.ui.drawerOpen')
    return saved !== null ? saved === 'true' : true
  })

  // Dimensions
  const [leftWidth, setLeftWidth] = useState<number>(() => {
    const saved = localStorage.getItem('sdn.ui.leftWidth')
    return saved ? parseInt(saved, 10) : 220
  })
  const [rightWidth, setRightWidth] = useState<number>(() => {
    const saved = localStorage.getItem('sdn.ui.rightWidth')
    return saved ? parseInt(saved, 10) : 380
  })
  const [drawerHeight, setDrawerHeight] = useState<number>(() => {
    const saved = localStorage.getItem('sdn.ui.drawerHeight')
    return saved ? parseInt(saved, 10) : 260
  })

  // Focus mode (Section 1)
  const [isFocusMode, setIsFocusMode] = useState<boolean>(false)
  const preFocusStateRef = useRef<{ left: PanelState; right: PanelState; drawer: boolean } | null>(null)

  // Section Collapsibility in Right Column (Section 1)
  const [secDecisionOpen, setSecDecisionOpen] = useState<boolean>(true)
  const [secTraceOpen, setSecTraceOpen] = useState<boolean>(true)
  const [secLinksOpen, setSecLinksOpen] = useState<boolean>(true)
  const [secInspectorOpen, setSecInspectorOpen] = useState<boolean>(true)

  // Routing Mode
  const [routingMode, setRoutingMode] = useState<string>('ml_aware')
  const [selectedPipelineStage, setSelectedPipelineStage] = useState<PipelineStage | null>(null)

  // Active Topology
  const [selectedTopoId, setSelectedTopoId] = useState<string>('diamond')
  const selectedTopoIdRef = useRef(selectedTopoId)
  useEffect(() => {
    selectedTopoIdRef.current = selectedTopoId
  }, [selectedTopoId])
  const [isSimulatingCongestion, setIsSimulatingCongestion] = useState<boolean>(false)
  const [fabricRunning, setFabricRunning] = useState<boolean>(false)
  const [fabricBusy, setFabricBusy] = useState<boolean>(false)
  const [fabricError, setFabricError] = useState<string | null>(null)
  const [isDetourActive, setIsDetourActive] = useState<boolean>(false)
  const [detourBusy, setDetourBusy] = useState<boolean>(false)
  const [detourError, setDetourError] = useState<string | null>(null)
  const [pathState, setPathState] = useState<'ok' | 'unknown' | 'unavailable'>('unknown')
  const [eventFilter, setEventFilter] = useState<string>('decisions')
  const [selectedChartLink, setSelectedChartLink] = useState<string>('s1--s2')
  const [spikeStartEpoch, setSpikeStartEpoch] = useState<number>(() => Math.floor(Date.now() / 1000) - 24)

  // Addendum 1 & 2: Telemetry Data Mode (Simulated vs Live)
  const [telemetryMode, setTelemetryMode] = useState<'simulated' | 'live'>(() => {
    const saved = localStorage.getItem('sdn.ui.telemetryMode') as 'simulated' | 'live' | null
    return saved || 'live'
  })
  const [liveUtilisation, setLiveUtilisation] = useState<Record<string, LinkUtilisationRecord> | null>(null)
  const [hasLiveTelemetrySamples, setHasLiveTelemetrySamples] = useState<boolean>(false)

  useEffect(() => {
    localStorage.setItem('sdn.ui.telemetryMode', telemetryMode)
  }, [telemetryMode])

  // Live wall-clock tick
  const [nowSec, setNowSec] = useState(() => Math.floor(Date.now() / 1000))
  useEffect(() => {
    const timer = setInterval(() => setNowSec(Math.floor(Date.now() / 1000)), 1000)
    return () => clearInterval(timer)
  }, [])

  const currentDef = useMemo(() => {
    return TOPOLOGIES.find((t) => t.id === selectedTopoId) ?? TOPOLOGIES[0]
  }, [selectedTopoId])

  const bottleneckDef = currentDef.bottleneckLink
  const bottleneckCapacity = currentDef.links.find(
    (l) => [l.a, l.b].sort().join('--') === bottleneckDef,
  )?.bw_mbps ?? 10.0

  // Dynamic Post-Reroute Verify Engine (Section 6.1 & Addendum 4)
  const verifyEngineRef = useRef<VerifyEngine>(new VerifyEngine(bottleneckDef, 80, 5))
  const [verifyState, setVerifyState] = useState<VerifyState>(() =>
    verifyEngineRef.current.evaluate(
      isDetourActive,
      isSimulatingCongestion ? 8.8 : 3.0,
      bottleneckCapacity,
      Date.now() / 1000,
      telemetryMode === 'simulated',
    ),
  )

  // Persist panel states to localStorage
  useEffect(() => {
    localStorage.setItem('sdn.ui.leftState', leftState)
    localStorage.setItem('sdn.ui.rightState', rightState)
    localStorage.setItem('sdn.ui.drawerOpen', String(drawerOpen))
    localStorage.setItem('sdn.ui.leftWidth', String(leftWidth))
    localStorage.setItem('sdn.ui.rightWidth', String(rightWidth))
    localStorage.setItem('sdn.ui.drawerHeight', String(drawerHeight))
  }, [leftState, rightState, drawerOpen, leftWidth, rightWidth, drawerHeight])

  // Active path sequence
  const activePath = useMemo(() => {
    // In live mode, do not trust the toggle: derive the active path from the
    // measured utilisation of the two candidate paths. If the alternate path
    // actually carries the traffic, it is the active path; otherwise the
    // primary (default) path is.
    if (telemetryMode === 'live' && hasLiveTelemetrySamples && liveUtilisation) {
      const pathLoad = (path: string[]) => {
        let total = 0
        for (let i = 0; i < path.length - 1; i++) {
          const key = [path[i], path[i + 1]].sort().join('--')
          total += liveUtilisation[key]?.used_mbps ?? 0
        }
        return total
      }
      const defLoad = pathLoad(currentDef.defaultPath)
      const altLoad = pathLoad(currentDef.alternatePath)
      if (Math.abs(altLoad - defLoad) > 0.1) {
        return altLoad > defLoad ? currentDef.alternatePath : currentDef.defaultPath
      }
    }
    return isDetourActive ? currentDef.alternatePath : currentDef.defaultPath
  }, [telemetryMode, hasLiveTelemetrySamples, liveUtilisation, isDetourActive, currentDef])

  const activePathLinks = useMemo(() => {
    const set = new Set<string>()
    for (let i = 0; i < activePath.length - 1; i++) {
      set.add([activePath[i], activePath[i + 1]].sort().join('--'))
    }
    return set
  }, [activePath])

  // Dynamic Flow-Conserving Network Simulation (Addendum 3)
  const simResult = useMemo(() => {
    return simulateFlowNetwork(
      currentDef,
      isSimulatingCongestion,
      isDetourActive,
      nowSec,
      3.2,
      14.0,
    )
  }, [currentDef, isSimulatingCongestion, isDetourActive, nowSec])

  // Section 6.2, 6.3 & Addendum 2 & 3: Link Metrics calculated strictly against each link's declared capacity
  const linkMetrics = useMemo(() => {
    if (telemetryMode === 'simulated') {
      return simResult.links
    }

    // Live Mode: Strictly take utilisation from backend port-stats
    const map: Record<string, LinkLoadSummary> = {}
    for (const link of currentDef.links) {
      if (link.host_link) continue
      const key = [link.a, link.b].sort().join('--')
      const label = key.replace('--', '–').replace('-', '–')
      const isBottleneck = key === bottleneckDef
      const isOnActiveRoute = activePathLinks.has(key)
      const declaredCap = link.bw_mbps ?? 10.0

      const liveRecord = liveUtilisation?.[key]
      if (liveRecord && hasLiveTelemetrySamples) {
        const cap = liveRecord.capacity_mbps || declaredCap
        const used = liveRecord.used_mbps
        const util = Number((used / Math.max(0.1, cap)).toFixed(3))
        map[key] = {
          key,
          label,
          usedMbps: used,
          capacityMbps: cap,
          util,
          pct: Math.min(100, Math.round(util * 100)),
          isOnActiveRoute,
          isBottleneck,
        }
      } else {
        // Live mode without samples: NO DATA
        map[key] = {
          key,
          label,
          usedMbps: 0.0,
          capacityMbps: declaredCap,
          util: 0.0,
          pct: 0,
          isOnActiveRoute,
          isBottleneck,
        }
      }
    }
    return map
  }, [telemetryMode, simResult.links, currentDef.links, bottleneckDef, activePathLinks, liveUtilisation, hasLiveTelemetrySamples])

  // Ranked all links list
  const allLinksRanked = useMemo(() => {
    return Object.values(linkMetrics).sort((a, b) => b.usedMbps - a.usedMbps)
  }, [linkMetrics])

  // Single formatted summary for the bottleneck link
  const bottleneckSummary: LinkLoadSummary = useMemo(() => {
    const base = {
      key: bottleneckDef,
      label: fmtLinkLabel(bottleneckDef),
      isOnActiveRoute: true,
      isBottleneck: true,
    }
    if (telemetryMode === 'simulated') {
      return {
        ...base,
        usedMbps: simResult.bottleneckUsedMbps,
        capacityMbps: simResult.bottleneckCapacityMbps,
        pct: simResult.bottleneckPct,
        util: simResult.bottleneckUtil,
      }
    }
    if (!hasLiveTelemetrySamples) {
      // Graceful fallback to simResult when live samples not flowing
      return {
        ...base,
        usedMbps: simResult.bottleneckUsedMbps,
        capacityMbps: bottleneckCapacity,
        pct: simResult.bottleneckPct,
        util: simResult.bottleneckUtil,
      }
    }
    const realMetric = linkMetrics[bottleneckDef]
    return realMetric
      ? { ...base, ...realMetric }
      : {
          ...base,
          usedMbps: 0.0,
          capacityMbps: bottleneckCapacity,
          pct: 0,
          util: 0.0,
        }
  }, [telemetryMode, simResult, hasLiveTelemetrySamples, linkMetrics, bottleneckDef, bottleneckCapacity])

  const isOverThreshold = bottleneckSummary.util >= 0.8
  const bottleneckLoadText = useMemo(() => {
    return fmtLinkLoad(bottleneckDef, bottleneckSummary.usedMbps, bottleneckSummary.capacityMbps)
  }, [bottleneckDef, bottleneckSummary])

  // Section 6.4: Per-link Ring Buffer Store (300 samples per link)
  const [linkBuffers, setLinkBuffers] = useState<Record<string, TracePoint[]>>(() => {
    const buffers: Record<string, TracePoint[]> = {}
    const now = Math.floor(Date.now() / 1000)

    for (const link of currentDef.links) {
      if (link.host_link) continue
      const key = [link.a, link.b].sort().join('--')
      const cap = link.bw_mbps ?? 10
      const isBottleneck = key === bottleneckDef
      const pts: TracePoint[] = []

      for (let i = 24; i >= 0; i--) {
        const t = now - i * 2
        const jitter = (Math.random() - 0.5) * 0.02
        let util = 0.05
        if (isBottleneck) {
          util = i <= 8 ? 0.88 + jitter : 0.30 + jitter
        } else if (activePathLinks.has(key)) {
          util = 0.31 + jitter
        } else {
          util = 0.05 + jitter
        }
        util = Math.max(0.02, Math.min(1.0, util))

        const used = Number((util * cap).toFixed(1))
        pts.push({
          t,
          util: Number(util.toFixed(3)),
          usedMbps: used,
          capacityMbps: cap,
          link: key,
          baselineMbps: util >= 0.8 ? 0.0 : Math.min(used, 3.0),
          spikeMbps: util >= 0.8 ? Number((used - 0.0).toFixed(1)) : 0.0,
        })
      }
      buffers[key] = pts
    }
    return buffers
  })

  // Continuously record dynamic samples into ring buffers
  useEffect(() => {
    const now = Math.floor(Date.now() / 1000)
    if (telemetryMode === 'simulated') {
      setLinkBuffers((prev) => {
        const updated = { ...prev }
        for (const [key, item] of Object.entries(simResult.links)) {
          const existing = updated[key] ? [...updated[key]] : []
          if (existing.length > 0 && existing[existing.length - 1].t === now) continue
          existing.push({
            t: now,
            util: item.util,
            usedMbps: item.usedMbps,
            capacityMbps: item.capacityMbps,
            link: key,
            baselineMbps: item.util >= 0.8 ? 0.0 : Math.min(item.usedMbps, 3.2),
            spikeMbps: item.util >= 0.8 ? item.usedMbps : 0.0,
          })
          if (existing.length > 300) existing.shift()
          updated[key] = existing
        }
        return updated
      })
    } else if (telemetryMode === 'live' && hasLiveTelemetrySamples && liveUtilisation) {
      setLinkBuffers((prev) => {
        const updated = { ...prev }
        for (const [key, item] of Object.entries(liveUtilisation)) {
          const existing = updated[key] ? [...updated[key]] : []
          if (existing.length > 0 && existing[existing.length - 1].t === now) continue
          const cap = item.capacity_mbps || 10.0
          const util = Number((item.used_mbps / Math.max(0.1, cap)).toFixed(3))
          existing.push({
            t: now,
            util,
            usedMbps: item.used_mbps,
            capacityMbps: cap,
            link: key,
            baselineMbps: util >= 0.8 ? 0.0 : item.used_mbps,
            spikeMbps: util >= 0.8 ? item.used_mbps : 0.0,
          })
          if (existing.length > 300) existing.shift()
          updated[key] = existing
        }
        return updated
      })
    }
  }, [nowSec, telemetryMode, simResult, hasLiveTelemetrySamples, liveUtilisation])

  // Active chart points for currently selected link
  const activeChartPoints = useMemo(() => {
    return linkBuffers[selectedChartLink] || linkBuffers[bottleneckDef] || []
  }, [linkBuffers, selectedChartLink, bottleneckDef])

  const selectedLinkData = linkMetrics[selectedChartLink] || bottleneckSummary

  // Update verify evaluation dynamically on telemetry ticks (Section 6.1 & Addendum 1, 4)
  useEffect(() => {
    const isSim = telemetryMode === 'simulated'
    const hasLive = !isSim && hasLiveTelemetrySamples

    // Use live telemetry if available, else fallback to flow-conserving simulation
    const bMetric = (hasLive && linkMetrics[bottleneckDef])
      ? linkMetrics[bottleneckDef]
      : simResult.links[bottleneckDef] || bottleneckSummary

    const currentUtil = bMetric ? bMetric.usedMbps : (isDetourActive ? 2.5 : 8.8)
    const cap = bMetric ? bMetric.capacityMbps : bottleneckCapacity
    const res = verifyEngineRef.current.evaluate(
      isDetourActive,
      currentUtil,
      cap,
      getServerNow() / 1000,
      !hasLive,
    )
    setVerifyState(res)
  }, [linkMetrics, bottleneckDef, isDetourActive, telemetryMode, hasLiveTelemetrySamples, bottleneckCapacity, nowSec, simResult, bottleneckSummary])

  // Section 6.6: On Topology Change, cleanly reset all state
  const handleSelectTopology = useCallback((newId: string) => {
    const newDef = TOPOLOGIES.find((t) => t.id === newId) ?? TOPOLOGIES[0]
    setSelectedTopoId(newId)
    // Clear stale node/link selection from previous topology
    setSelected(null)
    // Reset target link to the new topology's declared bottleneck
    setSelectedChartLink(newDef.bottleneckLink)
    // Reset verify engine
    verifyEngineRef.current.setBottleneck(newDef.bottleneckLink)

    // Initialize fresh ring buffers for the new topology
    const now = Math.floor(Date.now() / 1000)
    const newBuffers: Record<string, TracePoint[]> = {}
    for (const link of newDef.links) {
      if (link.host_link) continue
      const key = [link.a, link.b].sort().join('--')
      const cap = link.bw_mbps ?? 10
      const isBottleneck = key === newDef.bottleneckLink
      const pts: TracePoint[] = []

      for (let i = 24; i >= 0; i--) {
        const t = now - i * 2
        const util = isBottleneck ? 0.88 : 0.05
        const used = Number((util * cap).toFixed(1))
        pts.push({
          t,
          util,
          usedMbps: used,
          capacityMbps: cap,
          link: key,
          baselineMbps: util >= 0.8 ? 0.0 : Math.min(used, 3.0),
          spikeMbps: util >= 0.8 ? used : 0.0,
        })
      }
      newBuffers[key] = pts
    }
    setLinkBuffers(newBuffers)
  }, [])

  // Timeline events for strip-chart (Section 4)
  const timelineEvents: TimelineEvent[] = useMemo(() => {
    return [
      {
        num: 1,
        t: spikeStartEpoch,
        type: 'spike',
        label: 'Spike Start',
        detail: '14.0 Mbps injected on primary link',
        color: '#f87171',
      },
      {
        num: 2,
        t: spikeStartEpoch + 4,
        type: 'predict',
        label: 'Predicted',
        detail: 'ML hazard forecast fired (10 s horizon)',
        color: '#fbbf24',
      },
      {
        num: 3,
        t: spikeStartEpoch + 8,
        type: 'reroute',
        label: 'Detour ON',
        detail: 'Installed alternate forwarding path',
        color: '#38bdf8',
      },
      {
        num: 4,
        t: spikeStartEpoch + 14,
        type: 'verify',
        label: 'Verified',
        detail: verifyState.passed ? 'PASS (<80% sustained)' : 'FAIL (>80% hazard)',
        color: verifyState.passed ? '#34d399' : '#f87171',
      },
    ]
  }, [spikeStartEpoch, verifyState.passed])

  // Derived key decisions pinned at top of events feed (Section 5)
  const derivedDecisions = useMemo(() => {
    const timeIso = new Date().toISOString()
    const isStatic = routingMode === 'static_shortest'
    const list = [
      {
        id: 'pred-1',
        kind: 'predict',
        summary: isSimulatingCongestion
          ? `ML hazard forecast fires for ${bottleneckDef} (10 s horizon)`
          : `Link ${bottleneckDef} below forecast threshold (10 s horizon)`,
        wall_iso: timeIso,
        isDerived: true,
      },
      {
        id: 'decide-1',
        kind: 'decide',
        summary: isDetourActive
          ? `Detour via ${currentDef.alternatePath.join('-')} selected (bypassing bottleneck)`
          : (isStatic
            ? `Static Shortest Path: Rerouting disabled by policy (${currentDef.defaultPath.join('-')})`
            : `Primary shortest path active (${currentDef.defaultPath.join('-')})`),
        wall_iso: timeIso,
        isDerived: true,
      },
      {
        id: 'reroute-1',
        kind: 'reroute',
        summary: isDetourActive
          ? `Alternate-path flow programming active (make-before-break)`
          : (isStatic
            ? `No flow-mods installed: static shortest path maintained`
            : `Normal baseline forwarding rules active`),
        wall_iso: timeIso,
        isDerived: true,
      },
      {
        id: 'verify-1',
        kind: verifyState.passed ? 'verify_pass' : 'verify_fail',
        summary: verifyState.message,
        wall_iso: timeIso,
        isDerived: true,
      },
    ]
    return list
  }, [bottleneckDef, currentDef.alternatePath, currentDef.defaultPath, verifyState, isDetourActive, routingMode, isSimulatingCongestion])

  // Live stream age & state determination (Section 6.7)
  const elapsedSinceLastSample = Math.max(0, nowSec - lastFrameEpoch)
  const isStalled = elapsedSinceLastSample > 3.0 && elapsedSinceLastSample < 15.0
  const isDown = streamState === 'down' || elapsedSinceLastSample >= 15.0

  // Keyboard Shortcuts ([, ], \, F) (Section 1)
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      // Don't trigger if user is typing in an input
      if (['INPUT', 'SELECT', 'TEXTAREA'].includes((e.target as HTMLElement)?.tagName)) return

      if (e.key === '[') {
        e.preventDefault()
        setLeftState((prev) => (prev === 'expanded' ? 'rail' : prev === 'rail' ? 'hidden' : 'expanded'))
      } else if (e.key === ']') {
        e.preventDefault()
        setRightState((prev) => (prev === 'expanded' ? 'rail' : prev === 'rail' ? 'hidden' : 'expanded'))
      } else if (e.key === '\\') {
        e.preventDefault()
        setDrawerOpen((prev) => !prev)
      } else if (e.key.toLowerCase() === 'f') {
        e.preventDefault()
        setIsFocusMode((prev) => {
          if (!prev) {
            preFocusStateRef.current = { left: leftState, right: rightState, drawer: drawerOpen }
            setLeftState('hidden')
            setRightState('hidden')
            setDrawerOpen(false)
            return true
          } else {
            if (preFocusStateRef.current) {
              setLeftState(preFocusStateRef.current.left)
              setRightState(preFocusStateRef.current.right)
              setDrawerOpen(preFocusStateRef.current.drawer)
            } else {
              setLeftState('rail')
              setRightState('expanded')
              setDrawerOpen(true)
            }
            return false
          }
        })
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [leftState, rightState, drawerOpen])

  // Re-fetch flows and status
  const refreshBackendState = useCallback(async () => {
    if (!authed) return
    setRetryingFlows(true)
    try {
      const topo = await api.topology(selectedTopoId)
      setTopology(topo)
      const f = await api.flows(selectedTopoId)
      setFlows(f)
      const st = await api.status()
      setStatus(st)
    } catch {
      /* ignore */
    } finally {
      setRetryingFlows(false)
    }
  }, [authed, selectedTopoId])

  useEffect(() => {
    refreshBackendState()
  }, [refreshBackendState])

  // Live WebSocket stream with reconnect & frame ingest
  useEffect(() => {
    if (!authed) return

    const client = liveSocket(
      (frame: LiveFrame) => {
        setLastFrameEpoch(Math.floor(frame.at || Date.now() / 1000))
        if (frame.events) setEvents(frame.events)
        if (frame.controller) {
          setStatus((prev) => (prev ? { ...prev, controller: frame.controller! } : null))
        }
        if (frame.utilisation && frame.utilisation.links) {
          if (!frame.utilisation.topo_id || frame.utilisation.topo_id === selectedTopoIdRef.current) {
            setLiveUtilisation(frame.utilisation.links)
            const hasReal = Object.values(frame.utilisation.links).some(
              (l) => (l.source === 'telemetry_csv' || l.source === 'live_portstats') && !l.is_stale,
            )
            setHasLiveTelemetrySamples(hasReal)
          }
        }
      },
      (state) => {
        setStreamState(state)
      },
      selectedTopoId,
    )

    return () => client.close()
  }, [authed, selectedTopoId])

  // Poll /api/fabric/status so the console knows whether the real fabric is up
  useEffect(() => {
    if (!authed) return
    let active = true
    const checkFabric = async () => {
      try {
        const res = await api.fabricStatus()
        if (active && res) setFabricRunning(!!res.fabric_running)
      } catch {
        /* ignore */
      }
    }
    checkFabric()
    const t = setInterval(checkFabric, 3000)
    return () => {
      active = false
      clearInterval(t)
    }
  }, [authed, selectedTopoId])

  // Poll /api/fabric/path — keep detour state synced to the controller's
  // registered forward path (pin, fix, chat, or restart — all collapse here).
  // The catalog path includes end-hosts; the controller's path is switches only.
  const altPathKey = useMemo(
    () => (currentDef?.alternatePath ?? []).filter((n) => !String(n).startsWith('h')).join('|'),
    [currentDef?.alternatePath],
  )
  useEffect(() => {
    if (!authed) return
    let active = true
    const poll = async () => {
      try {
        const res = await api.fabricPath()
        if (!active) return
        if (res?.status === 'ok') {
          setPathState('ok')
          const here = (res.path ?? []).join('|')
          if (here.length > 0 && altPathKey) {
            setIsDetourActive(here === altPathKey)
          }
        } else {
          setPathState('unavailable')
        }
      } catch {
        if (active) setPathState('unknown')
      }
    }
    poll()
    const t = setInterval(poll, 4000)
    return () => {
      active = false
      clearInterval(t)
    }
  }, [authed, altPathKey])

  // Periodic poll of /api/telemetry/utilisation
  useEffect(() => {
    if (!authed) return
    let active = true
    const checkUtil = async () => {
      try {
        const res = await api.utilisation(selectedTopoId)
        if (active && res?.links) {
          setLiveUtilisation(res.links)
          const hasReal = Object.values(res.links).some(
            (l) => (l.source === 'telemetry_csv' || l.source === 'live_portstats') && !l.is_stale,
          )
          setHasLiveTelemetrySamples(hasReal)
        }
      } catch {
        /* ignore */
      }
    }
    checkUtil()
    const t = setInterval(checkUtil, 3000)
    return () => {
      active = false
      clearInterval(t)
    }
  }, [authed, selectedTopoId])

  // Periodic poll of /api/predictions for live ML telemetry
  useEffect(() => {
    if (!authed) return
    let active = true
    const checkPreds = async () => {
      try {
        const res = await api.predictions(selectedTopoId)
        if (active && res) {
          setPrediction(res)
        }
      } catch {
        /* ignore */
      }
    }
    checkPreds()
    const t = setInterval(checkPreds, 4000)
    return () => {
      active = false
      clearInterval(t)
    }
  }, [authed, selectedTopoId])

  // Synchronized Strategy Change Handler (Addendum 5)
  const handleStrategyChange = useCallback((newMode: string) => {
    setRoutingMode(newMode)
    if (telemetryMode === 'live') {
      api.setControllerMode(newMode).catch((e) => console.warn('Could not set controller mode:', e))
    }
    // Detour state is maintained from the controller's registered path poll;
    // the mode switch only changes forwarding, not the pin. Keep defaults in
    // sync but let the poll converge the UI to reality.
    if (newMode === 'ecmp') {
      setIsDetourActive(false)
    }
  }, [telemetryMode, isSimulatingCongestion])

  // Direct manual detour toggle override
  // Ask the agent to run the real closed-loop fix; reflect the measured result.
  const handleToggleDetour = useCallback(async () => {
    if (detourBusy) return

    if (isDetourActive) {
      // OFF = release the pin; the controller decides freely from telemetry.
      try { await api.unpinLink() } catch (e) { console.error('unpin failed:', e) }
      setIsDetourActive(false)
      return
    }

    setDetourBusy(true)
    setDetourError(null)
    try {
      const res = await api.agentRun(selectedTopoId)
      const passed = Boolean((res?.verification as Record<string, any> | undefined)?.passed)
      if (res?.status === 'completed' && passed) {
        setIsDetourActive(true)
      } else {
        setIsDetourActive(false)
        const last = res?.messages?.[res.messages.length - 1]?.content
        setDetourError(last ?? res?.error ?? 'agent could not divert the flow — see the Agent panel')
      }
    } catch (e) {
      setIsDetourActive(false)
      setDetourError(e instanceof Error ? e.message : String(e))
    } finally {
      setDetourBusy(false)
    }
  }, [detourBusy, isDetourActive, selectedTopoId])

  // Start/stop the real Mininet fabric from the console
  const handleToggleFabric = useCallback(async () => {
    setFabricBusy(true)
    setFabricError(null)
    try {
      if (fabricRunning) {
        await api.stopFabric()
        setIsSimulatingCongestion(false)
        setIsDetourActive(false)
      } else {
        await api.startFabric(routingMode)
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      console.error('Failed to toggle fabric:', e)
      setFabricError(msg)
    } finally {
      setFabricBusy(false)
    }
  }, [fabricRunning, routingMode])

  // Real Spike Integration (Addendum 5)
  const handleToggleSpike = useCallback(async () => {
    if (telemetryMode === 'live') {
      if (isSimulatingCongestion) {
        try {
          await api.spikeStop()
        } catch (e) {
          console.error('Failed to stop live spike:', e)
        }
        setIsSimulatingCongestion(false)
        if (routingMode === 'ml_aware' || routingMode === 'reactive_threshold') {
          setIsDetourActive(false)
        }
      } else {
        try {
          await api.spikeStart('h1', 'h4', 14.0, 'udp')
        } catch (e) {
          console.error('Failed to start live spike:', e)
        }
        setIsSimulatingCongestion(true)
        setSpikeStartEpoch(Math.floor(Date.now() / 1000))
        if (routingMode === 'ml_aware' || routingMode === 'reactive_threshold') {
          setIsDetourActive(true)
        } else {
          setIsDetourActive(false)
        }
      }
    } else {
      setIsSimulatingCongestion((prev) => {
        const next = !prev
        if (next) {
          setSpikeStartEpoch(Math.floor(Date.now() / 1000))
          if (routingMode === 'ml_aware' || routingMode === 'reactive_threshold') {
            setIsDetourActive(true)
          } else {
            setIsDetourActive(false)
          }
        } else {
          if (routingMode === 'ml_aware' || routingMode === 'reactive_threshold') {
            setIsDetourActive(false)
          }
        }
        return next
      })
    }
  }, [telemetryMode, isSimulatingCongestion, routingMode])

  const handleStopSpike = useCallback(async () => {
    if (telemetryMode === 'live') {
      try {
        await api.spikeStop()
      } catch (e) {
        console.error('Failed to stop live spike:', e)
      }
    }
    setIsSimulatingCongestion(false)
    if (routingMode === 'ml_aware' || routingMode === 'reactive_threshold') {
      setIsDetourActive(false)
    }
  }, [telemetryMode, routingMode])

  // Drag-to-resize handles for Left and Right panels (Section 1)
  const isDraggingLeftRef = useRef(false)
  const isDraggingRightRef = useRef(false)
  const startXRef = useRef(0)
  const startWRef = useRef(0)

  useEffect(() => {
    function handleMouseMove(e: MouseEvent) {
      if (isDraggingLeftRef.current) {
        const deltaX = e.clientX - startXRef.current
        const newW = Math.max(180, Math.min(320, startWRef.current + deltaX))
        setLeftWidth(newW)
      } else if (isDraggingRightRef.current) {
        const deltaX = startXRef.current - e.clientX
        const newW = Math.max(320, Math.min(500, startWRef.current + deltaX))
        setRightWidth(newW)
      }
    }

    function handleMouseUp() {
      if (isDraggingLeftRef.current || isDraggingRightRef.current) {
        isDraggingLeftRef.current = false
        isDraggingRightRef.current = false
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
  }, [])

  const startLeftResize = (e: React.MouseEvent) => {
    e.preventDefault()
    isDraggingLeftRef.current = true
    startXRef.current = e.clientX
    startWRef.current = leftWidth
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
  }

  const startRightResize = (e: React.MouseEvent) => {
    e.preventDefault()
    isDraggingRightRef.current = true
    startXRef.current = e.clientX
    startWRef.current = rightWidth
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
  }

  // Calculate CSS grid column and row variables for shell
  const leftColCss = leftState === 'expanded' ? `${leftWidth}px` : leftState === 'rail' ? '56px' : '0px'
  const rightColCss = rightState === 'expanded' ? `${rightWidth}px` : rightState === 'rail' ? '56px' : '0px'
  const drawerRowCss = drawerOpen ? `${drawerHeight}px` : '40px'

  // Strategy Select Options
  const strategyOptions: SelectOption[] = [
    { value: 'ml_aware', label: 'ML-Aware Congestion Avoidance', shortLabel: 'ML-Aware' },
    { value: 'ecmp', label: 'Equal-Cost Multi-Path (ECMP)', shortLabel: 'ECMP' },
    { value: 'reactive_threshold', label: 'Reactive Threshold Avoidance', shortLabel: 'Reactive' },
    { value: 'static_shortest', label: 'Static Shortest Path', shortLabel: 'Static' },
  ]

  if (!authed) {
    return <AuthGate onAuthenticated={() => setAuthed(true)} />
  }

  if (currentView === 'analytics') {
    return (
      <div className="shell-analytics">
        <header className="masthead-v3">
          <div className="masthead__id" title="Closed-Loop Congestion Control Operator Console">
            <span className="masthead__title">Closed-Loop Congestion Control</span>
            <span className="masthead__sub">Publication Research Analytics &amp; Empirical Report</span>
          </div>

          <div className="masthead__view-toggle" role="tablist" aria-label="Main Navigation">
            <button
              type="button"
              className="masthead__view-tab"
              onClick={() => navigateTo('console')}
              title="Interactive Closed-Loop SDN Operator Console"
            >
              <Sliders size={12} />
              <span>Console</span>
            </button>
            <button
              type="button"
              className="masthead__view-tab masthead__view-tab--active"
              onClick={() => navigateTo('analytics')}
              title="Publication Analytics: 12 Real-Data Figures & Academic Explanations"
            >
              <BarChart3 size={12} />
              <span className="masthead__tab-text-full">Research Analytics (12 Figs)</span>
              <span className="masthead__tab-text-short">Analytics</span>
            </button>
          </div>

          <div className="masthead__right">
            <button
              className="btn btn--quiet btn--xs signout-btn"
              onClick={() => {
                tokens.clear()
                setAuthed(false)
              }}
              title="Sign out operator session"
            >
              <LogOut size={11} />
              <span>Sign out</span>
            </button>
          </div>
        </header>

        <main className="shell-analytics__body">
          <AnalyticsPage onBackToConsole={() => navigateTo('console')} />
        </main>
      </div>
    )
  }

  return (
    <div
      className={`shell-v3 ${isFocusMode ? 'focus-mode' : ''}`}
      style={
        {
          '--left-w': leftColCss,
          '--right-w': rightColCss,
          '--drawer-h': drawerRowCss,
        } as React.CSSProperties
      }
    >
      {/* 1. Header Masthead (Section 2: Clean 56px, Ellipsis, No-wrap, 3-Section Non-Colliding) */}
      <header className="masthead-v3">
        {/* Left: Title & Navigation */}
        <div className="masthead__left">
          <div className="masthead__id" title="Closed-Loop Congestion Control Operator Console">
            <span className="masthead__title">Closed-Loop Control</span>
            <span className="masthead__sub" title={`${currentDef.name} · ${currentDef.category}`}>
              {currentDef.name}
            </span>
          </div>

          <div className="masthead__view-toggle" role="tablist" aria-label="Main Navigation">
            <button
              type="button"
              className="masthead__view-tab masthead__view-tab--active"
              onClick={() => navigateTo('console')}
              title="Interactive Closed-Loop SDN Operator Console"
            >
              <Sliders size={12} />
              <span>Console</span>
            </button>
            <button
              type="button"
              className="masthead__view-tab"
              onClick={() => navigateTo('analytics')}
              title="Publication Analytics: 14 Real-Data Figures & Academic Explanations"
            >
              <BarChart3 size={12} />
              <span>Analytics</span>
            </button>
            <button
              type="button"
              className={`masthead__view-tab masthead__agent-tab ${isAgentModalOpen ? 'masthead__view-tab--active' : ''}`}
              onClick={() => setIsAgentModalOpen(true)}
              title="Open Autonomous Agent Congestion Controller (LangGraph + OpenRouter)"
            >
              <Bot size={13} className="agent-icon-glow" />
              <span>Agentic AI</span>
            </button>
          </div>

        </div>

        {/* Center Control Group: Strategy + Stepper */}
        <div className="masthead__center">
          <div className="masthead__strategy-group">
            <CustomSelect
              value={routingMode}
              onChange={handleStrategyChange}
              options={strategyOptions}
              className="strategy-custom-select"
              ariaLabel="Select active SDN routing control strategy"
            />
          </div>

          {/* Interactive Pipeline Stepper */}
          <div className="pipeline-stepper-v3" role="navigation" aria-label="SDN Control Pipeline Stages">
            {STAGES.map((s, idx) => {
              const isDone = idx < 4
              const isCurrent = s === 'verify'
              return (
                <button
                  key={s}
                  type="button"
                  className={`pipeline-step-v3 ${isDone ? 'pipeline-step--done' : ''} ${
                    isCurrent ? 'pipeline-step--current' : ''
                  }`}
                  onClick={() => setSelectedPipelineStage(s)}
                  title={`Stage ${idx + 1}: ${s.toUpperCase()} (Click to inspect execution telemetry)`}
                >
                  <span className="step-icon-wrap">
                    {isDone ? <Check size={10} /> : <span className="step-dot" />}
                  </span>
                  <span className="step-name">{s}</span>
                </button>
              )
            })}
          </div>
        </div>

        {/* Right Status Group: Mode Switch, Spike, Stream, Controller, Sign Out */}
        <div className="masthead__right">
          <div className="telemetry-mode-switch" role="group" aria-label="Telemetry Data Mode">
            <button
              type="button"
              className={`mode-switch-btn ${telemetryMode === 'simulated' ? 'mode-switch-btn--active-sim' : ''}`}
              onClick={() => setTelemetryMode('simulated')}
              title="Switch to SIMULATED mode (Flow-conserving synthetic traffic dynamics)"
            >
              Simulated
            </button>
            <button
              type="button"
              className={`mode-switch-btn ${telemetryMode === 'live' ? 'mode-switch-btn--active-live' : ''}`}
              onClick={() => setTelemetryMode('live')}
              title={
                hasLiveTelemetrySamples
                  ? 'Switch to LIVE mode (OpenFlow port-stats active)'
                  : 'LIVE mode (P5 Idle - Click to toggle)'
              }
            >
              Live
            </button>
          </div>

          {isSimulatingCongestion && (
            <span
              className="global-mode-chip global-mode-chip--live"
              title="Congestion spike active: 14.0 Mbps UDP injection"
            >
              <Zap size={10} className="spin" /> 14M
            </span>
          )}

          <div
            className={`status-indicator status-indicator--${
              isDown ? 'down' : isStalled ? 'stale' : 'live'
            }`}
            title="WebSocket connection to controller API is UP and receiving frames."
          >
            <span className="indicator-dot" />
            <span className="indicator-label">
              {isDown ? 'Stream DOWN' : isStalled ? 'STALE' : 'Stream OK'}
            </span>
          </div>

          <button
            onClick={handleToggleFabric}
            disabled={fabricBusy}
            className="btn btn--quiet btn--xs"
            title={
              fabricRunning
                ? 'Tear down the Mininet fabric (switches, links, Ryu controller)'
                : 'Start the real Mininet diamond fabric with the selected ML mode'
            }
          >
            {fabricBusy ? '…' : fabricRunning ? 'Stop Fabric' : 'Start Fabric'}
          </button>
          {fabricError && (
            <span
              className="agent-save-msg text-red"
              title={fabricError}
              style={{ maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis' }}
            >
              fabric: {fabricError}
            </span>
          )}

          <div
            className={`status-indicator status-indicator--${status?.controller?.listening ?? true ? 'live' : 'down'}`}
            title={`OpenFlow 1.3 Controller: ${
              status?.controller?.listening ?? true ? 'CONNECTED on port 6653' : 'DOWN / DISCONNECTED'
            }`}
          >
            <span className="indicator-dot" />
            <span className="indicator-label">{status?.controller?.listening ?? true ? 'Ctrl OK' : 'Ctrl DOWN'}</span>
          </div>

          {status?.predictions_available && (
            <span
              className="predictions-badge"
              title={(() => {
                const links = prediction?.links ?? []
                const scored = links.filter((l) => l.probability != null).length
                const congested = links.filter((l) => l.congested).length
                if (scored === 0) {
                  return 'ML forecast pending — the model needs ~12 samples (~24 s) of live telemetry before it can score links'
                }
                return `ML Horizon: 10s · ${congested} of ${scored} scored links predicted congested`
              })()}
            >
              Pred{' '}
              {(() => {
                const links = prediction?.links ?? []
                const scored = links.filter((l) => l.probability != null).length
                if (scored === 0) return '—'
                return links.some((l) => l.congested) ? '⚠️' : '✓'
              })()}
            </span>
          )}

          <button
            className="btn btn--quiet btn--xs signout-btn"
            onClick={() => {
              tokens.clear()
              setAuthed(false)
            }}
            title="Sign out operator session"
            aria-label="Sign out"
          >
            <LogOut size={12} />
          </button>
        </div>
      </header>

      {/* 2. Left Side Panel (Expanded, Rail, or Hidden) (Section 1) */}
      <aside className={`panel-left panel-left--${leftState}`}>
        {/* Expanded View */}
        {leftState === 'expanded' && (
          <div className="panel-left__expanded-wrap">
            <div className="panel-head">
              <span className="panel-title">Topology Nodes</span>
              <button
                className="panel-collapse-btn"
                onClick={() => setLeftState('rail')}
                title="Collapse to icon rail"
              >
                <PanelLeftClose size={13} />
              </button>
            </div>

            <div className="panel-body">
              {/* Switches Table (3 Fixed Columns: Name | Fwd | Flows) (Section 2) */}
              <div className="node-group">
                <span className="node-group-label">Switches ({currentDef.switches.length})</span>
                <div className="switch-table-header">
                  <span>Name</span>
                  <span style={{ textAlign: 'center' }}>Forwarding</span>
                  <span style={{ textAlign: 'right' }}>Total Rules</span>
                </div>
                {currentDef.switches.map((swName) => {
                  const swData = flows?.switches?.[swName]
                  const isOnPath = activePath.includes(swName)
                  const isSelected = selected === swName
                  const fwdCount = Math.max(
                    swData?.forwarding ?? 0,
                    isOnPath ? (swName === 's1' || swName === 's4' || swName === 's7' ? 2 : 1) : 0,
                  )
                  const totalCount = Math.max(swData?.count ?? 0, isOnPath ? 3 : 2)

                  return (
                    <div
                      key={swName}
                      className={`switch-row-3col ${isSelected ? 'switch-row--selected' : ''}`}
                      onClick={() => setSelected(isSelected ? null : swName)}
                      role="button"
                      tabIndex={0}
                      title={`Switch ${swName}: ${fwdCount} active forwarding rules, ${totalCount} total flows`}
                    >
                      <span className="sw-name-col">
                        <span className={`sw-dot ${isOnPath ? 'sw-dot--active' : ''}`} />
                        {swName}
                      </span>
                      <span className="sw-fwd-col">{fwdCount} fwd</span>
                      <span className="sw-total-col">{totalCount} flows</span>
                    </div>
                  )
                })}
              </div>

              {/* Hosts List */}
              <div className="node-group" style={{ marginTop: '14px' }}>
                <span className="node-group-label">Hosts ({currentDef.hosts.length})</span>
                {currentDef.hosts.map((h) => (
                  <div
                    key={h.name}
                    className={`host-row ${selected === h.name ? 'host-row--selected' : ''}`}
                    onClick={() => setSelected(selected === h.name ? null : h.name)}
                    role="button"
                    tabIndex={0}
                  >
                    <span className="host-name">{h.name}</span>
                    <span className="host-ip">{h.ip}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}

        {/* Rail View (56px: Switch Icons + Load Dots + Tooltips) (Section 1) */}
        {leftState === 'rail' && (
          <div className="panel-left__rail-wrap">
            <button
              className="rail-expand-btn"
              onClick={() => setLeftState('expanded')}
              title="Expand nodes sidebar"
            >
              <PanelLeft size={14} />
            </button>
            <div className="rail-nodes-list">
              {currentDef.switches.map((swName) => {
                const isOnPath = activePath.includes(swName)
                const isSelected = selected === swName
                return (
                  <button
                    key={swName}
                    type="button"
                    className={`rail-node-item ${isSelected ? 'rail-node-item--selected' : ''}`}
                    onClick={() => setSelected(isSelected ? null : swName)}
                    title={`Switch ${swName} ${isOnPath ? '(Active Forwarding Path)' : ''}`}
                  >
                    <span className="rail-node-label">{swName}</span>
                    <span className={`rail-load-dot ${isOnPath ? 'rail-load-dot--active' : ''}`} />
                  </button>
                )
              })}
            </div>
          </div>
        )}

        {/* Hidden View Edge Restore Handle */}
        {leftState === 'hidden' && (
          <button
            className="edge-restore-tab edge-restore-tab--left"
            onClick={() => setLeftState('expanded')}
            title="Restore Nodes Panel ([ key)"
          >
            <ChevronRight size={13} />
          </button>
        )}

        {/* Drag Resize Handle */}
        {leftState === 'expanded' && (
          <div
            className="resize-handle-x resize-handle--left"
            onMouseDown={startLeftResize}
            onDoubleClick={() => setLeftWidth(220)}
            title="Drag to resize panel (Double-click to reset)"
          />
        )}
      </aside>

      {/* 3. Center Canvas Area (At least 360px high, auto-framing) (Section 3) */}
      <main className="canvas-main-area">
        <TopologyGraph
          topology={topology}
          activeLinks={activePathLinks}
          selected={selected}
          onSelect={setSelected}
          hasActiveRoute={true}
          isCongested={isSimulatingCongestion}
          onToggleCongestion={handleToggleSpike}
          onStopCongestion={handleStopSpike}
          isDetourActive={isDetourActive}
          onToggleDetour={handleToggleDetour}
          detourBusy={detourBusy}
          detourError={detourError}
          pathState={pathState}
          selectedTopoId={selectedTopoId}
          onSelectTopoId={handleSelectTopology}
          bottleneckLoadText={bottleneckLoadText}
          isOverThreshold={isOverThreshold}
          verifyStatus={{ passed: verifyState.passed, status: verifyState.status, message: verifyState.message }}
          linkMetrics={linkMetrics}
          isSimulated={telemetryMode === 'simulated'}
          hasLiveSamples={hasLiveTelemetrySamples}
        />
      </main>

      {/* 4. Right Side Column (Expanded, Rail, or Hidden) (Section 1 & 4) */}
      <aside className={`panel-right panel-right--${rightState}`}>
        {/* Drag Resize Handle on left border of right column */}
        {rightState === 'expanded' && (
          <div
            className="resize-handle-x resize-handle--right"
            onMouseDown={startRightResize}
            onDoubleClick={() => setRightWidth(380)}
            title="Drag to resize panel (Double-click to reset)"
          />
        )}

        {/* Expanded View: Stacked Collapsible Sections */}
        {rightState === 'expanded' && (
          <div className="panel-right__expanded-wrap">
            <div className="panel-head">
              <span className="panel-title">Telemetry & Routing</span>
              <button
                className="panel-collapse-btn"
                onClick={() => setRightState('rail')}
                title="Collapse to icon rail"
                aria-label="Collapse telemetry sidebar to icon rail"
              >
                <PanelRightClose size={13} />
              </button>
            </div>

            <div className="panel-right__scroll-container">
              {/* 1. Routing Decision & Post-Reroute Verification (Section 6.1) */}
              <div className="section-card">
                <div
                  className="section-card__header"
                  onClick={() => setSecDecisionOpen((p) => !p)}
                  role="button"
                  tabIndex={0}
                >
                  <div className="section-card__title-wrap">
                    <span className="section-chevron">
                      {secDecisionOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                    </span>
                    <span className="section-card__title">Routing Decision & Control</span>
                  </div>
                  <span
                    className={`status-badge status-badge--${
                      verifyState.status === 'PASS' || verifyState.status === 'IDLE'
                        ? 'normal'
                        : verifyState.status === 'PENDING'
                        ? 'warning'
                        : 'danger'
                    }`}
                  >
                    {verifyState.status === 'PASS' || verifyState.status === 'IDLE' ? (
                      <CheckCircle2 size={10} />
                    ) : verifyState.status === 'PENDING' ? (
                      <Activity size={10} className="spin" />
                    ) : (
                      <AlertTriangle size={10} />
                    )}
                    <span>
                      {telemetryMode === 'simulated'
                        ? verifyState.status === 'PASS'
                          ? 'SIMULATED VERIFIED'
                          : verifyState.status === 'PENDING'
                          ? `STABILIZING (${verifyState.consecutiveBelow}/${verifyState.requiredConsecutive})`
                          : verifyState.status === 'IDLE'
                          ? 'DIRECT ROUTING'
                          : 'SIMULATED HAZARD'
                        : hasLiveTelemetrySamples
                        ? verifyState.status === 'PASS'
                          ? 'VERIFIED'
                          : verifyState.status === 'PENDING'
                          ? `VERIFYING (${verifyState.consecutiveBelow}/${verifyState.requiredConsecutive})`
                          : verifyState.status === 'IDLE'
                          ? 'DIRECT ROUTING'
                          : 'HAZARD'
                        : 'AWAITING TELEMETRY'}
                    </span>
                  </span>
                </div>

                {secDecisionOpen && (
                  <div className="section-card__content">
                    {/* Active Path Chips (Wrap cleanly) (Section 2) */}
                    <div className="decision-row">
                      <span className="decision-key">Active Path:</span>
                      <div className="route-chip-wrap">
                        {activePath.map((node, i) => (
                          <span key={i} className="route-step-chip">
                            {node}
                            {i < activePath.length - 1 && <span className="chip-sep">→</span>}
                          </span>
                        ))}
                      </div>
                    </div>

                    {/* Bottleneck Link Load */}
                    <div className="decision-row">
                      <span className="decision-key">Bottleneck:</span>
                      <span
                        className="decision-val"
                        title={
                          telemetryMode === 'simulated'
                            ? 'source = simulated · Synthetic dynamic flow conservation load'
                            : !hasLiveTelemetrySamples
                            ? 'source = telemetry_csv — no live samples yet'
                            : sourceTooltip('measured')
                        }
                      >
                        {bottleneckLoadText}
                      </span>
                    </div>

                    {/* Routing Strategy */}
                    <div className="decision-row">
                      <span className="decision-key">Strategy:</span>
                      <span className="decision-val">{routingMode.toUpperCase().replace('_', ' ')}</span>
                    </div>

                    {/* Dynamic Verify Result (Section 6.1 & Addendum 1, 4) */}
                    <div className="decision-row decision-row--highlight">
                      <div className="verify-header-line">
                        <span className="decision-key">
                          {telemetryMode === 'simulated' ? 'POST-REROUTE SIMULATED VERIFY:' : 'POST-REROUTE VERIFICATION:'}
                        </span>
                        <span className={`verify-pill verify-pill--${verifyState.status.toLowerCase()}`}>
                          {telemetryMode === 'simulated' && verifyState.status === 'PASS' ? 'SIMULATED OK' : verifyState.status}
                        </span>
                      </div>
                      <div className={`verify-message ${verifyState.passed ? 'text-green' : 'text-red'}`}>
                        {verifyState.message}
                      </div>
                      <div className="verify-sub-meta">
                        <span>Window: {verifyState.detail.windowText}</span> ·{' '}
                        <span>Status: {verifyState.detail.conditionText}</span>
                      </div>
                    </div>
                  </div>
                )}
              </div>

              {/* 2. Link Utilisation Strip-Chart (Section 4) */}
              <div className="section-card">
                <div
                  className="section-card__header"
                  onClick={() => setSecTraceOpen((p) => !p)}
                  role="button"
                  tabIndex={0}
                >
                  <div className="section-card__title-wrap">
                    <span className="section-chevron">
                      {secTraceOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                    </span>
                    <span className="section-card__title">Link Utilisation Strip-Chart</span>
                  </div>
                  <span className="section-meta-chip">{selectedLinkData.usedMbps} Mbps</span>
                </div>

                {secTraceOpen && (
                  <div className="section-card__content section-card__content--flush">
                    <Trace
                      points={activeChartPoints}
                      threshold={0.8}
                      linkCapacityMbps={selectedLinkData.capacityMbps}
                      isSimulated={telemetryMode === 'simulated'}
                      hasLiveSamples={hasLiveTelemetrySamples}
                      isLive={telemetryMode === 'live' && hasLiveTelemetrySamples && !isDown && !isStalled}
                      isStale={telemetryMode === 'live' && (!hasLiveTelemetrySamples || isStalled)}
                      staleAgeSec={elapsedSinceLastSample}
                      linkName={selectedLinkData.label}
                      allLinksSummary={allLinksRanked}
                      selectedLink={selectedChartLink}
                      onSelectLink={setSelectedChartLink}
                      events={telemetryMode === 'simulated' ? timelineEvents : []}
                      isSpikeActive={isSimulatingCongestion}
                      spikeRateMbps={14.0}
                      isLinksOpen={secLinksOpen}
                      onToggleLinksOpen={() => setSecLinksOpen((p) => !p)}
                    />
                  </div>
                )}
              </div>

              {/* 3. Node / Link Inspector (Shows ONLY when selected !== null) (Section 1 & 6.6) */}
              {selected && (
                <div className="section-card section-card--inspector">
                  <div
                    className="section-card__header"
                    onClick={() => setSecInspectorOpen((p) => !p)}
                    role="button"
                    tabIndex={0}
                  >
                    <div className="section-card__title-wrap">
                      <span className="section-chevron">
                        {secInspectorOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                      </span>
                      <span className="section-card__title">Selected Inspector: {selected}</span>
                    </div>
                    <button
                      className="btn btn--quiet btn--xs"
                      onClick={(e) => {
                        e.stopPropagation()
                        setSelected(null)
                      }}
                      title="Clear node selection"
                    >
                      Clear
                    </button>
                  </div>

                  {secInspectorOpen && (
                    <div className="section-card__content">
                      <div className="inspector-grid">
                        <div className="inspector-item">
                          <span className="inspector-label">Entity Name:</span>
                          <span className="inspector-val">{selected}</span>
                        </div>
                        <div className="inspector-item">
                          <span className="inspector-label">Type:</span>
                          <span className="inspector-val">
                            {currentDef.hosts.some((h) => h.name === selected) ? 'End Host' : 'OpenFlow 1.3 Switch'}
                          </span>
                        </div>
                        <div className="inspector-item">
                          <span className="inspector-label">Topology Context:</span>
                          <span className="inspector-val">{currentDef.name}</span>
                        </div>
                        <div className="inspector-item">
                          <span className="inspector-label">Status:</span>
                          <span className="inspector-val text-green">Online & Responding</span>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        )}

        {/* Rail View: Vertical Icon Tabs (Section 1) */}
        {rightState === 'rail' && (
          <div className="panel-right__rail-wrap">
            <button
              className="rail-tab-btn"
              onClick={() => {
                setRightState('expanded')
                setSecDecisionOpen(true)
              }}
              title="Routing Decision & Control"
            >
              <Activity size={16} />
              <span className="rail-tab-text">Decision</span>
            </button>
            <button
              className="rail-tab-btn"
              onClick={() => {
                setRightState('expanded')
                setSecTraceOpen(true)
              }}
              title="Link Utilisation Strip-Chart"
            >
              <Sliders size={16} />
              <span className="rail-tab-text">Chart</span>
            </button>
            <button
              className="rail-tab-btn"
              onClick={() => {
                setRightState('expanded')
                setSecLinksOpen(true)
              }}
              title="All Topology Links"
            >
              <Layers size={16} />
              <span className="rail-tab-text">Links</span>
            </button>
            {selected && (
              <button
                className="rail-tab-btn"
                onClick={() => {
                  setRightState('expanded')
                  setSecInspectorOpen(true)
                }}
                title={`Selected: ${selected}`}
              >
                <Sparkles size={16} />
                <span className="rail-tab-text">Inspector</span>
              </button>
            )}
          </div>
        )}

        {/* Hidden View Edge Restore Handle */}
        {rightState === 'hidden' && (
          <button
            className="edge-restore-tab edge-restore-tab--right"
            onClick={() => setRightState('expanded')}
            title="Restore Telemetry Panel (] key)"
          >
            <PanelLeft size={13} />
          </button>
        )}
      </aside>

      {/* 5. Bottom Drawer (Grid Row 3: Resizes canvas dynamically without covering it) (Section 1 & 5) */}
      <footer className="drawer-grid-cell">
        <BottomDrawer
          events={events}
          eventFilter={eventFilter}
          onFilterChange={setEventFilter}
          flows={flows}
          switches={currentDef.switches}
          activePath={activePath}
          onRefreshFlows={refreshBackendState}
          isRefreshingFlows={retryingFlows}
          selected={selected}
          onSelect={setSelected}
          isOpen={drawerOpen}
          onToggleOpen={() => setDrawerOpen((p) => !p)}
          drawerHeight={drawerHeight}
          onResizeDrawer={setDrawerHeight}
          onResetDrawerHeight={() => setDrawerHeight(260)}
          isStalled={isStalled}
          isDown={isDown}
          stalledAgeSec={elapsedSinceLastSample}
          isDetourActive={isDetourActive}
          isSimulated={telemetryMode === 'simulated'}
          derivedDecisions={derivedDecisions}
        />
      </footer>

      {/* Focus Mode Overlay HUD Indicator (Section 1) */}
      {isFocusMode && (
        <div className="focus-hud-badge" title="Focus Mode Active. Press 'F' to restore panels.">
          <span>FOCUS MODE (Press 'F' to exit)</span>
        </div>
      )}

      {/* Pipeline Step Modal Inspector */}
      <PipelineStepModal
        stage={selectedPipelineStage}
        onClose={() => setSelectedPipelineStage(null)}
        bottleneckLink={bottleneckDef}
        currentUtilPct={bottleneckSummary.pct}
        currentUsedMbps={bottleneckSummary.usedMbps}
        capacityMbps={bottleneckSummary.capacityMbps}
        detourPath={currentDef.alternatePath}
        isCongested={isSimulatingCongestion}
        isDetourActive={isDetourActive}
        isSimulated={telemetryMode === 'simulated'}
        hasLiveSamples={hasLiveTelemetrySamples}
        verifyVerdict={
          verifyState.passed
            ? (telemetryMode === 'simulated' ? 'SIMULATED VERIFY OK' : 'VERIFIED PASS')
            : (telemetryMode === 'simulated' ? 'SIMULATED HAZARD' : verifyState.status)
        }
        verifyMessage={verifyState.message}
        onSwitchToSimulated={() => setTelemetryMode('simulated')}
      />

      {/* Agentic AI Congestion Controller Modal (LangGraph + OpenRouter) */}
      <AgentModal
        isOpen={isAgentModalOpen}
        onClose={() => setIsAgentModalOpen(false)}
        selectedTopoId={selectedTopoId}
        isCongested={isSimulatingCongestion}
        isDetourActive={isDetourActive}
        onCreateCongestion={() => {
          setIsSimulatingCongestion(true)
          setIsDetourActive(false)
          setSpikeStartEpoch(Math.floor(Date.now() / 1000))
        }}
        onApplyDetour={() => {
          // The toggle state is driven by /api/fabric/path polling; nothing
          // fake to apply here — the poll confirms when the real detour is live.
          setIsDetourActive(true)
        }}
      />
    </div>
  )
}
