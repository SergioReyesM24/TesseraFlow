export type Mode = 'text' | 'voice'

export type ConnectionState = 'connecting' | 'connected' | 'disconnected' | 'error'

export interface ToolActivity {
  callId: string
  name: string
  status: 'running' | 'success' | 'error'
  durationMs?: number
}

export interface ConversationMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  status?: 'streaming' | 'complete' | 'error'
  tools?: ToolActivity[]
}

export interface SocketEnvelope {
  type: string
  request_id?: string | null
  data?: Record<string, unknown>
}

export interface SessionResponse {
  session_uid: string
}
