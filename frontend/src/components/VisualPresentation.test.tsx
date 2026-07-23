import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { ChartVisualComponent, VisualPresentation as VisualData } from '../types'
import { VisualPresentation } from './VisualPresentation'

/** Build the smallest valid chart needed to inspect its interactive SVG markup. */
function chartPresentation(chartType: 'line' | 'bar'): VisualData {
  const component: ChartVisualComponent = {
    kind: 'chart',
    title: 'Saldo',
    subtitle: null,
    chart_type: chartType,
    x_axis: { label: 'Semana' },
    y_axis: { label: 'Saldo', unit: 'EUR' },
    series: [{ name: 'Saldo al cierre', points: [{ x: '2026-07-19', y: 13275.65 }] }],
  }
  return {
    componentId: `balance-${chartType}`,
    fallbackText: 'El saldo al cierre fue 13.275,65 EUR.',
    component,
  }
}

describe('VisualPresentation chart data tooltips', () => {
  it.each(['line', 'bar'] as const)(
    'makes every %s datum clickable without series-specific copy',
    (kind) => {
    const markup = renderToStaticMarkup(
      <VisualPresentation presentation={chartPresentation(kind)} />,
    )
    const value = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(
      13275.65,
    )
    const description = `2026-07-19, ${value} EUR`

    expect(markup).toContain('class="chart-data-mark ')
    expect(markup).toContain('tabindex="0"')
    expect(markup).toContain(`aria-label="${description}"`)
    expect(markup).not.toContain('<title>Saldo al cierre')
    if (kind === 'line') {
      expect(markup).toContain('class="chart-line-series"')
      expect(markup).toContain('pathLength="1"')
      expect(markup).toContain('class="chart-data-mark chart-line-point"')
    } else {
      expect(markup).toContain('class="chart-data-mark chart-bar"')
      expect(markup).toContain('transform-origin:')
    }
    },
  )
})
