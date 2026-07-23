import type {
  ConversationGroupResponse,
  ConversationHistoryResponse,
  ConversationListResponse,
  SessionResponse,
} from '../types'

/** Remove trailing slashes so endpoint composition stays stable. */
export function normalizeBaseUrl(baseUrl: string): string {
  return baseUrl.trim().replace(/\/+$/, '')
}

/** Build an HTTP URL from an optional runtime API origin. */
export function httpUrl(path: string, baseUrl: string): string {
  const normalized = normalizeBaseUrl(baseUrl)
  return normalized ? `${normalized}${path}` : path
}

/** Build a browser-compatible WebSocket URL from the configured HTTP origin. */
export function websocketUrl(path: string, baseUrl: string): string {
  const normalized = normalizeBaseUrl(baseUrl)
  const url = new URL(path, normalized || window.location.origin)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString()
}

/** Create one owned backend conversation before opening either socket. */
export async function createSession(baseUrl: string, userId: string): Promise<string> {
  const response = await fetch(httpUrl('/v1/sessions', baseUrl), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: userId }),
  })

  if (!response.ok) {
    throw new Error(`No se pudo crear la sesión (${response.status}).`)
  }

  const payload = (await response.json()) as SessionResponse
  return payload.session_uid
}

/** Load one bounded page of canonical database history for an owned session. */
export async function loadConversationHistory(
  baseUrl: string,
  userId: string,
  sessionUid: string,
  afterSequence = 0,
  limit = 50,
  signal?: AbortSignal,
): Promise<ConversationHistoryResponse> {
  const query = new URLSearchParams({
    user_id: userId,
    after_sequence: String(afterSequence),
    limit: String(limit),
  })
  const response = await fetch(
    httpUrl(`/v1/sessions/${encodeURIComponent(sessionUid)}/history?${query}`, baseUrl),
    { signal },
  )

  if (!response.ok) {
    if (response.status === 404) throw new Error('No existe una sesión con ese identificador.')
    if (response.status === 403) throw new Error('La sesión pertenece a otro usuario.')
    throw new Error(`No se pudo cargar el historial técnico (${response.status}).`)
  }

  return (await response.json()) as ConversationHistoryResponse
}

/** Resolve the root conversation and every isolated A2A worker session. */
export async function loadConversationGroup(
  baseUrl: string,
  userId: string,
  sessionUid: string,
  signal?: AbortSignal,
): Promise<ConversationGroupResponse> {
  const query = new URLSearchParams({ user_id: userId })
  const response = await fetch(
    httpUrl(`/v1/sessions/${encodeURIComponent(sessionUid)}/group?${query}`, baseUrl),
    { signal },
  )

  if (!response.ok) {
    if (response.status === 404) throw new Error('No existe una sesión con ese identificador.')
    if (response.status === 403) throw new Error('La sesión pertenece a otro usuario.')
    throw new Error(`No se pudo cargar el grupo de conversaciones (${response.status}).`)
  }

  return (await response.json()) as ConversationGroupResponse
}

/** List a bounded page of persisted sessions owned by the configured user. */
export async function listConversationSessions(
  baseUrl: string,
  userId: string,
  offset = 0,
  limit = 50,
  signal?: AbortSignal,
): Promise<ConversationListResponse> {
  const query = new URLSearchParams({
    user_id: userId,
    offset: String(offset),
    limit: String(limit),
  })
  const response = await fetch(httpUrl(`/v1/sessions?${query}`, baseUrl), { signal })

  if (!response.ok) {
    throw new Error(`No se pudo cargar la lista de sesiones (${response.status}).`)
  }

  return (await response.json()) as ConversationListResponse
}
