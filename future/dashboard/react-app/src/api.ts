/**
 * API client with automatic access-token refresh.
 *
 * Tokens live in localStorage. That is acceptable *only* because this is a
 * localhost, single-operator tool; the refresh token is long-lived and
 * localStorage is XSS-readable. A deployment on an untrusted origin should
 * move the refresh token into an httpOnly cookie. See README.
 */

const BASE = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8000'

const ACCESS_KEY = 'sdn.access'
const REFRESH_KEY = 'sdn.refresh'

export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
}

export interface Topology {
  name: string
  switches: string[]
  hosts: { name: string; ip: string; switch: string }[]
  links: { a: string; b: string; bw_mbps?: number | null; delay_ms?: number | null; host_link?: boolean }[]
  link_bw_mbps?: number | null
  link_delay_ms?: number | null
  datapath_type?: string | null
  ports_available: boolean
  port_map: Record<string, Record<string, number>>
}

export interface SdnEvent {
  kind: string
  wall_ts?: number
  wall_iso?: string
  ts?: number
  [key: string]: unknown
}

export interface Status {
  controller: { listening: boolean; events_logged: number; checked_at: number }
  mongo_enabled: boolean
  predictions_available: boolean
  phases: Record<string, boolean>
}

export interface LiveFrame {
  type: string
  at: number
  topology: Topology
  controller: Status['controller']
  events: SdnEvent[]
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

/** Called when refresh fails and the session must end. */
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

/**
 * Exchange the refresh token for a new access token.
 *
 * @returns true when a new pair was stored.
 */
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
  /** Set false to avoid a refresh+retry cycle (used by auth calls). */
  retryOn401?: boolean
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, retryOn401 = true } = options
  const headers: Record<string, string> = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const access = tokens.access()
  if (access) headers.Authorization = `Bearer ${access}`

  const response = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })

  if (response.status === 401 && retryOn401 && tokens.refresh()) {
    if (await refreshSession()) {
      return request<T>(path, { ...options, retryOn401: false })
    }
  }

  if (!response.ok) {
    const payload = await parse(response)
    const message =
      (payload && typeof payload === 'object' && 'detail' in payload
        ? String((payload as { detail: unknown }).detail)
        : typeof payload === 'string'
          ? payload
          : response.statusText) || `HTTP ${response.status}`
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
  logout: () => request<{ revoked: boolean }>('/api/auth/logout', {
    method: 'POST',
    body: { refresh_token: tokens.refresh() },
    retryOn401: false,
  }),
  topology: () => request<Topology>('/api/topology'),
  flows: () => request<Record<string, unknown>>('/api/flows'),
  status: () => request<Status>('/api/status'),
  events: (limit = 100) => request<SdnEvent[]>(`/api/events?limit=${limit}`),
  predictions: () =>
    request<{ available: boolean; reason?: string; links: unknown[] }>('/api/predictions'),
}

export function liveSocket(onFrame: (f: LiveFrame) => void, onClose: () => void): WebSocket {
  const access = tokens.access()
  const url = `${BASE.replace(/^http/, 'ws')}/ws/live?token=${encodeURIComponent(access ?? '')}`
  const socket = new WebSocket(url)
  socket.onmessage = (event) => {
    try {
      onFrame(JSON.parse(event.data) as LiveFrame)
    } catch {
      /* a malformed frame should not tear down the stream */
    }
  }
  socket.onclose = onClose
  return socket
}

export { BASE as API_BASE }