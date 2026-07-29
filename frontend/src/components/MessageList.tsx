import { Bot, Check, CircleDollarSign, LoaderCircle, Wrench, X } from 'lucide-react'
import { useEffect, useRef } from 'react'
import { formatModelCost } from '../lib/costs'
import type { ConversationMessage } from '../types'
import { VisualPresentation } from './VisualPresentation'

interface MessageListProps {
  messages: ConversationMessage[]
  emptyTitle: string
  emptyDescription: string
  children?: React.ReactNode
}

/** Render a stable activity row for model tool execution. */
function ToolRow({ message }: { message: ConversationMessage }) {
  if (!message.tools?.length) return null
  return (
    <div className="tool-list" aria-label="Actividad de herramientas">
      {message.tools.map((tool) => (
        <div className="tool-row" key={tool.callId}>
          {tool.status === 'running' ? (
            <LoaderCircle size={14} className="spin" aria-hidden="true" />
          ) : tool.status === 'success' ? (
            <Check size={14} aria-hidden="true" />
          ) : (
            <X size={14} aria-hidden="true" />
          )}
          <Wrench size={13} aria-hidden="true" />
          <span>{tool.name}</span>
          {tool.durationMs !== undefined && <small>{Math.round(tool.durationMs)} ms</small>}
        </div>
      ))}
    </div>
  )
}

/** Show the turn-level cost while keeping token and request detail inspectable. */
function CostRow({ message }: { message: ConversationMessage }) {
  const metrics = message.metrics
  if (!metrics) return null
  const cost = metrics.cost ? formatModelCost(metrics.cost) : 'Tarifa no configurada'
  return (
    <div
      className="cost-row"
      title={`${metrics.usage.uncached_input_tokens.toLocaleString('es-ES')} entrada no cacheada · ${metrics.usage.cached_input_tokens.toLocaleString('es-ES')} entrada cacheada · ${metrics.usage.output_tokens.toLocaleString('es-ES')} salida · ${metrics.calls.length} llamadas`}
    >
      <CircleDollarSign size={14} aria-hidden="true" />
      <span>{cost}</span>
      <small>{metrics.usage.total_tokens.toLocaleString('es-ES')} tokens</small>
    </div>
  )
}

/** Present the conversation and keep the newest streamed content in view. */
export function MessageList({
  messages,
  emptyTitle,
  emptyDescription,
  children,
}: MessageListProps) {
  const endRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages])

  if (messages.length === 0) {
    return (
      <div className="conversation-scroll empty-conversation">
        {children}
        <h2>{emptyTitle}</h2>
        <p>{emptyDescription}</p>
      </div>
    )
  }

  return (
    <div className="conversation-scroll" aria-live="polite">
      <div className="message-stack">
        {messages.map((message) => (
          <article className={`message message-${message.role}`} key={message.id}>
            {message.role === 'assistant' && (
              <div className="assistant-avatar" aria-hidden="true">
                <Bot size={16} />
              </div>
            )}
            <div className="message-content">
              {message.content ? (
                <p>{message.content}</p>
              ) : (
                <span className="typing-dots" aria-label="TesseraFlow está respondiendo">
                  <i />
                  <i />
                  <i />
                </span>
              )}
              {message.visuals?.map((visual) => (
                <VisualPresentation key={visual.componentId} presentation={visual} />
              ))}
              <ToolRow message={message} />
              {message.role === 'assistant' && <CostRow message={message} />}
              {message.status === 'error' && <small className="message-error">Error</small>}
            </div>
          </article>
        ))}
        <div ref={endRef} />
      </div>
    </div>
  )
}
