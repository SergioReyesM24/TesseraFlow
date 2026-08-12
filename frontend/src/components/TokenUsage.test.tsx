import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { TokenUsage } from './TokenUsage'

describe('TokenUsage', () => {
  it('renders input, output, cached and total counters with turn accounting', () => {
    const markup = renderToStaticMarkup(
      <TokenUsage
        usage={{
          input_tokens: 1_000,
          output_tokens: 200,
          total_tokens: 1_200,
          cached_input_tokens: 400,
          uncached_input_tokens: 600,
          cached_input_audio_tokens: 0,
          reasoning_tokens: 0,
          input_audio_tokens: 0,
          output_audio_tokens: 0,
        }}
        cost={{ amount: 0.0012, currency: '€' }}
        calls={2}
      />,
    )

    expect(markup).toContain('Desglose de uso de tokens')
    expect(markup).toContain('Entrada')
    expect(markup).toContain('Salida')
    expect(markup).toContain('En caché')
    expect(markup).toContain('40% de la entrada reutilizada')
    expect(markup).toContain('2 llamadas')
    expect(markup).toContain('0,0012 €')
  })
})
