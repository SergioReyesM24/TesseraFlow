import { describe, expect, it } from 'vitest'
import { formatCurrencyUnit, formatModelCost, parseTurnMetrics } from './costs'

describe('turn cost metrics', () => {
  it('parses the provider-neutral completed-event payload', () => {
    const metrics = parseTurnMetrics({
      usage: {
        input_tokens: 100,
        output_tokens: 20,
        total_tokens: 120,
        cached_input_tokens: 10,
        uncached_input_tokens: 90,
        cached_input_audio_tokens: 0,
        reasoning_tokens: 5,
        input_audio_tokens: 0,
        output_audio_tokens: 0,
      },
      cost: { amount: 0.000065, currency: 'USD' },
      calls: [
        {
          model: 'model-a',
          usage: {
            input_tokens: 100,
            output_tokens: 20,
            total_tokens: 120,
            cached_input_tokens: 10,
            uncached_input_tokens: 90,
            cached_input_audio_tokens: 0,
            reasoning_tokens: 5,
            input_audio_tokens: 0,
            output_audio_tokens: 0,
          },
          cost: { amount: 0.000065, currency: 'USD' },
        },
      ],
    })

    expect(metrics?.usage.total_tokens).toBe(120)
    expect(metrics?.calls[0].model).toBe('model-a')
    expect(formatModelCost(metrics!.cost!)).toContain('0,000065 USD')
  })

  it('rejects incomplete transport data', () => {
    expect(parseTurnMetrics({ usage: {}, calls: [] })).toBeUndefined()
  })

  it('formats configured euro costs with the euro symbol', () => {
    expect(formatModelCost({ amount: 1.236, currency: 'EUR' })).toBe('1,24 €')
    expect(formatModelCost({ amount: 0.0012, currency: '€' })).toBe('0,0012 €')
    expect(formatModelCost({ amount: 0.0000001, currency: 'EUR' })).toBe('< 0,000001 €')
    expect(formatCurrencyUnit('eur')).toBe('€')
  })
})
