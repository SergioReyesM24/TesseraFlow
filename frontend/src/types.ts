export type Mode = 'text' | 'voice' | 'history'

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
  visuals?: VisualPresentation[]
}

export interface ChartPoint {
  x: string
  y: number
}

export interface ChartSeries {
  name: string
  points: ChartPoint[]
}

export interface ChartVisualComponent {
  kind: 'chart'
  title: string
  subtitle: string | null
  chart_type: 'line' | 'bar'
  x_axis: { label: string | null }
  y_axis: { label: string | null; unit: string | null }
  series: ChartSeries[]
}

export interface MetricVisual {
  label: string
  value: string
  unit: string | null
  detail: string | null
}

export interface MetricGroupVisualComponent {
  kind: 'metric_group'
  title: string
  subtitle: string | null
  metrics: MetricVisual[]
}

export type VisualComponent = ChartVisualComponent | MetricGroupVisualComponent

export interface VisualPresentation {
  componentId: string
  fallbackText: string
  component: VisualComponent | null
}

export interface SocketEnvelope {
  type: string
  request_id?: string | null
  data?: Record<string, unknown>
}

export interface SessionResponse {
  session_uid: string
}

export interface ConversationMessageHistoryPayload {
  type: 'message'
  role: 'user' | 'assistant'
  content: string
  source: 'text_user' | 'speech_user' | 'worker_agent' | 'assistant'
}

export interface ToolCallHistoryPayload {
  type: 'tool_call'
  call_id: string
  tool_name: string
  arguments: Record<string, unknown>
}

export interface ToolResultHistoryPayload {
  type: 'tool_result'
  call_id: string
  output: unknown
  error: string | null
}

export type ConversationHistoryPayload =
  | ConversationMessageHistoryPayload
  | ToolCallHistoryPayload
  | ToolResultHistoryPayload

export interface ConversationHistoryItem {
  sequence: number
  turn_id: string
  created_at: string
  payload: ConversationHistoryPayload
}

export interface ConversationCorrelation {
  conversation_id: string
  root_conversation_id: string
  parent_conversation_id: string | null
  worker_conversation_id: string | null
  thread_id: string | null
}

export interface ConversationJobCorrelation {
  job_id: string
  request_id: string
  turn_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
}

export interface ConversationGroupMember {
  correlation: ConversationCorrelation
  jobs: ConversationJobCorrelation[]
}

export interface ConversationGroupResponse {
  user_id: string
  root_conversation_id: string
  conversations: ConversationGroupMember[]
}

export interface ConversationHistoryResponse {
  session_uid: string
  user_id: string
  title: string
  status: 'active' | 'archived'
  version: number
  last_sequence: number
  created_at: string
  updated_at: string
  last_message_at: string | null
  items: ConversationHistoryItem[]
  has_more: boolean
  next_after_sequence: number | null
  correlation: ConversationCorrelation
}

export interface ConversationSummary {
  session_uid: string
  title: string
  status: 'active' | 'archived'
  version: number
  last_sequence: number
  created_at: string
  updated_at: string
  last_message_at: string | null
  correlation: ConversationCorrelation
}

export interface ConversationListResponse {
  user_id: string
  sessions: ConversationSummary[]
  has_more: boolean
  next_offset: number | null
}
