import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { ConversationHistoryItem } from '../types'
import { HistoryRecord } from './ConversationHistory'

function messageRecord(role: 'user' | 'assistant'): ConversationHistoryItem {
  return {
    sequence: role === 'user' ? 1 : 2,
    turn_id: 'turn-1',
    created_at: '31-07-2026T12:00:00Z',
    payload: {
      type: 'message',
      role,
      content: role === 'user' ? 'Hola' : '¿En qué puedo ayudarte?',
      source: role === 'user' ? 'text_user' : 'assistant',
      metrics: null,
    },
  }
}

describe('technical history records', () => {
  it('marks user and assistant messages for opposite alignment', () => {
    const userMarkup = renderToStaticMarkup(<HistoryRecord record={messageRecord('user')} />)
    const assistantMarkup = renderToStaticMarkup(
      <HistoryRecord record={messageRecord('assistant')} />,
    )

    expect(userMarkup).toContain('history-record-message-user')
    expect(assistantMarkup).toContain('history-record-message-assistant')
  })

  it('collapses tool calls behind their tool name', () => {
    const record: ConversationHistoryItem = {
      sequence: 3,
      turn_id: 'turn-1',
      created_at: '31-07-2026T12:00:01Z',
      payload: {
        type: 'tool_call',
        call_id: 'call-123',
        tool_name: 'recent_transactions',
        arguments: { limit: 10 },
      },
    }

    const response: ConversationHistoryItem = {
      sequence: 4,
      turn_id: 'turn-1',
      created_at: '31-07-2026T12:00:02Z',
      payload: {
        type: 'tool_result',
        call_id: 'call-123',
        output: { transactions: [] },
        error: null,
      },
    }

    const markup = renderToStaticMarkup(
      <HistoryRecord record={record} toolResult={response} />,
    )

    expect(markup).toContain('<details class="history-tool-call">')
    expect(markup).not.toContain('<details class="history-tool-call" open="">')
    expect(markup).toContain('<strong>recent_transactions</strong>')
    expect(markup).toContain('Call ID')
    expect(markup).toContain('call-123')
    expect(markup).toContain('Respuesta')
    expect(markup).toContain('transactions')
  })

  it('keeps orphan tool responses collapsed as well', () => {
    const response: ConversationHistoryItem = {
      sequence: 5,
      turn_id: 'turn-2',
      created_at: '31-07-2026T12:01:00Z',
      payload: {
        type: 'tool_result',
        call_id: 'call-orphan',
        output: null,
        error: 'Timeout',
      },
    }

    const markup = renderToStaticMarkup(<HistoryRecord record={response} />)

    expect(markup).toContain('<details class="history-tool-call history-orphan-tool-result">')
    expect(markup).not.toContain(' open=""')
    expect(markup).toContain('Respuesta de herramienta')
  })
})
