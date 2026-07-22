import type { SessionResponse } from '../types'

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
