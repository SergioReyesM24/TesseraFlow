import { useCallback, useEffect, useRef, useState } from 'react'
import { websocketUrl } from '../lib/api'
import { base64ToArrayBuffer, PcmPlayer } from '../lib/audio'
import { mergeVisual, parseVisualPresentation } from '../lib/visuals'
import type {
  ConnectionState,
  ConversationMessage,
  SocketEnvelope,
  ToolActivity,
} from '../types'

interface AgentSocketOptions {
  apiBaseUrl: string
  sessionUid: string
  userId: string
  enabled: boolean
}

interface AgentSocketController {
  messages: ConversationMessage[]
  connection: ConnectionState
  error: string | null
  sendMessage: (message: string) => boolean
}

/** Insert or mutate the assistant message correlated to one durable request. */
function updateAssistantMessage(
  current: ConversationMessage[],
  requestId: string,
  mutate: (message: ConversationMessage) => ConversationMessage,
): ConversationMessage[] {
  const id = `assistant-${requestId}`
  const index = current.findIndex((message) => message.id === id)
  if (index === -1) {
    return [
      ...current,
      mutate({ id, role: 'assistant', content: '', status: 'streaming', tools: [] }),
    ]
  }
  return current.map((message, messageIndex) =>
    messageIndex === index ? mutate(message) : message,
  )
}

/** Convert a tool event into the compact activity shown beneath a response. */
function mergeTool(
  tools: ToolActivity[] | undefined,
  incoming: ToolActivity,
): ToolActivity[] {
  const current = tools ?? []
  const exists = current.some((tool) => tool.callId === incoming.callId)
  return exists
    ? current.map((tool) => (tool.callId === incoming.callId ? incoming : tool))
    : [...current, incoming]
}

/** Manage the durable text WebSocket, including streamed deltas and reconnects. */
export function useAgentSocket(options: AgentSocketOptions): AgentSocketController {
  const { apiBaseUrl, sessionUid, userId, enabled } = options
  const [messages, setMessages] = useState<ConversationMessage[]>([])
  const [connection, setConnection] = useState<ConnectionState>('disconnected')
  const [error, setError] = useState<string | null>(null)
  const socketRef = useRef<WebSocket | null>(null)
  const [player] = useState(() => new PcmPlayer())

  const handleEnvelope = useCallback(
    (event: SocketEnvelope) => {
      if (event.type === 'connected') {
        setConnection('connected')
        setError(null)
        return
      }
      const requestId = event.request_id ?? crypto.randomUUID()
      const data = event.data ?? {}

      if (event.type === 'text_delta' && typeof data.text === 'string') {
        setMessages((current) =>
          updateAssistantMessage(current, requestId, (message) => ({
            ...message,
            content: message.content + data.text,
            status: 'streaming',
          })),
        )
      } else if (event.type === 'tool_started') {
        const callId = String(data.call_id ?? crypto.randomUUID())
        setMessages((current) =>
          updateAssistantMessage(current, requestId, (message) => ({
            ...message,
            tools: mergeTool(message.tools, {
              callId,
              name: String(data.tool_name ?? 'Herramienta'),
              status: 'running',
            }),
          })),
        )
      } else if (event.type === 'tool_completed') {
        const callId = String(data.call_id ?? crypto.randomUUID())
        setMessages((current) =>
          updateAssistantMessage(current, requestId, (message) => ({
            ...message,
            tools: mergeTool(message.tools, {
              callId,
              name: String(data.tool_name ?? 'Herramienta'),
              status: data.status === 'error' ? 'error' : 'success',
              durationMs: typeof data.duration_ms === 'number' ? data.duration_ms : undefined,
            }),
          })),
        )
      } else if (event.type === 'visual_component') {
        const visual = parseVisualPresentation(data)
        if (!visual) return
        setMessages((current) =>
          updateAssistantMessage(current, requestId, (message) => ({
            ...message,
            visuals: mergeVisual(message.visuals, visual),
          })),
        )
      } else if (event.type === 'completed') {
        setMessages((current) =>
          updateAssistantMessage(current, requestId, (message) => ({
            ...message,
            content: typeof data.answer === 'string' ? data.answer : message.content,
            status: 'complete',
          })),
        )
      } else if (event.type === 'error') {
        const message = String(data.message ?? 'La respuesta no pudo completarse.')
        setMessages((current) =>
          updateAssistantMessage(current, requestId, (assistant) => ({
            ...assistant,
            content: assistant.content || message,
            status: 'error',
          })),
        )
      } else if (event.type === 'audio_delta' && typeof data.audio === 'string') {
        void player.enqueue(base64ToArrayBuffer(data.audio), 24_000)
      } else if (event.type === 'audio_interrupted') {
        player.clear()
      }
    },
    [player],
  )

  useEffect(() => {
    if (!enabled) {
      socketRef.current?.close(1000, 'mode changed')
      socketRef.current = null
      return
    }

    let disposed = false
    let reconnectTimer: number | undefined
    let attempts = 0

    const connect = () => {
      setConnection('connecting')
      const query = new URLSearchParams({ session_uid: sessionUid, user_id: userId })
      const socket = new WebSocket(
        websocketUrl(`/v1/agent/ws?${query.toString()}`, apiBaseUrl),
      )
      socketRef.current = socket

      socket.onmessage = (message) => {
        if (typeof message.data !== 'string') return
        try {
          handleEnvelope(JSON.parse(message.data) as SocketEnvelope)
        } catch {
          setError('El servidor envió un evento que no se pudo interpretar.')
        }
      }
      socket.onerror = () => setError('No se pudo conectar con el canal de texto.')
      socket.onopen = () => {
        attempts = 0
      }
      socket.onclose = (closeEvent) => {
        if (socketRef.current === socket) socketRef.current = null
        if (disposed) return
        setConnection('disconnected')
        if (closeEvent.code === 1008) {
          setError(closeEvent.reason || 'El servidor rechazó esta sesión.')
          return
        }
        const delay = Math.min(1000 * 2 ** attempts, 8000)
        attempts += 1
        reconnectTimer = window.setTimeout(connect, delay)
      }
    }

    connect()
    return () => {
      disposed = true
      if (reconnectTimer) window.clearTimeout(reconnectTimer)
      socketRef.current?.close(1000, 'view disposed')
      socketRef.current = null
    }
  }, [apiBaseUrl, enabled, handleEnvelope, sessionUid, userId])

  useEffect(() => () => void player.close(), [player])

  const sendMessage = useCallback((message: string): boolean => {
    const socket = socketRef.current
    const content = message.trim()
    if (!content || socket?.readyState !== WebSocket.OPEN) return false
    const requestId = crypto.randomUUID()
    socket.send(JSON.stringify({ type: 'message', request_id: requestId, message: content }))
    setMessages((current) => [
      ...current,
      { id: `user-${requestId}`, role: 'user', content, status: 'complete' },
      {
        id: `assistant-${requestId}`,
        role: 'assistant',
        content: '',
        status: 'streaming',
        tools: [],
      },
    ])
    return true
  }, [])

  return {
    messages,
    connection: enabled ? connection : 'disconnected',
    error,
    sendMessage,
  }
}
