import { describe, expect, it } from 'vitest'
import { mergeVisual, parseVisualPresentation } from './visuals'

describe('visual presentation protocol', () => {
  it('parses the closed chart schema', () => {
    const visual = parseVisualPresentation({
      schema: 'tesseraflow.visual',
      version: 1,
      component_id: 'weekly-balance',
      fallback_text: 'El saldo termina en 120 EUR.',
      component: {
        kind: 'chart',
        title: 'Saldo semanal',
        subtitle: null,
        chart_type: 'line',
        x_axis: { label: 'Semana' },
        y_axis: { label: 'Saldo', unit: 'EUR' },
        series: [
          {
            name: 'Saldo',
            points: [
              { x: '2026-07-01', y: 100 },
              { x: '2026-07-08', y: 120 },
            ],
          },
        ],
      },
    })

    expect(visual?.component?.kind).toBe('chart')
    if (visual?.component?.kind === 'chart') {
      expect(visual.component.series[0].points[1].y).toBe(120)
    }
  })

  it('keeps fallback text for unsupported versions', () => {
    const visual = parseVisualPresentation({
      schema: 'tesseraflow.visual',
      version: 99,
      component_id: 'future',
      fallback_text: 'Resumen compatible.',
      component: { kind: 'future_component' },
    })

    expect(visual).toEqual({
      componentId: 'future',
      fallbackText: 'Resumen compatible.',
      component: null,
    })
  })

  it('deduplicates a replay by component id', () => {
    const first = {
      componentId: 'summary',
      fallbackText: 'Primero',
      component: null,
    }
    const updated = { ...first, fallbackText: 'Actualizado' }

    expect(mergeVisual(mergeVisual([], first), updated)).toEqual([updated])
  })
})
