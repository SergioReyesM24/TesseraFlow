import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { DailyTokenUsageReport } from '../types'
import { DailyTokenAnalytics } from './DailyTokenAnalytics'

describe('DailyTokenAnalytics', () => {
  it('aggregates global usage and renders every daily series', () => {
    const report: DailyTokenUsageReport = {
      user_id: 'user-1',
      timezone: 'UTC',
      days: [
        {
          date: '30-07-2026',
          usage: {
            input_tokens: 1_000,
            output_tokens: 200,
            total_tokens: 1_200,
            cached_input_tokens: 400,
            uncached_input_tokens: 600,
            cached_input_audio_tokens: 0,
            reasoning_tokens: 0,
            input_audio_tokens: 0,
            output_audio_tokens: 0,
          },
          cost: { amount: 0.0012, currency: 'USD' },
          turn_count: 3,
          model_call_count: 5,
          fully_priced: true,
        },
      ],
    }

    const markup = renderToStaticMarkup(
      <DailyTokenAnalytics
        report={report}
        rangeDays={30}
        loading={false}
        error={null}
        onRangeChange={vi.fn()}
      />,
    )

    expect(markup).toContain('Consumo global de tokens')
    expect(markup).toContain('<dd>1200</dd>')
    expect(markup).toContain('0,0012 USD')
    expect(markup).toContain('40% de la entrada')
    expect(markup).toContain('Entrada total')
    expect(markup).toContain('Entrada en caché')
    expect(markup).toContain('Gráfica del histórico')
    expect(markup).toContain('<option value="tokens" selected="">Tokens</option>')
    expect(markup).toContain('<option value="cost">Coste</option>')
    expect(markup).not.toContain('Coste diario')
    expect(markup).toContain('30-07-2026')
  })

  it('does not offer a monetary chart when any consumed day is unpriced', () => {
    const report: DailyTokenUsageReport = {
      user_id: 'user-1',
      timezone: 'UTC',
      days: [
        {
          date: '30-07-2026',
          usage: {
            input_tokens: 10,
            output_tokens: 0,
            total_tokens: 10,
            cached_input_tokens: 0,
            uncached_input_tokens: 10,
            cached_input_audio_tokens: 0,
            reasoning_tokens: 0,
            input_audio_tokens: 0,
            output_audio_tokens: 0,
          },
          cost: null,
          turn_count: 1,
          model_call_count: 1,
          fully_priced: false,
        },
      ],
    }

    const markup = renderToStaticMarkup(
      <DailyTokenAnalytics
        report={report}
        rangeDays={7}
        loading={false}
        error={null}
        onRangeChange={vi.fn()}
      />,
    )

    expect(markup).toContain('<option value="cost" disabled="">Coste</option>')
    expect(markup).toContain('Faltan tarifas para algunas llamadas')
  })
})
