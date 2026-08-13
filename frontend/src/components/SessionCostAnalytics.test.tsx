import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { SessionUsageReport } from '../types'
import { SessionCostAnalytics } from './SessionCostAnalytics'

const report: SessionUsageReport = {
  user_id: 'user-1',
  root_conversation_id: 'root-session',
  usage: {
    input_tokens: 3_000,
    output_tokens: 700,
    total_tokens: 3_700,
    cached_input_tokens: 800,
    uncached_input_tokens: 2_200,
    cached_input_audio_tokens: 0,
    reasoning_tokens: 0,
    input_audio_tokens: 0,
    output_audio_tokens: 0,
  },
  cost: { amount: 0.0047, currency: 'USD' },
  turn_count: 3,
  model_call_count: 4,
  fully_priced: true,
  models: [
    {
      model: 'gpt-5-mini',
      usage: {
        input_tokens: 3_000,
        output_tokens: 700,
        total_tokens: 3_700,
        cached_input_tokens: 800,
        uncached_input_tokens: 2_200,
        cached_input_audio_tokens: 0,
        reasoning_tokens: 0,
        input_audio_tokens: 0,
        output_audio_tokens: 0,
      },
      cost: { amount: 0.0047, currency: 'USD' },
      model_call_count: 4,
      fully_priced: true,
    },
  ],
  conversations: [
    {
      conversation_id: 'root-session',
      title: 'Principal',
      role: 'interactive',
      thread_id: null,
      usage: {
        input_tokens: 1_000,
        output_tokens: 200,
        total_tokens: 1_200,
        cached_input_tokens: 0,
        uncached_input_tokens: 1_000,
        cached_input_audio_tokens: 0,
        reasoning_tokens: 0,
        input_audio_tokens: 0,
        output_audio_tokens: 0,
      },
      cost: { amount: 0.0012, currency: 'USD' },
      turn_count: 2,
      model_call_count: 2,
      fully_priced: true,
      models: [],
    },
    {
      conversation_id: 'worker-session',
      title: 'Worker',
      role: 'worker',
      thread_id: 'thread-1',
      usage: {
        input_tokens: 2_000,
        output_tokens: 500,
        total_tokens: 2_500,
        cached_input_tokens: 800,
        uncached_input_tokens: 1_200,
        cached_input_audio_tokens: 0,
        reasoning_tokens: 0,
        input_audio_tokens: 0,
        output_audio_tokens: 0,
      },
      cost: { amount: 0.0035, currency: 'USD' },
      turn_count: 1,
      model_call_count: 2,
      fully_priced: true,
      models: [],
    },
  ],
}

describe('SessionCostAnalytics', () => {
  it('renders global, worker, and model cost breakdowns', () => {
    const markup = renderToStaticMarkup(
      <SessionCostAnalytics report={report} loading={false} error={null} />,
    )

    expect(markup).toContain('Coste de agente y workers')
    expect(markup).toContain('0,0047 USD')
    expect(markup).toContain('Agente usuario')
    expect(markup).toContain('Worker 1')
    expect(markup).toContain('gpt-5-mini')
    expect(markup).toContain('Coste por participante')
    expect(markup).toContain('Tokens por modelo')
  })

  it('shows partial pricing when a model call has no configured rate', () => {
    const markup = renderToStaticMarkup(
      <SessionCostAnalytics
        report={{ ...report, cost: null, fully_priced: false }}
        loading={false}
        error={null}
      />,
    )

    expect(markup).toContain('Parcial')
    expect(markup).toContain('Faltan tarifas')
    expect(markup).not.toContain('Coste por participante')
  })

  it('renders only KPIs in compact session mode', () => {
    const markup = renderToStaticMarkup(
      <SessionCostAnalytics report={report} loading={false} error={null} variant="kpis" />,
    )

    expect(markup).toContain('Resumen económico de la sesión')
    expect(markup).toContain('0,0047 USD')
    expect(markup).not.toContain('Principal vs workers')
    expect(markup).not.toContain('Coste por participante')
    expect(markup).not.toContain('Tokens por modelo')
  })
})
