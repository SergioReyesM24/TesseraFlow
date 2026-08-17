import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { VisualPresentation as VisualPresentationData } from '../types'
import { VisualInspector } from './VisualInspector'

const visuals: VisualPresentationData[] = [
  {
    componentId: 'balance-chart',
    fallbackText: 'Saldo semanal.',
    component: {
      kind: 'chart',
      title: 'Historial semanal',
      subtitle: null,
      chart_type: 'line',
      x_axis: { label: 'Semana' },
      y_axis: { label: 'Saldo', unit: 'EUR' },
      series: [{ name: 'Saldo', points: [{ x: '01-07-2026', y: 100 }] }],
    },
  },
  {
    componentId: 'summary-metrics',
    fallbackText: 'Resumen.',
    component: {
      kind: 'metric_group',
      title: 'Resumen de saldos',
      subtitle: null,
      metrics: [{ label: 'Actual', value: '120', unit: 'EUR', detail: null }],
    },
  },
]

describe('VisualInspector tabs', () => {
  it('keeps multiple visuals available as tabs', () => {
    const markup = renderToStaticMarkup(
      <VisualInspector presentations={visuals} onClose={() => undefined} />,
    )

    expect(markup).toContain('role="tablist"')
    expect(markup).toContain('Historial semanal')
    expect(markup).toContain('Resumen de saldos')
    expect(markup).toContain('He preparado 2 vistas para ti')
    expect(markup).toContain('role="tabpanel"')
    expect(markup).toContain('tabindex="0"')
    expect(markup).toContain('tabindex="-1"')
    expect(markup).toContain('aria-labelledby=')
  })
})
