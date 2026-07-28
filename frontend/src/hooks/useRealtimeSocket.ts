import { useCallback, useEffect, useRef, useState } from 'react'
import { websocketUrl } from '../lib/api'
import { MicrophoneCapture, PcmPlayer } from '../lib/audio'
import { mergeVisual, parseVisualPresentation } from '../lib/visuals'
import type {
  ConnectionState,
  ConversationMessage,
  SocketEnvelope,
  ToolActivity,
} from '../types'

interface RealtimeSocketOptions {
  apiBaseUrl: string
  sessionUid: string
  userId: string
  enabled: boolean
}

interface RealtimeSocketController {
  messages: ConversationMessage[]
  connection: ConnectionState
  error: string | null
  ready: boolean
  recording: boolean
  activateAudio: () => Promise<void>
  startRecording: () => Promise<void>
  stopRecording: () => Promise<void>
  sendText: (message: string) => boolean
}

/** Update the transcript bubble for one role and realtime turn. */
function updateTurnMessage(
  current: ConversationMessage[],
  turnId: string,
  role: 'user' | 'assistant',
  mutate: (message: ConversationMessage) => ConversationMessage,
): ConversationMessage[] {
  const id = `voice-${role}-${turnId}`
  const index = current.findIndex((message) => message.id === id)
  if (index === -1) {
    return [...current, mutate({ id, role, content: '', status: 'streaming', tools: [] })]
  }
  return current.map((message, messageIndex) =>
    messageIndex === index ? mutate(message) : message,
  )
}

/** Merge one tool state into its owning realtime assistant bubble. */
function mergeTool(tools: ToolActivity[] | undefined, incoming: ToolActivity): ToolActivity[] {
  const current = tools ?? []
  return current.some((tool) => tool.callId === incoming.callId)
    ? current.map((tool) => (tool.callId === incoming.callId ? incoming : tool))
    : [...current, incoming]
}

/** Manage microphone capture, PCM playback and semantic realtime events. */
export function useRealtimeSocket(
  options: RealtimeSocketOptions,
): RealtimeSocketController {
  const { apiBaseUrl, sessionUid, userId, enabled } = options
  const [messages, setMessages] = useState<ConversationMessage[]>([])
  const [connection, setConnection] = useState<ConnectionState>('disconnected')
  const [error, setError] = useState<string | null>(null)
  const [ready, setReady] = useState(false)
  const [recording, setRecording] = useState(false)
  const socketRef = useRef<WebSocket | null>(null)
  const currentTurnRef = useRef<string | null>(null)
  const activityModeRef = useRef<'automatic' | 'explicit'>('automatic')
  const outputSampleRateRef = useRef(24_000)
  const readyRef = useRef(false)
  const [capture] = useState(() => new MicrophoneCapture())
  const [player] = useState(() => new PcmPlayer())

  const handleEnvelope = useCallback(
    (event: SocketEnvelope) => {
      const data = event.data ?? {}
      if (event.type === 'connected') {
        setConnection('connected')
        setError(null)
        return
      }
      if (event.type === 'realtime_ready') {
        activityModeRef.current = data.activity_detection === 'explicit' ? 'explicit' : 'automatic'
        if (typeof data.output_audio === 'string') {
          const rate = /(?:^|;)rate=(\d+)(?:;|$)/.exec(data.output_audio)?.[1]
          if (rate) outputSampleRateRef.current = Number(rate)
        }
        readyRef.current = true
        setConnection('connected')
        setReady(true)
        setError(null)
        return
      }
      const turnId = String(data.turn_id ?? currentTurnRef.current ?? crypto.randomUUID())
      if (event.type === 'input_transcript_delta' && typeof data.text === 'string') {
        setMessages((current) =>
          updateTurnMessage(current, turnId, 'user', (message) => ({
            ...message,
            content: message.content + data.text,
            status: 'streaming',
          })),
        )
      } else if (event.type === 'output_transcript_delta' && typeof data.text === 'string') {
        setMessages((current) =>
          updateTurnMessage(current, turnId, 'assistant', (message) => ({
            ...message,
            content: message.content + data.text,
            status: 'streaming',
          })),
        )
      } else if (event.type === 'tool_started') {
        const tool: ToolActivity = {
          callId: String(data.call_id ?? crypto.randomUUID()),
          name: String(data.tool_name ?? 'Herramienta'),
          status: 'running',
        }
        setMessages((current) =>
          updateTurnMessage(current, turnId, 'assistant', (message) => ({
            ...message,
            tools: mergeTool(message.tools, tool),
          })),
        )
      } else if (event.type === 'tool_completed') {
        const tool: ToolActivity = {
          callId: String(data.call_id ?? crypto.randomUUID()),
          name: String(data.tool_name ?? 'Herramienta'),
          status: data.status === 'error' ? 'error' : 'success',
          durationMs: typeof data.duration_ms === 'number' ? data.duration_ms : undefined,
        }
        setMessages((current) =>
          updateTurnMessage(current, turnId, 'assistant', (message) => ({
            ...message,
            tools: mergeTool(message.tools, tool),
          })),
        )
      } else if (event.type === 'visual_component') {
        const visual = parseVisualPresentation(data)
        if (!visual) return
        setMessages((current) =>
          updateTurnMessage(current, turnId, 'assistant', (message) => ({
            ...message,
            visuals: mergeVisual(message.visuals, visual),
          })),
        )
      } else if (event.type === 'turn_completed') {
        setMessages((current) => {
          const withAssistant = updateTurnMessage(current, turnId, 'assistant', (message) => ({
            ...message,
            content: typeof data.answer === 'string' ? data.answer : message.content,
            status: 'complete',
          }))
          if (data.source === 'worker_agent') return withAssistant
          return updateTurnMessage(withAssistant, turnId, 'user', (message) => ({
            ...message,
            status: 'complete',
          }))
        })
        if (currentTurnRef.current === turnId) currentTurnRef.current = null
      } else if (event.type === 'audio_interrupted') {
        player.clear()
      } else if (event.type === 'reconnecting') {
        readyRef.current = false
        void capture.stop()
        setRecording(false)
        setReady(false)
        setConnection('connecting')
      } else if (event.type === 'reconnected') {
        readyRef.current = true
        setReady(true)
        setConnection('connected')
      } else if (event.type === 'error') {
        setError(String(data.message ?? 'La sesión de voz no pudo continuar.'))
      }
    },
    [capture, player],
  )

  const activateAudio = useCallback(async (): Promise<void> => {
    try {
      await player.resume()
      setError(null)
    } catch (cause) {
      const detail = cause instanceof Error ? ` (${cause.message})` : ''
      setError(`El navegador bloqueó la salida de audio${detail}.`)
      throw cause
    }
  }, [player])

  const playAudio = useCallback(
    (payload: ArrayBuffer | Blob): void => {
      void (async () => {
        const bytes = payload instanceof Blob ? await payload.arrayBuffer() : payload
        await player.enqueue(bytes, outputSampleRateRef.current)
      })().catch((cause: unknown) => {
        const detail = cause instanceof Error ? ` (${cause.message})` : ''
        setError(`Se recibió audio, pero no pudo reproducirse${detail}.`)
      })
    },
    [player],
  )

  useEffect(() => {
    if (!enabled) {
      readyRef.current = false
      socketRef.current?.close(1000, 'mode changed')
      socketRef.current = null
      void capture.stop()
      return
    }

    let disposed = false
    let reconnectTimer: number | undefined
    let attempts = 0

    const connect = () => {
      setConnection('connecting')
      setReady(false)
      const query = new URLSearchParams({ session_uid: sessionUid, user_id: userId })
      const socket = new WebSocket(
        websocketUrl(`/v1/agent/realtime?${query.toString()}`, apiBaseUrl),
      )
      socket.binaryType = 'arraybuffer'
      socketRef.current = socket
      socket.onopen = () => {
        attempts = 0
      }
      socket.onmessage = (message) => {
        if (message.data instanceof ArrayBuffer || message.data instanceof Blob) {
          playAudio(message.data)
          return
        }
        if (typeof message.data !== 'string') return
        try {
          handleEnvelope(JSON.parse(message.data) as SocketEnvelope)
        } catch {
          setError('El servidor envió un evento realtime no reconocido.')
        }
      }
      socket.onerror = () => setError('No se pudo conectar con el canal realtime.')
      socket.onclose = (closeEvent) => {
        readyRef.current = false
        if (socketRef.current === socket) socketRef.current = null
        void capture.stop()
        setRecording(false)
        setReady(false)
        if (disposed) return
        setConnection('disconnected')
        if (closeEvent.code === 1008) {
          setError(closeEvent.reason || 'El backend no tiene habilitado el modo de voz.')
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
      void capture.stop()
    }
  }, [apiBaseUrl, capture, enabled, handleEnvelope, playAudio, sessionUid, userId])

  useEffect(() => () => void player.close(), [player])

  const startRecording = useCallback(async (): Promise<void> => {
    const socket = socketRef.current
    if (!ready || recording || socket?.readyState !== WebSocket.OPEN) return
    setError(null)
    try {
      await activateAudio()
    } catch {
      return
    }
    try {
      const pendingChunks: ArrayBuffer[] = []
      let audioStarted = false
      await capture.start((chunk) => {
        if (!audioStarted) {
          pendingChunks.push(chunk)
          return
        }
        const activeSocket = socketRef.current
        if (readyRef.current && activeSocket?.readyState === WebSocket.OPEN) {
          activeSocket.send(chunk)
        }
      })
      const turnId = crypto.randomUUID()
      currentTurnRef.current = turnId
      socket.send(JSON.stringify({ type: 'audio_start', turn_id: turnId }))
      if (activityModeRef.current === 'explicit') {
        socket.send(JSON.stringify({ type: 'activity_start' }))
      }
      audioStarted = true
      for (const chunk of pendingChunks) {
        if (socket.readyState === WebSocket.OPEN) socket.send(chunk)
      }
      setRecording(true)
    } catch {
      await capture.stop()
      setError('No se pudo acceder al micrófono. Revisa el permiso del navegador.')
    }
  }, [activateAudio, capture, ready, recording])

  const stopRecording = useCallback(async (): Promise<void> => {
    if (!recording) return
    await capture.stop()
    const socket = socketRef.current
    if (socket?.readyState === WebSocket.OPEN) {
      if (activityModeRef.current === 'explicit') {
        socket.send(JSON.stringify({ type: 'activity_end' }))
      }
      socket.send(JSON.stringify({ type: 'audio_end' }))
    }
    setRecording(false)
  }, [capture, recording])

  const sendText = useCallback((message: string): boolean => {
    const socket = socketRef.current
    const content = message.trim()
    if (!readyRef.current || !content || socket?.readyState !== WebSocket.OPEN) return false
    void activateAudio().catch(() => undefined)
    const turnId = crypto.randomUUID()
    currentTurnRef.current = turnId
    socket.send(JSON.stringify({ type: 'text', turn_id: turnId, text: content }))
    setMessages((current) => [
      ...current,
      {
        id: `voice-user-${turnId}`,
        role: 'user',
        content,
        status: 'complete',
      },
    ])
    return true
  }, [activateAudio])

  return {
    messages,
    connection: enabled ? connection : 'disconnected',
    error,
    ready: enabled && ready,
    recording: enabled && recording,
    activateAudio,
    startRecording,
    stopRecording,
    sendText,
  }
}
