import { useState } from 'react'
import { formatCurrencyUnit } from '../lib/costs'
import { formatDate } from '../lib/dates'
import type {
  ChartVisualComponent,
  MetricGroupVisualComponent,
  TransactionListVisualComponent,
  VisualPresentation as VisualPresentationData,
} from '../types'

const CHART_COLORS = [
  'var(--chart-1)',
  'var(--chart-2)',
  'var(--chart-3)',
  'var(--chart-4)',
  'var(--chart-5)',
  'var(--chart-6)',
]
const WIDTH = 640
const HEIGHT = 260
const PADDING = { top: 18, right: 18, bottom: 42, left: 58 }

interface VisualPresentationProps {
  presentation: VisualPresentationData
}

interface ChartTooltipData {
  key: string
  x: number
  y: number
  xValue: string
  yValue: string
  values?: string[]
}

/** Render one supported semantic component or its mandatory text fallback. */
export function VisualPresentation({ presentation }: VisualPresentationProps) {
  const component = presentation.component
  if (!component) {
    return <aside className="visual-fallback">{presentation.fallbackText}</aside>
  }
  return (
    <section className="visual-card" aria-label={component.title}>
      {component.kind === 'chart' ? (
        <Chart component={component} fallbackText={presentation.fallbackText} />
      ) : component.kind === 'transaction_list' ? (
        <TransactionList component={component} />
      ) : (
        <MetricGroup component={component} />
      )}
    </section>
  )
}

/** Show savings before and after the period plus the exact income and expense rows. */
function TransactionList({ component }: { component: TransactionListVisualComponent }) {
  return (
    <>
      <VisualHeader title={component.title} subtitle={component.subtitle} />
      <div className="transaction-summary" aria-label="Resumen del ahorro">
        <FinancialMetric
          label="Ahorro base"
          value={formatMoney(component.base_savings, component.currency)}
        />
        <FinancialMetric
          label="Ahorro actual"
          value={formatMoney(component.current_savings, component.currency)}
          emphasis
        />
        <FinancialMetric
          label="Ingresos"
          value={`+${formatMoney(component.total_income, component.currency)}`}
          tone="income"
        />
        <FinancialMetric
          label="Gastos"
          value={`−${formatMoney(component.total_expenses, component.currency)}`}
          tone="expense"
        />
      </div>
      <div className="transaction-table" role="table" aria-label="Últimos movimientos">
        <div className="transaction-table-header" role="row">
          <span role="columnheader">Movimiento</span>
          <span role="columnheader">Comercio</span>
          <span role="columnheader">Cantidad</span>
        </div>
        {component.transactions.map((transaction, index) => {
          const isIncome = transaction.transaction_type === 'income'
          return (
            <div
              className={`transaction-row transaction-row-${isIncome ? 'income' : 'expense'}`}
              role="row"
              key={`${transaction.booked_at}-${transaction.merchant}-${index}`}
              style={{ animationDelay: `${100 + index * 45}ms` }}
            >
              <div className="transaction-kind" role="cell">
                <span
                  className={`transaction-icon ${isIncome ? 'income' : 'expense'}`}
                  aria-hidden="true"
                >
                  {isIncome ? '+' : '−'}
                </span>
              </div>
              <div className="transaction-merchant" role="cell">
                <strong>{transaction.merchant}</strong>
                <span>
                  <time>{formatDate(transaction.booked_at.slice(0, 10))}</time>
                  <i aria-hidden="true">·</i>
                  {transaction.category}
                  <i aria-hidden="true">·</i>
                  <b className={isIncome ? 'income' : 'expense'}>
                    {isIncome ? 'Ingreso' : 'Gasto'}
                  </b>
                </span>
              </div>
              <div className="transaction-amount" role="cell">
                <strong className={isIncome ? 'income' : 'expense'}>
                  {isIncome ? '+' : '−'}
                  {formatMoney(transaction.amount, component.currency)}
                </strong>
                <span>
                  Saldo: {formatMoney(transaction.balance_after, component.currency)}
                </span>
              </div>
            </div>
          )
        })}
      </div>
    </>
  )
}

/** Render one compact monetary summary value with a semantic tone. */
function FinancialMetric({
  label,
  value,
  emphasis = false,
  tone,
}: {
  label: string
  value: string
  emphasis?: boolean
  tone?: 'income' | 'expense'
}) {
  return (
    <div
      className={`financial-metric${emphasis ? ' emphasized' : ''}${
        tone ? ` financial-metric-${tone}` : ''
      }`}
    >
      <span>{label}</span>
      <strong className={tone}>{value}</strong>
    </div>
  )
}

/** Present headline values without accepting arbitrary layout instructions. */
function MetricGroup({ component }: { component: MetricGroupVisualComponent }) {
  return (
    <>
      <VisualHeader title={component.title} subtitle={component.subtitle} />
      <div className="metric-grid">
        {component.metrics.map((metric, index) => (
          <div
            className="metric-item"
            key={metric.label}
            style={{ animationDelay: `${120 + index * 75}ms` }}
          >
            <span>{metric.label}</span>
            <strong>
              {metric.value}
              {metric.unit && <small>{formatCurrencyUnit(metric.unit)}</small>}
            </strong>
            {metric.detail && <p>{metric.detail}</p>}
          </div>
        ))}
      </div>
    </>
  )
}

/** Draw the two allowed chart variants using a dependency-free responsive SVG. */
function Chart({
  component,
  fallbackText,
}: {
  component: ChartVisualComponent
  fallbackText: string
}) {
  const [hoveredTooltip, setHoveredTooltip] = useState<ChartTooltipData | null>(null)
  const [pinnedTooltip, setPinnedTooltip] = useState<ChartTooltipData | null>(null)
  const rawLabels = Array.from(
    new Set(component.series.flatMap((series) => series.points.map((point) => point.x))),
  )
  const labels = resolveXLabels(rawLabels, component.x_axis.min, component.x_axis.max)
  const labelSet = new Set(labels)
  const renderedSeries = component.series.map((series) => ({
    ...series,
    points: series.points.filter((point) => labelSet.has(point.x)),
  }))
  const renderedComponent = { ...component, series: renderedSeries }
  const values = renderedSeries.flatMap((series) => series.points.map((point) => point.y))
  const [minimum, maximum] = resolveYDomain(values, component.y_axis.min, component.y_axis.max)
  const valueRange = maximum - minimum || 1
  const plotWidth = WIDTH - PADDING.left - PADDING.right
  const plotHeight = HEIGHT - PADDING.top - PADDING.bottom
  const x = (label: string) => {
    const index = Math.max(0, labels.indexOf(label))
    if (component.chart_type === 'bar') {
      const bandWidth = plotWidth / Math.max(labels.length, 1)
      return PADDING.left + bandWidth * (index + 0.5)
    }
    return PADDING.left + (labels.length === 1 ? plotWidth / 2 : (index / (labels.length - 1)) * plotWidth)
  }
  const y = (value: number) => PADDING.top + ((maximum - value) / valueRange) * plotHeight
  const chartUnit = formatCurrencyUnit(component.y_axis.unit)
  const format = new Intl.NumberFormat('es-ES', {
    minimumFractionDigits: chartUnit === '€' ? 2 : 0,
    maximumFractionDigits: 2,
  })
  const gridValues = [maximum, minimum + valueRange / 2, minimum]
  const activeTooltip = hoveredTooltip ?? pinnedTooltip
  const hoverTooltips = labels.map((label) =>
    chartColumnTooltip(
      label,
      renderedComponent,
      x(label),
      PADDING.top + 12,
      chartUnit,
      format,
    ),
  )

  /** Pin one datum on click, or close it when the same mark is selected again. */
  const toggleTooltip = (tooltip: ChartTooltipData) => {
    setPinnedTooltip((current) => (current?.key === tooltip.key ? null : tooltip))
  }

  return (
    <>
      <VisualHeader title={component.title} subtitle={component.subtitle} />
      <div className="chart-wrap">
        <div className="chart-stage">
          <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={fallbackText}>
            {gridValues.map((value) => (
              <g key={value}>
                <line
                  className="chart-grid-line"
                  x1={PADDING.left}
                  x2={WIDTH - PADDING.right}
                  y1={y(value)}
                  y2={y(value)}
                />
                <text className="chart-axis-value" x={PADDING.left - 9} y={y(value) + 4} textAnchor="end">
                  {format.format(value)}
                </text>
              </g>
            ))}
            {component.chart_type === 'line'
              ? renderedSeries.map((series, seriesIndex) => {
                  const color = CHART_COLORS[seriesIndex % CHART_COLORS.length]
                  const points = series.points.map((point) => `${x(point.x)},${y(point.y)}`).join(' ')
                  return (
                    <g key={series.name}>
                      <polyline
                        className="chart-line-series"
                        points={points}
                        fill="none"
                        stroke={color}
                        strokeWidth="1.5"
                        strokeLinejoin="round"
                        pathLength={1}
                        style={{ animationDelay: `${100 + seriesIndex * 100}ms` }}
                      />
                      {series.points.map((point, pointIndex) => {
                        const sequenceIndex = Math.max(0, labels.indexOf(point.x))
                        const tooltip = chartTooltip(
                          `line-${seriesIndex}-${pointIndex}`,
                          x(point.x),
                          y(point.y),
                          point.x,
                          point.y,
                          chartUnit,
                          format,
                        )
                        return (
                          <circle
                            className="chart-data-mark chart-line-point"
                            key={tooltip.key}
                            cx={tooltip.x}
                            cy={tooltip.y}
                            r="3"
                            fill={color}
                            style={{
                              animationDelay: `${240 + sequenceIndex * 75 + seriesIndex * 35}ms`,
                            }}
                            tabIndex={0}
                            aria-label={`${tooltip.xValue}, ${tooltip.yValue}`}
                            onMouseEnter={() => setHoveredTooltip(tooltip)}
                            onMouseLeave={() => setHoveredTooltip(null)}
                            onFocus={() => setHoveredTooltip(tooltip)}
                            onBlur={() => setHoveredTooltip(null)}
                            onClick={() => toggleTooltip(tooltip)}
                            onKeyDown={(event) => {
                              if (event.key === 'Enter' || event.key === ' ') {
                                event.preventDefault()
                                toggleTooltip(tooltip)
                              }
                            }}
                          />
                        )
                      })}
                    </g>
                  )
                })
              : renderedSeries.flatMap((series, seriesIndex) => {
                  const groupWidth = plotWidth / Math.max(labels.length, 1)
                  const barWidth = Math.min(34, (groupWidth * 0.72) / renderedSeries.length)
                  return series.points.map((point, pointIndex) => {
                    const baselineValue =
                      minimum <= 0 && maximum >= 0 ? 0 : minimum > 0 ? minimum : maximum
                    const baseline = y(baselineValue)
                    const pointY = y(point.y)
                    const sequenceIndex = Math.max(0, labels.indexOf(point.x))
                    const renderedBarWidth = Math.max(barWidth - 2, 1)
                    const barX =
                      x(point.x) -
                      (barWidth * renderedSeries.length) / 2 +
                      seriesIndex * barWidth
                    const tooltip = chartTooltip(
                      `bar-${seriesIndex}-${pointIndex}`,
                      barX + renderedBarWidth / 2,
                      Math.min(pointY, baseline),
                      point.x,
                      point.y,
                      chartUnit,
                      format,
                    )
                    return (
                      <rect
                        className="chart-data-mark chart-bar"
                        key={tooltip.key}
                        x={barX}
                        y={Math.min(pointY, baseline)}
                        width={renderedBarWidth}
                        height={Math.max(Math.abs(baseline - pointY), 1)}
                        rx="3"
                        fill={CHART_COLORS[seriesIndex % CHART_COLORS.length]}
                        style={{
                          animationDelay: `${120 + sequenceIndex * 90 + seriesIndex * 40}ms`,
                          transformOrigin: `${tooltip.x}px ${baseline}px`,
                        }}
                        tabIndex={0}
                        aria-label={`${tooltip.xValue}, ${tooltip.yValue}`}
                        onMouseEnter={() => setHoveredTooltip(tooltip)}
                        onMouseLeave={() => setHoveredTooltip(null)}
                        onFocus={() => setHoveredTooltip(tooltip)}
                        onBlur={() => setHoveredTooltip(null)}
                        onClick={() => toggleTooltip(tooltip)}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter' || event.key === ' ') {
                            event.preventDefault()
                            toggleTooltip(tooltip)
                          }
                        }}
                      />
                    )
                  })
                })}
            {labels.map((label, index) => {
              const interval = Math.max(1, Math.ceil(labels.length / 6))
              if (index % interval !== 0 && index !== labels.length - 1) return null
              return (
                <text className="chart-x-label" key={label} x={x(label)} y={HEIGHT - 18} textAnchor="middle">
                  {shorten(formatDate(label))}
                </text>
              )
            })}
            {activeTooltip && (
              <line
                className="chart-cursor-line"
                x1={activeTooltip.x}
                x2={activeTooltip.x}
                y1={PADDING.top}
                y2={HEIGHT - PADDING.bottom}
              />
            )}
            {hoverTooltips.map((tooltip, index) => {
              const bounds = labelHoverBounds(index, labels.length, plotWidth)
              return (
                <rect
                  className="chart-hover-zone"
                  key={tooltip.key}
                  x={PADDING.left + bounds.start}
                  y={PADDING.top}
                  width={bounds.width}
                  height={plotHeight}
                  tabIndex={0}
                  aria-label={`${tooltip.xValue}, ${tooltip.yValue}`}
                  onMouseEnter={() => setHoveredTooltip(tooltip)}
                  onMouseMove={() => setHoveredTooltip(tooltip)}
                  onMouseLeave={() => setHoveredTooltip(null)}
                  onFocus={() => setHoveredTooltip(tooltip)}
                  onBlur={() => setHoveredTooltip(null)}
                  onClick={() => toggleTooltip(tooltip)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault()
                      toggleTooltip(tooltip)
                    }
                  }}
                />
              )
            })}
          </svg>
          {activeTooltip && (
            <div
              className={`chart-tooltip ${activeTooltip.y < 70 ? 'chart-tooltip-below' : ''}`}
              style={{
                left: `${(Math.min(Math.max(activeTooltip.x, 105), WIDTH - 105) / WIDTH) * 100}%`,
                top: `${(activeTooltip.y / HEIGHT) * 100}%`,
              }}
              role="status"
            >
              <span>{formatDate(activeTooltip.xValue)}</span>
              <strong>{activeTooltip.yValue}</strong>
              {activeTooltip.values?.map((value, index) => (
                <small key={`${index}-${value}`}>{value}</small>
              ))}
            </div>
          )}
        </div>
      </div>
      <div className="chart-footer">
        <span>{component.x_axis.label}</span>
        <span>{[component.y_axis.label, chartUnit].filter(Boolean).join(' · ')}</span>
      </div>
      {renderedSeries.length > 1 && (
        <div className="chart-legend">
          {renderedSeries.map((series, index) => (
            <span key={series.name}>
              <i style={{ backgroundColor: CHART_COLORS[index % CHART_COLORS.length] }} />
              {series.name}
            </span>
          ))}
        </div>
      )}
    </>
  )
}

/** Keep shared title semantics identical across component kinds. */
function VisualHeader({ title, subtitle }: { title: string; subtitle: string | null }) {
  return (
    <header className="visual-header">
      <h3>{title}</h3>
      {subtitle && <p>{subtitle}</p>}
    </header>
  )
}

/** Keep dense axis labels legible without changing their underlying meaning. */
function shorten(value: string): string {
  return value.length > 14 ? `${value.slice(0, 12)}…` : value
}

/** Format exact source money consistently while keeping the supplied currency visible. */
function formatMoney(value: number, currency: string): string {
  const formatted = new Intl.NumberFormat('es-ES', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value)
  return `${formatted} ${formatCurrencyUnit(currency) ?? currency}`
}

/** Derive the visible X labels from optional semantic axis bounds. */
function resolveXLabels(
  labels: string[],
  minimum: string | null | undefined,
  maximum: string | null | undefined,
): string[] {
  if (!minimum && !maximum) return labels
  const firstIndex = minimum ? labels.indexOf(minimum) : 0
  const lastIndex = maximum ? labels.indexOf(maximum) : labels.length - 1
  const hasMatchingBounds =
    (!minimum || firstIndex >= 0) && (!maximum || lastIndex >= 0) && firstIndex <= lastIndex
  if (hasMatchingBounds) {
    return labels.slice(firstIndex, lastIndex + 1)
  }

  const minimumValue = comparableAxisValue(minimum)
  const maximumValue = comparableAxisValue(maximum)
  if (minimumValue === null && maximumValue === null) return labels
  const labelValues = labels.map((label) => ({ label, value: comparableAxisValue(label) }))
  if (labelValues.some((item) => item.value === null)) return labels
  const filtered = labelValues
    .filter((item) => {
      const value = item.value as number
      return (
        (minimumValue === null || value >= minimumValue) &&
        (maximumValue === null || value <= maximumValue)
      )
    })
    .map((item) => item.label)
  return filtered.length > 0 ? filtered : labels
}

/** Resolve the Y domain, preserving the previous zero-based default when bounds are absent. */
function resolveYDomain(
  values: number[],
  explicitMinimum: number | null | undefined,
  explicitMaximum: number | null | undefined,
): [number, number] {
  const rawMin = values.length > 0 ? Math.min(...values) : 0
  const rawMax = values.length > 0 ? Math.max(...values) : 0
  const hasNegativeValues = rawMin < 0
  const rangePadding = rawMin === rawMax ? (rawMax === 0 ? 1 : Math.abs(rawMax) * 0.1) : 0
  const fallbackMinimum = hasNegativeValues ? rawMin - rangePadding : 0
  const fallbackMaximum = rawMax === 0 ? 1 : rawMax <= 0 ? 0 : rawMax + rangePadding
  const minimum = finiteAxisBound(explicitMinimum) ?? fallbackMinimum
  let maximum = finiteAxisBound(explicitMaximum) ?? fallbackMaximum
  if (maximum <= minimum) {
    maximum = minimum + Math.max(Math.abs(minimum) * 0.1, 1)
  }
  return [minimum, maximum]
}

/** Compare labels as numbers first, then as dates, when the axis gives continuous bounds. */
function comparableAxisValue(value: string | null | undefined): number | null {
  if (!value) return null
  const asNumber = Number(value)
  if (Number.isFinite(asNumber)) return asNumber
  const asDate = Date.parse(value)
  return Number.isFinite(asDate) ? asDate : null
}

/** Accept only finite numeric axis bounds at render time. */
function finiteAxisBound(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** Build one exact, series-agnostic tooltip from the X and Y source values. */
function chartTooltip(
  key: string,
  x: number,
  y: number,
  xValue: string,
  value: number,
  unit: string | null,
  format: Intl.NumberFormat,
): ChartTooltipData {
  return {
    key,
    x,
    y,
    xValue,
    yValue: `${format.format(value)}${unit ? ` ${unit}` : ''}`,
  }
}

/** Build an X-column tooltip so hovering near a point is enough to inspect values. */
function chartColumnTooltip(
  label: string,
  component: ChartVisualComponent,
  x: number,
  y: number,
  unit: string | null,
  format: Intl.NumberFormat,
): ChartTooltipData {
  const values = component.series.flatMap((series) => {
    const point = series.points.find((candidate) => candidate.x === label)
    if (!point) return []
    return `${series.name}: ${format.format(point.y)}${unit ? ` ${unit}` : ''}`
  })
  return {
    key: `column-${label}`,
    x,
    y,
    xValue: label,
    yValue: values.length === 1 ? values[0] : `${values.length} series`,
    values: values.length > 1 ? values : undefined,
  }
}

/** Split the plot into stable hover bands, centered on each X label. */
function labelHoverBounds(index: number, count: number, plotWidth: number) {
  if (count <= 1) return { start: 0, width: plotWidth }
  const center = (index / (count - 1)) * plotWidth
  const previousCenter = index === 0 ? 0 : ((index - 1) / (count - 1)) * plotWidth
  const nextCenter = index === count - 1 ? plotWidth : ((index + 1) / (count - 1)) * plotWidth
  const start = index === 0 ? 0 : (previousCenter + center) / 2
  const end = index === count - 1 ? plotWidth : (center + nextCenter) / 2
  return { start, width: Math.max(end - start, 1) }
}
