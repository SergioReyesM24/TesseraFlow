import type {
  ChartPoint,
  ChartSeries,
  ChartVisualComponent,
  MetricGroupVisualComponent,
  MetricVisual,
  VisualPresentation,
} from '../types'

type UnknownObject = Record<string, unknown>

/** Preserve the text fallback even when a visual kind or version is unsupported. */
export function parseVisualPresentation(data: UnknownObject): VisualPresentation | null {
  const componentId = text(data.component_id)
  const fallbackText = text(data.fallback_text)
  if (!componentId || !fallbackText) return null
  if (data.schema !== 'tesseraflow.visual' || data.version !== 1) {
    return { componentId, fallbackText, component: null }
  }
  const rawComponent = object(data.component)
  if (!rawComponent) return { componentId, fallbackText, component: null }
  const component =
    rawComponent.kind === 'chart'
      ? parseChart(rawComponent)
      : rawComponent.kind === 'metric_group'
        ? parseMetricGroup(rawComponent)
        : null
  return { componentId, fallbackText, component }
}

/** Upsert one component so a terminal replay cannot duplicate a streamed visual. */
export function mergeVisual(
  current: VisualPresentation[] | undefined,
  incoming: VisualPresentation,
): VisualPresentation[] {
  const visuals = current ?? []
  return visuals.some((visual) => visual.componentId === incoming.componentId)
    ? visuals.map((visual) =>
        visual.componentId === incoming.componentId ? incoming : visual,
      )
    : [...visuals, incoming]
}

/** Validate the closed chart payload before it reaches SVG calculations. */
function parseChart(raw: UnknownObject): ChartVisualComponent | null {
  const title = text(raw.title)
  const chartType = raw.chart_type === 'line' || raw.chart_type === 'bar' ? raw.chart_type : null
  const xAxis = object(raw.x_axis)
  const yAxis = object(raw.y_axis)
  if (!title || !chartType || !xAxis || !yAxis || !Array.isArray(raw.series)) return null
  const series = raw.series.map(parseSeries)
  if (
    series.length < 1 ||
    series.length > 6 ||
    series.some((item) => item === null) ||
    series.reduce((total, item) => total + (item?.points.length ?? 0), 0) > 200
  ) {
    return null
  }
  return {
    kind: 'chart',
    title,
    subtitle: nullableText(raw.subtitle),
    chart_type: chartType,
    x_axis: { label: nullableText(xAxis.label) },
    y_axis: { label: nullableText(yAxis.label), unit: nullableText(yAxis.unit) },
    series: series as ChartSeries[],
  }
}

/** Validate one named, non-empty chart series. */
function parseSeries(value: unknown): ChartSeries | null {
  const raw = object(value)
  if (!raw || !text(raw.name) || !Array.isArray(raw.points) || raw.points.length < 1) return null
  const points = raw.points.map(parsePoint)
  if (points.some((point) => point === null)) return null
  return { name: text(raw.name) as string, points: points as ChartPoint[] }
}

/** Reject non-finite values before deriving chart ranges. */
function parsePoint(value: unknown): ChartPoint | null {
  const raw = object(value)
  const x = raw ? text(raw.x) : null
  const y = raw?.y
  return x && typeof y === 'number' && Number.isFinite(y) ? { x, y } : null
}

/** Validate the small metric catalog without accepting arbitrary component props. */
function parseMetricGroup(raw: UnknownObject): MetricGroupVisualComponent | null {
  const title = text(raw.title)
  if (!title || !Array.isArray(raw.metrics) || raw.metrics.length < 1 || raw.metrics.length > 6) {
    return null
  }
  const metrics = raw.metrics.map(parseMetric)
  if (metrics.some((metric) => metric === null)) return null
  return {
    kind: 'metric_group',
    title,
    subtitle: nullableText(raw.subtitle),
    metrics: metrics as MetricVisual[],
  }
}

/** Read one metric whose value is already formatted by the semantic producer. */
function parseMetric(value: unknown): MetricVisual | null {
  const raw = object(value)
  const label = raw ? text(raw.label) : null
  const metricValue = raw ? text(raw.value) : null
  if (!label || !metricValue) return null
  return {
    label,
    value: metricValue,
    unit: nullableText(raw?.unit),
    detail: nullableText(raw?.detail),
  }
}

/** Narrow an unknown JSON value to a plain object. */
function object(value: unknown): UnknownObject | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as UnknownObject)
    : null
}

/** Accept one non-empty string. */
function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null
}

/** Accept the nullable text fields produced by the v1 schema. */
function nullableText(value: unknown): string | null {
  return value === null || value === undefined ? null : text(value)
}
