import type { ModelCost, ModelUsage, TurnMetrics } from '../types'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function parseUsage(value: unknown): ModelUsage | null {
  if (!isRecord(value)) return null
  const keys = [
    'input_tokens',
    'output_tokens',
    'total_tokens',
    'cached_input_tokens',
    'uncached_input_tokens',
    'cached_input_audio_tokens',
    'reasoning_tokens',
    'input_audio_tokens',
    'output_audio_tokens',
  ] as const
  if (keys.some((key) => typeof value[key] !== 'number')) return null
  return Object.fromEntries(keys.map((key) => [key, value[key]])) as unknown as ModelUsage
}

function parseCost(value: unknown): ModelCost | null {
  if (!isRecord(value) || typeof value.amount !== 'number' || typeof value.currency !== 'string') {
    return null
  }
  return { amount: value.amount, currency: value.currency }
}

/** Validate accounting data at the transport boundary before rendering it. */
export function parseTurnMetrics(value: unknown): TurnMetrics | undefined {
  if (!isRecord(value) || !Array.isArray(value.calls)) return undefined
  const usage = parseUsage(value.usage)
  if (!usage) return undefined
  const calls = value.calls.flatMap((raw) => {
    if (!isRecord(raw) || typeof raw.model !== 'string') return []
    const callUsage = parseUsage(raw.usage)
    if (!callUsage) return []
    return [{ model: raw.model, usage: callUsage, cost: parseCost(raw.cost) }]
  })
  if (calls.length !== value.calls.length) return undefined
  return { usage, cost: parseCost(value.cost), calls }
}

/** Format tiny per-turn amounts without rounding them down to a misleading zero. */
export function formatModelCost(cost: ModelCost): string {
  if (cost.amount > 0 && cost.amount < 0.000001) return `< 0,000001 ${cost.currency}`
  return `${new Intl.NumberFormat('es-ES', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 6,
  }).format(cost.amount)} ${cost.currency}`
}
