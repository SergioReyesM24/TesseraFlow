import { useState } from 'react'
import type {
  ChartVisualComponent,
  MetricGroupVisualComponent,
  VisualPresentation as VisualPresentationData,
} from '../types'

const CHART_COLORS = ['#377b63', '#b26a3d', '#5579a6', '#9a5f8c', '#7d843a', '#845d47']
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
      ) : (
        <MetricGroup component={component} />
      )}
    </section>
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
              {metric.unit && <small>{metric.unit}</small>}
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
  const labels = Array.from(
    new Set(component.series.flatMap((series) => series.points.map((point) => point.x))),
  )
  const values = component.series.flatMap((series) => series.points.map((point) => point.y))
  const rawMin = Math.min(...values)
  const rawMax = Math.max(...values)
  const rangePadding = rawMin === rawMax ? Math.max(Math.abs(rawMin) * 0.1, 1) : 0
  const minimum = rawMin - rangePadding
  const maximum = rawMax + rangePadding
  const valueRange = maximum - minimum || 1
  const plotWidth = WIDTH - PADDING.left - PADDING.right
  const plotHeight = HEIGHT - PADDING.top - PADDING.bottom
  const x = (label: string) => {
    const index = Math.max(0, labels.indexOf(label))
    return PADDING.left + (labels.length === 1 ? plotWidth / 2 : (index / (labels.length - 1)) * plotWidth)
  }
  const y = (value: number) => PADDING.top + ((maximum - value) / valueRange) * plotHeight
  const format = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 })
  const gridValues = [maximum, minimum + valueRange / 2, minimum]
  const activeTooltip = hoveredTooltip ?? pinnedTooltip

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
              ? component.series.map((series, seriesIndex) => {
                  const color = CHART_COLORS[seriesIndex % CHART_COLORS.length]
                  const points = series.points.map((point) => `${x(point.x)},${y(point.y)}`).join(' ')
                  return (
                    <g key={series.name}>
                      <polyline
                        className="chart-line-series"
                        points={points}
                        fill="none"
                        stroke={color}
                        strokeWidth="3"
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
                          component.y_axis.unit,
                          format,
                        )
                        return (
                          <circle
                            className="chart-data-mark chart-line-point"
                            key={tooltip.key}
                            cx={tooltip.x}
                            cy={tooltip.y}
                            r="5"
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
              : component.series.flatMap((series, seriesIndex) => {
                  const groupWidth = plotWidth / Math.max(labels.length, 1)
                  const barWidth = Math.min(34, (groupWidth * 0.72) / component.series.length)
                  return series.points.map((point, pointIndex) => {
                    const baselineValue =
                      minimum <= 0 && maximum >= 0 ? 0 : minimum > 0 ? minimum : maximum
                    const baseline = y(baselineValue)
                    const pointY = y(point.y)
                    const sequenceIndex = Math.max(0, labels.indexOf(point.x))
                    const renderedBarWidth = Math.max(barWidth - 2, 1)
                    const barX =
                      x(point.x) -
                      (barWidth * component.series.length) / 2 +
                      seriesIndex * barWidth
                    const tooltip = chartTooltip(
                      `bar-${seriesIndex}-${pointIndex}`,
                      barX + renderedBarWidth / 2,
                      Math.min(pointY, baseline),
                      point.x,
                      point.y,
                      component.y_axis.unit,
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
                  {shorten(label)}
                </text>
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
              <span>{activeTooltip.xValue}</span>
              <strong>{activeTooltip.yValue}</strong>
            </div>
          )}
        </div>
      </div>
      <div className="chart-footer">
        <span>{component.x_axis.label}</span>
        <span>{[component.y_axis.label, component.y_axis.unit].filter(Boolean).join(' · ')}</span>
      </div>
      {component.series.length > 1 && (
        <div className="chart-legend">
          {component.series.map((series, index) => (
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
