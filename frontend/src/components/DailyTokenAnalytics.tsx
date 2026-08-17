import { ArrowDownToLine, ArrowUpFromLine, Database, Gauge, RefreshCw } from 'lucide-react'
import { useMemo, useState } from 'react'
import { formatDate } from '../lib/dates'
import { formatCurrencyUnit, formatModelCost } from '../lib/costs'
import type {
  DailyTokenUsageReport,
  ModelCost,
  ModelUsage,
  VisualPresentation as VisualPresentationData,
} from '../types'
import { VisualPresentation } from './VisualPresentation'

interface DailyTokenAnalyticsProps {
  report: DailyTokenUsageReport | null
  rangeDays: number
  loading: boolean
  error: string | null
  onRangeChange: (days: number) => void
}

const numberFormatter = new Intl.NumberFormat('es-ES')

/** Present owner-global daily consumption independently from the selected session. */
export function DailyTokenAnalytics({
  report,
  rangeDays,
  loading,
  error,
  onRangeChange,
}: DailyTokenAnalyticsProps) {
  const [chartMetric, setChartMetric] = useState<'tokens' | 'cost'>('tokens')
  const totals = useMemo(() => aggregateUsage(report), [report])
  const chart = useMemo(() => buildDailyChart(report), [report])
  const costChart = useMemo(() => buildDailyCostChart(report), [report])
  const activeChartMetric = chartMetric === 'cost' && costChart ? 'cost' : 'tokens'
  const activeChart = activeChartMetric === 'cost' ? costChart : chart

  return (
    <section className="history-global-usage" aria-labelledby="global-token-title">
      <header className="history-global-usage-header">
        <div>
          <span className="eyebrow">Todas las conversaciones · UTC</span>
          <h2 id="global-token-title">Consumo global de tokens</h2>
          <p>Incluye conversaciones principales y ejecuciones de agentes internos.</p>
        </div>
        <div className="history-global-usage-controls">
          <label>
            Gráfica
            <select
              aria-label="Gráfica del histórico"
              value={activeChartMetric}
              onChange={(event) => setChartMetric(event.target.value as 'tokens' | 'cost')}
            >
              <option value="tokens">Tokens</option>
              <option value="cost" disabled={!costChart}>Coste</option>
            </select>
          </label>
          <label>
            Periodo
            <span>
              {loading && <RefreshCw className="spin" size={13} aria-label="Actualizando" />}
              <select
                value={rangeDays}
                onChange={(event) => onRangeChange(Number(event.target.value))}
                disabled={loading}
              >
                <option value={7}>7 días</option>
                <option value={30}>30 días</option>
                <option value={90}>90 días</option>
                <option value={365}>365 días</option>
              </select>
            </span>
          </label>
        </div>
      </header>

      {error ? (
        <div className="history-global-usage-error" role="alert">{error}</div>
      ) : report ? (
        <>
          <dl className="history-global-token-totals">
            <TokenTotal icon={<Gauge size={15} />} label="Total" value={totals.usage.total_tokens} />
            <TokenTotal
              icon={<ArrowDownToLine size={15} />}
              label="Entrada"
              value={totals.usage.input_tokens}
            />
            <TokenTotal
              icon={<ArrowUpFromLine size={15} />}
              label="Salida"
              value={totals.usage.output_tokens}
            />
            <TokenTotal
              icon={<Database size={15} />}
              label="En caché"
              value={totals.usage.cached_input_tokens}
              detail={`${totals.cacheRatio}% de la entrada`}
            />
            <TokenTotal
              icon={<Gauge size={15} />}
              label="Llamadas"
              value={totals.modelCalls}
              detail={`${numberFormatter.format(totals.turns)} turnos`}
            />
            <CostTotal
              icon={<Gauge size={15} />}
              label="Coste"
              cost={totals.cost}
              modelCalls={totals.modelCalls}
              fullyPriced={totals.fullyPriced}
            />
          </dl>
          {activeChart && <VisualPresentation presentation={activeChart} />}
        </>
      ) : (
        <div className="history-global-usage-loading">
          <RefreshCw className="spin" size={16} />
          Cargando consumo global…
        </div>
      )}
    </section>
  )
}

function TokenTotal({
  icon,
  label,
  value,
  detail,
}: {
  icon: React.ReactNode
  label: string
  value: number
  detail?: string
}) {
  return (
    <div>
      <dt>{icon}{label}</dt>
      <dd>{numberFormatter.format(value)}</dd>
      {detail && <small>{detail}</small>}
    </div>
  )
}

function CostTotal({
  icon,
  label,
  cost,
  modelCalls,
  fullyPriced,
}: {
  icon: React.ReactNode
  label: string
  cost: ModelCost | null
  modelCalls: number
  fullyPriced: boolean
}) {
  const value = modelCalls === 0 ? 'Sin consumo' : cost ? formatModelCost(cost) : 'Parcial'
  const detail =
    modelCalls === 0
      ? 'Sin llamadas en el periodo'
      : fullyPriced
        ? 'Todas las llamadas con tarifa'
        : 'Faltan tarifas para algunas llamadas'
  return (
    <div>
      <dt>{icon}{label}</dt>
      <dd>{value}</dd>
      <small>{detail}</small>
    </div>
  )
}

function aggregateUsage(report: DailyTokenUsageReport | null) {
  const usage = report?.days.reduce<ModelUsage>(
    (total, day) => ({
      input_tokens: total.input_tokens + day.usage.input_tokens,
      output_tokens: total.output_tokens + day.usage.output_tokens,
      total_tokens: total.total_tokens + day.usage.total_tokens,
      cached_input_tokens: total.cached_input_tokens + day.usage.cached_input_tokens,
      uncached_input_tokens: total.uncached_input_tokens + day.usage.uncached_input_tokens,
      cached_input_audio_tokens:
        total.cached_input_audio_tokens + day.usage.cached_input_audio_tokens,
      reasoning_tokens: total.reasoning_tokens + day.usage.reasoning_tokens,
      input_audio_tokens: total.input_audio_tokens + day.usage.input_audio_tokens,
      output_audio_tokens: total.output_audio_tokens + day.usage.output_audio_tokens,
    }),
    emptyUsage(),
  ) ?? emptyUsage()
  const turns = report?.days.reduce((total, day) => total + day.turn_count, 0) ?? 0
  const modelCalls = report?.days.reduce((total, day) => total + day.model_call_count, 0) ?? 0
  const pricedDays = report?.days.filter((day) => day.cost !== null) ?? []
  const currencies = new Set(pricedDays.map((day) => day.cost?.currency))
  const fullyPriced = Boolean(report?.days.every((day) => day.fully_priced))
  const cost =
    fullyPriced && pricedDays.length > 0 && currencies.size === 1
      ? {
          amount: pricedDays.reduce((total, day) => total + (day.cost?.amount ?? 0), 0),
          currency: pricedDays[0].cost?.currency ?? '',
        }
      : null
  return {
    usage,
    turns,
    modelCalls,
    cost,
    fullyPriced,
    cacheRatio: usage.input_tokens
      ? Math.round((usage.cached_input_tokens / usage.input_tokens) * 100)
      : 0,
  }
}

function buildDailyChart(report: DailyTokenUsageReport | null): VisualPresentationData | null {
  if (!report?.days.length) return null
  return {
    componentId: `global-token-usage-${report.days[0].date}-${report.days.at(-1)?.date}`,
    fallbackText: report.days
      .map(
        (day) =>
          `${formatDate(day.date)}: ${day.usage.input_tokens} entrada, ${day.usage.output_tokens} salida y ${day.usage.cached_input_tokens} en caché`,
      )
      .join('. '),
    component: {
      kind: 'chart',
      title: 'Consumo diario',
      subtitle: 'Entrada, salida y entrada reutilizada por día',
      chart_type: 'line',
      x_axis: { label: 'Día (UTC)' },
      y_axis: { label: 'Tokens', unit: null },
      series: [
        {
          name: 'Entrada total',
          points: report.days.map((day) => ({ x: formatDate(day.date), y: day.usage.input_tokens })),
        },
        {
          name: 'Salida',
          points: report.days.map((day) => ({ x: formatDate(day.date), y: day.usage.output_tokens })),
        },
        {
          name: 'Entrada en caché',
          points: report.days.map((day) => ({
            x: formatDate(day.date),
            y: day.usage.cached_input_tokens,
          })),
        },
      ],
    },
  }
}

function buildDailyCostChart(report: DailyTokenUsageReport | null): VisualPresentationData | null {
  if (!report?.days.length) return null
  const hasCompletePricing = report.days.every(
    (day) => day.model_call_count === 0 || (day.fully_priced && day.cost !== null),
  )
  if (!hasCompletePricing) return null
  const costDays = report.days.filter((day) => day.cost !== null)
  const currencies = new Set(costDays.map((day) => day.cost?.currency))
  if (!costDays.length || currencies.size !== 1) return null
  const currency = formatCurrencyUnit(costDays[0].cost?.currency)
  const points = report.days.map((day) => ({
    x: formatDate(day.date),
    y: day.cost?.amount ?? 0,
  }))
  return {
    componentId: `global-token-cost-${report.days[0].date}-${report.days.at(-1)?.date}`,
    fallbackText: points.map((point) => `${point.x}: ${point.y} ${currency ?? ''}`).join('. '),
    component: {
      kind: 'chart',
      title: 'Coste diario',
      subtitle: 'Coste calculado de llamadas por día',
      chart_type: 'bar',
      x_axis: { label: 'Día (UTC)' },
      y_axis: { label: 'Coste', unit: currency },
      series: [{ name: 'Coste', points }],
    },
  }
}

function emptyUsage(): ModelUsage {
  return {
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    cached_input_tokens: 0,
    uncached_input_tokens: 0,
    cached_input_audio_tokens: 0,
    reasoning_tokens: 0,
    input_audio_tokens: 0,
    output_audio_tokens: 0,
  }
}
