import {
  ArrowDownToLine,
  ArrowUpFromLine,
  CircleDollarSign,
  Database,
  Gauge,
} from 'lucide-react'
import { formatModelCost } from '../lib/costs'
import type { ModelCost, ModelUsage } from '../types'

interface TokenUsageProps {
  usage: ModelUsage
  cost?: ModelCost | null
  calls?: number
  compact?: boolean
}

const numberFormatter = new Intl.NumberFormat('es-ES')

/** Show the billable token categories without hiding their relationship to the total. */
export function TokenUsage({ usage, cost, calls, compact = false }: TokenUsageProps) {
  const cacheRatio = usage.input_tokens
    ? Math.round((usage.cached_input_tokens / usage.input_tokens) * 100)
    : null

  return (
    <section
      className={`token-usage${compact ? ' token-usage-compact' : ''}`}
      aria-label="Desglose de uso de tokens"
    >
      <header className="token-usage-header">
        <span className="token-usage-title">
          <Gauge size={14} aria-hidden="true" />
          Uso de tokens
        </span>
        <span className="token-usage-total">
          <strong>{numberFormatter.format(usage.total_tokens)}</strong>
          <small>total</small>
        </span>
      </header>

      <dl className="token-usage-grid">
        <div className="token-stat token-stat-input">
          <dt>
            <ArrowDownToLine size={13} aria-hidden="true" />
            Entrada
          </dt>
          <dd>{numberFormatter.format(usage.input_tokens)}</dd>
        </div>
        <div className="token-stat token-stat-output">
          <dt>
            <ArrowUpFromLine size={13} aria-hidden="true" />
            Salida
          </dt>
          <dd>{numberFormatter.format(usage.output_tokens)}</dd>
        </div>
        <div className="token-stat token-stat-cached">
          <dt>
            <Database size={13} aria-hidden="true" />
            En caché
          </dt>
          <dd>{numberFormatter.format(usage.cached_input_tokens)}</dd>
        </div>
      </dl>

      <footer className="token-usage-footer">
        <span>
          {cacheRatio === null
            ? 'Caché sobre tokens de entrada'
            : `${cacheRatio}% de la entrada reutilizada`}
        </span>
        <span className="token-usage-cost">
          <CircleDollarSign size={13} aria-hidden="true" />
          {cost ? formatModelCost(cost) : 'Sin tarifa configurada'}
          {calls !== undefined && (
            <small>
              · {calls} {calls === 1 ? 'llamada' : 'llamadas'}
            </small>
          )}
        </span>
      </footer>
    </section>
  )
}
