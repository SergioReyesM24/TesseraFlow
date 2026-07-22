import { Bot, Check, LoaderCircle, Wrench, X } from 'lucide-react'
import { useEffect, useRef } from 'react'
import type { ConversationMessage } from '../types'

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
              <ToolRow message={message} />
              {message.status === 'error' && <small className="message-error">Error</small>}
            </div>
          </article>
        ))}
        <div ref={endRef} />
      </div>
    </div>
  )
}
