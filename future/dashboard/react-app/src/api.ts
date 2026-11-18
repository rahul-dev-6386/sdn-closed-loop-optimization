/**
 * API client with automatic access-token refresh, server clock offset sync,
 * resilient WebSocket reconnection with exponential backoff, and edge schema validation.
 */
import { safeValidate, LiveFrameSchema, TopologyResponseSchema } from './utils/schemas'

const BASE =
  import.meta.env.VITE_API_BASE ??
  (typeof window !== 'undefined'
    ? `${window.location.protocol}//${window.location.hostname}:8000`
    : 'http://127.0.0.1:8000')

const ACCESS_KEY = 'sdn.access'
const REFRESH_KEY = 'sdn.refresh'

// Server clock offset in milliseconds (server_time - client_time)
let serverClockOffsetMs = 0

export function getServerClockOffsetMs(): number {
  return serverClockOffsetMs
}

export function getServerNow(): number {
  return Date.now() + serverClockOffsetMs
}

export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
}

export interface Topology {
  id?: string
  name: string
  switches: string[]
  hosts: { name: string; ip: string; switch: string; x?: number; y?: number }[]
  links: { a: string; b: string; bw_mbps?: number | null; delay_ms?: number | null; host_link?: boolean }[]
  link_bw_mbps?: number | null
  link_delay_ms?: number | null
  datapath_type?: string | null
  ports_available: boolean
  port_map: Record<string, Record<string, number>>
  node_coordinates?: Record<string, { x: number; y: number; layer?: number }>
  default_path?: string[]
  alternate_path?: string[]
  bottleneck_link?: string
}

export interface SdnEvent {
  id?: string
  kind: string
  wall_ts?: number
  wall_iso?: string
  ts?: number
  summary?: string
  elapsed_s?: number
  link?: string
  rate_mbps?: number
  [key: string]: unknown
}

export interface Status {
  controller: { listening: boolean; events_logged: number; checked_at: number }
  mongo_enabled: boolean
  predictions_available: boolean
  phases: Record<string, boolean>
  fabric_running?: boolean
}

export interface LiveTelemetryRow {
  timestamp: string
  src_switch: string
  dst_switch: string
  direction: string
  utilization: string
  is_congested: string
  predicted_congested?: string
  latency_ms?: string
  tx_rate_mbps?: string
}

export interface FabricPathResponse {
  status: 'ok' | 'unavailable'
  reason?: string
  age_s?: number
  mode?: string
  n_links?: number
  graph?: Record<string, string[]>
  flows?: Array<{ flow?: string; path?: string[] }>
  flow?: string | null
  path: string[]
  constraints?: string[]
  state_path?: string
}

export interface FabricStatus {
  fabric_running: boolean
  routing_mode: string
  spike_active: boolean
  spike_rate_mbps: number
  spike_elapsed_s: number
  spike_duration_s: number
}

export interface LinkUtilisationRecord {
  key: string
  src: string
  dst: string
  capacity_mbps: number
  used_mbps: number
  util_pct: number
  is_stale: boolean
  source: string
}

export interface LiveFrame {
  type: string
  at: number
  topology?: Topology
  controller?: Status['controller']
  events: SdnEvent[]
  telemetry?: LiveTelemetryRow[]
  utilisation?: {
    available: boolean
    topo_id: string
    timestamp: number
    links: Record<string, LinkUtilisationRecord>
  }
  fabric?: FabricStatus
}

/** Raised when the server rejects a request even after a refresh attempt. */
export class ApiError extends Error {
  readonly status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

export const tokens = {
  access: () => localStorage.getItem(ACCESS_KEY),
  refresh: () => localStorage.getItem(REFRESH_KEY),
  set(pair: TokenPair) {
    localStorage.setItem(ACCESS_KEY, pair.access_token)
    localStorage.setItem(REFRESH_KEY, pair.refresh_token)
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY)
    localStorage.removeItem(REFRESH_KEY)
  },
}

let onSessionLost: (() => void) | null = null
export function setSessionLostHandler(fn: () => void) {
  onSessionLost = fn
}

async function parse(response: Response) {
  if (response.status === 204) return null
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

export async function refreshSession(): Promise<boolean> {
  const rt = tokens.refresh()
  if (!rt) return false
  try {
    const response = await fetch(`${BASE}/api/auth/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: rt }),
    })
    if (!response.ok) {
      tokens.clear()
      onSessionLost?.()
      return false
    }
    const pair = (await parse(response)) as TokenPair
    tokens.set(pair)
    return true
  } catch {
    return false
  }
}

interface RequestOptions {
  method?: string
  body?: unknown
  retryOn401?: boolean
  signal?: AbortSignal
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, retryOn401 = true, signal } = options
  const headers: Record<string, string> = { Accept: 'application/json' }
  const access = tokens.access()
  if (access) headers.Authorization = `Bearer ${access}`
  let payload: BodyInit | undefined
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }

  const response = await fetch(`${BASE}${path}`, { method, headers, body: payload, signal })

  // Synchronize server clock offset from response Date header
  const serverDateStr = response.headers.get('date')
  if (serverDateStr) {
    const serverTimeMs = new Date(serverDateStr).getTime()
    if (!isNaN(serverTimeMs)) {
      serverClockOffsetMs = serverTimeMs - Date.now()
    }
  }

  if (response.status === 401 && retryOn401) {
    const refreshed = await refreshSession()
    if (refreshed) {
      return request<T>(path, { ...options, retryOn401: false })
    }
    tokens.clear()
    onSessionLost?.()
    throw new ApiError('Session expired. Sign in again.', 401)
  }

  if (!response.ok) {
    const data = await parse(response)
    const message =
      typeof data === 'object' && data && 'detail' in data
        ? String((data as { detail: unknown }).detail)
        : `Request failed with status ${response.status}`
    throw new ApiError(message, response.status)
  }
  return (await parse(response)) as T
}

export const api = {
  register: (username: string, password: string) =>
    request<TokenPair>('/api/auth/register', {
      method: 'POST',
      body: { username, password },
      retryOn401: false,
    }),
  login: (username: string, password: string) =>
    request<TokenPair>('/api/auth/login', {
      method: 'POST',
      body: { username, password },
      retryOn401: false,
    }),
  logout: () =>
    request<{ revoked: boolean }>('/api/auth/logout', {
      method: 'POST',
      body: { refresh_token: tokens.refresh() },
      retryOn401: false,
    }),
  topology: async (id?: string, signal?: AbortSignal): Promise<Topology> => {
    const raw = await request<unknown>(`/api/topology${id ? `?id=${encodeURIComponent(id)}` : ''}`, { signal })
    const validated = safeValidate(TopologyResponseSchema, raw, 'Topology')
    if (validated.success) {
      return validated.data as unknown as Topology
    }
    return raw as Topology
  },
  flows: (topo_id?: string, signal?: AbortSignal) =>
    request<Record<string, unknown>>(
      `/api/flows${topo_id ? `?topo_id=${encodeURIComponent(topo_id)}` : ''}`,
      { signal },
    ),
  status: (signal?: AbortSignal) => request<Status>('/api/status', { signal }),
  events: (limit = 100, signal?: AbortSignal) => request<SdnEvent[]>(`/api/events?limit=${limit}`, { signal }),
  predictions: (topo_id?: string, signal?: AbortSignal) =>
    request<{
      available: boolean
      reason?: string
      links: {
        link: string
        probability?: number | null
        congested?: boolean | null
        reason?: string | null
        source?: string
        direction?: string | null
      }[]
    }>(`/api/predictions${topo_id ? `?topo_id=${encodeURIComponent(topo_id)}` : ''}`, { signal }),
  fabricPath: () => request<FabricPathResponse>('/api/fabric/path'),
  fabricStatus: (signal?: AbortSignal) => request<FabricStatus>('/api/fabric/status', { signal }),
  startFabric: (mode = 'ml_aware') =>
    request<{ status: string }>('/api/fabric/start', { method: 'POST', body: { mode } }),
  stopFabric: () => request<{ status: string }>('/api/fabric/stop', { method: 'POST' }),
  pinLink: (link: string, seconds = 30) =>
    request<{ status: string; link: string; seconds: number }>(
      '/api/fabric/pin-link',
      { method: 'POST', body: { link, seconds } },
    ),
  unpinLink: () =>
    request<{ status: string; link: string | null; seconds: number }>(
      '/api/fabric/pin-link',
      { method: 'DELETE' },
    ),
  startSpike: (rate_mbps = 14.0, duration_s = 45.0) =>
    request<{ status: string }>('/api/traffic/spike', {
      method: 'POST',
      body: { rate_mbps, duration_s },
    }),
  stopSpike: () => request<{ status: string }>('/api/traffic/stop', { method: 'POST' }),
  spikeStart: (src = 'h1', dst = 'h4', mbps = 14.0, proto = 'udp') =>
    request<{ status: string }>('/api/spike', {
      method: 'POST',
      body: { src, dst, mbps, proto },
    }),
  spikeStop: () =>
    request<{ status: string }>('/api/spike', {
      method: 'DELETE',
    }),
  utilisation: (topo_id?: string, signal?: AbortSignal) =>
    request<{
      available: boolean
      topo_id: string
      timestamp: number
      links: Record<string, LinkUtilisationRecord>
    }>(`/api/telemetry/utilisation${topo_id ? `?topo_id=${encodeURIComponent(topo_id)}` : ''}`, { signal }),
  emitDecisionEvent: (payload: {
    predicted_link: string
    p: number
    action: string
    flow_mods?: string[]
    path_old?: string[]
    path_new?: string[]
  }) =>
    request<{ status: string; event: SdnEvent }>('/api/controller/decision-event', {
      method: 'POST',
      body: payload,
    }),
  emitVerifyEvent: (payload: {
    link: string
    before_pct: number
    after_pct: number
    window_s: number
    result: string
  }) =>
    request<{ status: string; event: SdnEvent }>('/api/controller/verify-event', {
      method: 'POST',
      body: payload,
    }),
  setControllerMode: (mode: string) =>
    request<{ status: string }>('/api/controller/mode', {
      method: 'POST',
      body: { mode },
    }),
  analyticsRealData: (signal?: AbortSignal) =>
    request<AnalyticsRealData>('/api/analytics/real-data', { signal }),
  agentConfig: () => request<AgentConfigData>('/api/agent/config'),
  saveAgentConfig: (api_key: string, model: string) =>
    request<AgentConfigData>('/api/agent/config', {
      method: 'POST',
      body: { api_key, model },
    }),
  agentState: () => request<AgentStateData>('/api/agent/state'),
  agentChat: (message: string, topology_id?: string) =>
    request<AgentStateData>('/api/agent/chat', {
      method: 'POST',
      body: { message, topology_id },
    }),
  agentRun: (topology_id?: string) =>
    request<AgentStateData>('/api/agent/run', {
      method: 'POST',
      body: { topology_id },
    }),
  agentCreateCongestion: (topology_id?: string, rate_mbps: number = 14.0) =>
    request<AgentStateData>('/api/agent/create-congestion', {
      method: 'POST',
      body: { topology_id, rate_mbps },
    }),
}

export interface AgentStepLog {
  node: string
  title: string
  description: string
  timestamp: string
  status: 'running' | 'success' | 'warning' | 'error'
  meta?: Record<string, any>
}

export interface AgentMessage {
  role: 'user' | 'assistant' | 'system'
  content: string
  time?: string
}

export interface AgentStateData {
  messages: AgentMessage[]
  task: string
  topology_id: string
  network_state: Record<string, any>
  predictions: Record<string, any>
  congestion_detected: boolean
  bottleneck_link: string
  analysis: string
  plan: string
  action_taken: string
  verification: Record<string, any>
  iteration: number
  status: string
  steps_log: AgentStepLog[]
  error?: string | null
}

export interface AgentConfigData {
  has_api_key: boolean
  masked_key: string
  model: string
  available_models: Array<{
    id: string
    name: string
    context: number
    provider?: string
  }>
  has_groq_api_key?: boolean
  groq_masked_key?: string
  congestion_threshold_pct: number
}


export interface AnalyticsRealData {
  status: string
  provenance: {
    experiments_file: string
    telemetry_file: string
    model_file: string
  }
  results_md?: string
  summary_table: Array<{
    mode: string
    scenario: string
    n_runs: string
    recovery_rate: string
    mean_latency_ms_mean: string
    mean_latency_ms_stdev: string
    p95_latency_ms_mean: string
    p95_latency_ms_stdev: string
    throughput_mbps_mean: string
    throughput_mbps_stdev: string
    loss_pct_mean: string
    loss_pct_stdev: string
    utilization_balance_mean: string
    utilization_balance_stdev: string
    congestion_duration_s_mean: string
    congestion_duration_s_stdev: string
    recovery_time_s_mean: string
    recovery_time_s_stdev: string
    reroute_count_mean: string
    reroute_count_stdev: string
  }>
  ml_models: {
    random_forest?: any
    logistic_regression?: any
  }
  ml_general: {
    trained_at?: string
    dataset?: string
    decision_threshold: number
    winner: string
  }
  features: Array<{ name: string; importance: number }>
  patterns: Record<string, number>
  time_series_sample: Array<{
    time: number
    tx_mbps: number
    rx_mbps: number
    util_pct: number
    latency_ms: number
    congested: boolean
  }>
  total_dataset_rows: number
}

/**
 * Resilient live WebSocket manager with exponential backoff and delta synchronisation.
 */
export function liveSocket(
  onFrame: (f: LiveFrame) => void,
  onStateChange?: (state: 'connecting' | 'connected' | 'stale' | 'down') => void,
  topoId?: string,
): { close: () => void; isConnected: () => boolean } {
  let ws: WebSocket | null = null
  let isClosedManually = false
  let retryCount = 0
  let reconnectTimer: number | null = null

  function connect() {
    if (isClosedManually) return

    const access = tokens.access()
    const url = `${BASE.replace(/^http/, 'ws')}/ws/live?token=${encodeURIComponent(access ?? '')}${topoId ? `&topo_id=${encodeURIComponent(topoId)}` : ''}`

    onStateChange?.('connecting')

    try {
      ws = new WebSocket(url)
    } catch {
      scheduleReconnect()
      return
    }

    ws.onopen = () => {
      retryCount = 0
      onStateChange?.('connected')
    }

    ws.onmessage = (event) => {
      try {
        const raw = JSON.parse(event.data)
        const validated = safeValidate(LiveFrameSchema, raw, 'LiveFrame')
        if (validated.success) {
          const frame = validated.data as unknown as LiveFrame
          // Server clock offset calculation from frame.at
          if (frame.at) {
            serverClockOffsetMs = frame.at * 1000 - Date.now()
          }
          onFrame(frame)
        }
      } catch {
        /* malformed message is safely discarded */
      }
    }

    ws.onclose = () => {
      if (!isClosedManually) {
        onStateChange?.('down')
        scheduleReconnect()
      }
    }

    ws.onerror = () => {
      ws?.close()
    }
  }

  function scheduleReconnect() {
    if (isClosedManually) return
    const delay = Math.min(15000, 1000 * Math.pow(1.5, retryCount))
    retryCount++
    if (reconnectTimer) clearTimeout(reconnectTimer)
    reconnectTimer = window.setTimeout(connect, delay)
  }

  connect()

  return {
    close: () => {
      isClosedManually = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      ws?.close()
    },
    isConnected: () => ws?.readyState === WebSocket.OPEN,
  }
}

export { BASE as API_BASE }