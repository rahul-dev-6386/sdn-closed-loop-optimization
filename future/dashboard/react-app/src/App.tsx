/**
 * Console shell.
 *
 * Masthead doubles as the hero and as a status display: the five stages of the
 * control loop are drawn as a path with a pulse travelling along it, so the
 * operator can see where the system currently is rather than reading a log to
 * work it out.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, liveSocket, setSessionLostHandler, tokens, type LiveFrame, type SdnEvent, type Status, type Topology } from './api'
import { AuthGate } from './components/AuthGate'
import { TopologyGraph } from './components/TopologyGraph'
import { Trace, type TracePoint } from './components/Trace'

const STAGES = ['measure', 'predict', 'decide', 'reroute', 'verify'] as const

/** Which stage is lit, derived from what the system has actually delivered. */
function activeStage(status: Status | null): number {
  if (!status) return 0
  if (status.predictions_available && status.phases.P5_telemetry) return 3
  if (status.phases.P5_telemetry) return 2
  if (status.controller.events_logged > 0) return 1
  return 0
}

interface FlowPayload {
  available: boolean
  reason?: string
  switches?: Record<string, { count: number; forwarding: number; entries: string[] }>
}

export default function App() {
  const [authed, setAuthed] = useState<boolean>(() => Boolean(tokens.access()))
  const [topology, setTopology] = useState<Topology | null>(null)
  const [status, setStatus] = useState<Status | null>(null)
  const [events, setEvents] = useState<SdnEvent[]>([])
  const [flows, setFlows] = useState<FlowPayload | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [prediction, setPrediction] = useState<{ available: boolean; reason?: string } | null>(null)
  const [live, setLive] = useState(false)
  const [points] = useState<TracePoint[]>([])
  const socketRef = useRef<WebSocket | null>(null)

  const signOut = useCallback(async () => {
    try {
      await api.logout()
    } catch {
      /* revoking is best-effort; clear locally regardless */
    }
    tokens.clear()
    socketRef.current?.close()
    setAuthed(false)
  }, [])

  // End the session when a refresh attempt fails, from anywhere in the app.
  useEffect(() => {
    setSessionLostHandler(() => setAuthed(false))
  }, [])

  // Initial load once authenticated.
  useEffect(() => {
    if (!authed) return
    let cancelled = false
    ;(async () => {
      try {
        const [topo, st, evs, fl, pred] = await Promise.all([
          api.topology(),
          api.status(),
          api.events(60),
          api.flows().catch(() => ({ available: false, reason: 'unavailable' } as FlowPayload)),
          api.predictions().catch(() => ({ available: false, links: [] })),
        ])
        if (cancelled) return
        setTopology(topo)
        setStatus(st)
        setEvents(evs)
        setFlows(fl as FlowPayload)
        setPrediction(pred)
      } catch {
        if (!cancelled) setAuthed(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [authed])

  // Live stream.
  useEffect(() => {
    if (!authed) return
    const socket = liveSocket(
      (frame: LiveFrame) => {
        setLive(true)
        // Merge rather than replace: the live snapshot carries the graph but not
        // `port_map`, which only the REST endpoint supplies. Assigning the frame
        // wholesale silently dropped the port map every 2 seconds, so the port
        // labels on a selected switch blinked out.
        setTopology((prev) =>
          prev ? { ...frame.topology, port_map: prev.port_map } : frame.topology,
        )
        setEvents((prev) => [...frame.events, ...prev].slice(0, 120))
        setStatus((prev) => (prev ? { ...prev, controller: frame.controller } : prev))
      },
      () => setLive(false),
    )
    socketRef.current = socket
    return () => {
      socket.close()
      socketRef.current = null
    }
  }, [authed])

  // Poll flow tables; they change independently of the WS snapshot.
  useEffect(() => {
    if (!authed) return
    const id = window.setInterval(() => {
      api.flows().then(setFlows as (f: unknown) => void).catch(() => undefined)
    }, 5000)
    return () => window.clearInterval(id)
  }, [authed])

  const activeLinks = useMemo(() => {
    const set = new Set<string>()
    if (!flows?.switches) return set
    // A link is "carrying" when both of its switches hold forwarding rules.
    for (const link of topology?.links ?? []) {
      if (link.host_link) continue
      const a = flows.switches[link.a]?.forwarding ?? 0
      const b = flows.switches[link.b]?.forwarding ?? 0
      if (a > 0 && b > 0) set.add([link.a, link.b].sort().join('--'))
    }
    return set
  }, [flows, topology])

  const stage = activeStage(status)
  const threshold = 0.8
  const forwardingTotal = Object.values(flows?.switches ?? {}).reduce((sum, s) => sum + s.forwarding, 0)

  if (!authed) return <AuthGate onAuthenticated={() => setAuthed(true)} />

  return (
    <div className="shell">
      <header className="masthead">
        <div className="masthead__id">
          <span className="masthead__title">Closed-loop congestion control</span>
          <span className="masthead__sub">
            {topology?.name ?? 'topology'} · {topology?.datapath_type ?? 'unknown'} datapath
          </span>
        </div>

        {/* The hero: the loop, drawn as a path with a pulse on it. */}
        <nav className="loop" aria-label="Control loop progress">
          {STAGES.map((name, i) => (
            <span key={name} style={{ display: 'contents' }}>
              <span className="loop__stage" data-live={i === stage} data-done={i < stage}>
                <span className="loop__dot" aria-hidden="true" />
                <span>{name}</span>
              </span>
              {i < STAGES.length - 1 && (
                <span className="loop__link" data-flow={i < stage} aria-hidden="true" />
              )}
            </span>
          ))}
        </nav>

        <div className="masthead__status">
          <span className="lamp" data-on={live}>stream</span>
          <span className="lamp" data-on={status?.controller.listening ?? false}>controller</span>
          <button className="btn btn--quiet" onClick={signOut}>Sign out</button>
        </div>
      </header>

      <aside className="rail">
        <div className="rail__group">
          <span className="rail__label">Switches</span>
          {(topology?.switches ?? []).map((name) => {
            const f = flows?.switches?.[name]
            const on = (f?.forwarding ?? 0) > 0
            return (
              <div
                key={name}
                className="node-row"
                data-selected={selected === name}
                data-load={on}
                onClick={() => setSelected(selected === name ? null : name)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => e.key === 'Enter' && setSelected(selected === name ? null : name)}
              >
                <span className="node-row__name">{name}</span>
                <span className="node-row__meta">
                  {f ? `${f.forwarding}/${f.count}` : '—'}
                </span>
              </div>
            )
          })}
        </div>

        <div className="rail__group">
          <span className="rail__label">Hosts</span>
          {(topology?.hosts ?? []).map((h) => (
            <div
              key={h.name}
              className="node-row"
              data-selected={selected === h.name}
              onClick={() => setSelected(selected === h.name ? null : h.name)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => e.key === 'Enter' && setSelected(selected === h.name ? null : h.name)}
            >
              <span className="node-row__name">{h.name}</span>
              <span className="node-row__meta">{h.ip}</span>
            </div>
          ))}
        </div>

        <div className="rail__foot">
          Click a node to read its port map.
        </div>
      </aside>

      <main className="main">
        <section className="panel panel--graph">
          <div className="panel__head">
            <span className="panel__title">Topology</span>
            {selected && topology?.port_map?.[selected] ? (
              // Port adjacency as a readout strip rather than on-graph labels,
              // which collided with the host names and IPs.
              <span className="portstrip readout">
                <span className="portstrip__key">{selected}</span>
                {Object.entries(topology.port_map[selected])
                  .sort(([a], [b]) => a.localeCompare(b))
                  .map(([neighbour, port]) => (
                    <span key={neighbour} className="portstrip__pair">
                      {neighbour}:{port}
                    </span>
                  ))}
              </span>
            ) : (
              <span className="panel__note">
                {activeLinks.size} active path{activeLinks.size === 1 ? '' : 's'}
              </span>
            )}
          </div>
          <div className="panel__body panel__body--flush">
            <TopologyGraph
              topology={topology}
              activeLinks={activeLinks}
              selected={selected}
              onSelect={setSelected}
            />
          </div>
        </section>

        <section className="panel panel--trace">
          <div className="panel__head">
            <span className="panel__title">Link utilisation</span>
            <span className="panel__note">
              {prediction?.available
                ? `threshold ${Math.round(threshold * 100)}%`
                : 'no model yet — P7 pending'}
            </span>
          </div>
          <div className="panel__body">
            <Trace
              points={points}
              threshold={threshold}
              linkCapacityMbps={topology?.link_bw_mbps ?? null}
            />
          </div>
        </section>

        <section className="panel panel--events">
          <div className="panel__head">
            <span className="panel__title">Controller events</span>
            <span className="panel__note">{events.length} shown</span>
          </div>
          <div className="panel__body panel__body--flush">
            {events.length === 0 ? (
              <div className="notice">
                <span className="notice__head">No events recorded</span>
                <span className="notice__body">
                  The controller writes events to a JSONL file as it runs.
                </span>
                <span className="notice__action">make verify-p3</span>
              </div>
            ) : (
              <div className="log">
                {events.map((e, i) => (
                  <div className="log__row" key={`${e.ts ?? e.wall_ts}-${i}`} data-kind={e.kind}>
                    <span className="log__time">
                      {e.wall_iso ? e.wall_iso.slice(11, 19) : '--:--:--'}
                    </span>
                    <span className="log__kind">{e.kind}</span>
                    <span className="log__msg">{summarise(e)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>

        <section className="panel panel--flows">
          <div className="panel__head">
            <span className="panel__title">OpenFlow tables</span>
            <span className="panel__note">{forwardingTotal} forwarding</span>
          </div>
          <div className="panel__body panel__body--flush">
            {!flows?.available ? (
              <div className="notice">
                <span className="notice__head">Tables unavailable</span>
                <span className="notice__body">
                  {flows?.reason ??
                    'Reading the flow tables needs permission to query Open vSwitch.'}
                </span>
                <span className="notice__action">sudo uvicorn api.main:app</span>
              </div>
            ) : (
              <div className="flows">
                {Object.entries(flows.switches ?? {}).map(([sw, data]) => (
                  <div className="flows__row" key={sw} data-active={data.forwarding > 0}>
                    <span className="flows__sw">{sw}</span>
                    <span>{data.count} entries</span>
                    <span className="flows__n">{data.forwarding} fwd</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>
      </main>
    </div>
  )
}

/** One-line, human phrasing of an event, naming things the way an operator would. */
function summarise(e: SdnEvent): string {
  const pick = (k: string) => (e[k] === undefined ? '' : String(e[k]))
  switch (e.kind) {
    case 'switch_connected':
      return `switch ${pick('switch')} connected`
    case 'switch_disconnected':
      return `switch ${pick('switch')} disconnected (${pick('state')})`
    case 'arp_reply':
      return `${pick('requester')} asked for ${pick('target_ip')}, told ${pick('target_mac')}`
    case 'host_resolved':
      return `resolved ${pick('ip')} to ${pick('mac')} on ${pick('switch')}`
    case 'path_installed':
      return `${pick('rules')} rules installed for ${pick('eth_dst')} via ${Array.isArray(e.path) ? e.path.join(' → ') : ''}`
    case 'unknown_unicast_dropped':
      return `dropped unicast to ${pick('eth_dst')} on ${pick('switch')}`
    case 'user_login':
      return `${pick('username')} signed in`
    case 'user_registered':
      return `${pick('username')} created an account`
    default: {
      const rest = Object.entries(e)
        .filter(([k]) => !['kind', 'ts', 'wall_ts', 'wall_iso'].includes(k))
        .slice(0, 3)
        .map(([k, v]) => `${k}=${v}`)
        .join(' ')
      return rest || 'recorded'
    }
  }
}