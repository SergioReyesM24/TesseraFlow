import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { ConversationHistoryItem, EvaluationTrace } from '../types'
import { EvaluationTraceCard, HistoryRecord, TurnRecords } from './ConversationHistory'

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
  it('renders a persisted evaluator decision without raw conversation evidence', () => {
    const trace: EvaluationTrace = {
      trace_id: 'trace-1',
      conversation_id: 'conversation-1',
      turn_id: 'turn-1',
      job_id: 'turn-1',
      attempt: 2,
      proposed_call_ids: ['call-1'],
      verdict: 'fail',
      risk: 'low',
      reason_code: 'wrong_tool',
      feedback: 'Selecciona una herramienta adecuada para la petición actual.',
      mode: 'enforce',
      executed: false,
      model: 'gpt-5-mini',
      usage: {
        input_tokens: 100,
        output_tokens: 20,
        total_tokens: 120,
        cached_input_tokens: 0,
        uncached_input_tokens: 100,
        cached_input_audio_tokens: 0,
        reasoning_tokens: 0,
        input_audio_tokens: 0,
        output_audio_tokens: 0,
      },
      latency_ms: 125.5,
      created_at: '31-07-2026T12:00:00Z',
    }

    const markup = renderToStaticMarkup(<EvaluationTraceCard trace={trace} />)

    expect(markup).toContain('Evaluación #2')
    expect(markup).toContain('wrong_tool')
    expect(markup).toContain('Bloqueada')
    expect(markup).toContain('gpt-5-mini')
    expect(markup).toContain('call-1')
  })

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

  it('pairs tool calls and results inside the same turn disclosure', () => {
    const call: ConversationHistoryItem = {
      sequence: 3,
      turn_id: 'turn-1',
      created_at: '31-07-2026T12:00:01Z',
      payload: {
        type: 'tool_call',
        call_id: 'call-paired',
        tool_name: 'recent_transactions',
        arguments: { limit: 5 },
      },
    }
    const result: ConversationHistoryItem = {
      sequence: 4,
      turn_id: 'turn-1',
      created_at: '31-07-2026T12:00:02Z',
      payload: {
        type: 'tool_result',
        call_id: 'call-paired',
        output: { count: 5 },
        error: null,
      },
    }

    const markup = renderToStaticMarkup(<TurnRecords records={[call, result]} />)

    expect(markup).toContain('recent_transactions')
    expect(markup).toContain('&quot;count&quot;: 5')
    expect(markup).not.toContain('history-orphan-tool-result')
  })
})
