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
          turn_count: 3,
          model_call_count: 5,
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
    expect(markup).toContain('40% de la entrada')
    expect(markup).toContain('Entrada total')
    expect(markup).toContain('Entrada en caché')
    expect(markup).toContain('30-07-2026')
  })
})
