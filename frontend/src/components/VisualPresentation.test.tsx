import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type {
  ChartVisualComponent,
  TransactionListVisualComponent,
  VisualPresentation as VisualData,
} from '../types'
import { VisualPresentation } from './VisualPresentation'

/** Build the smallest valid chart needed to inspect its interactive SVG markup. */
function chartPresentation(chartType: 'line' | 'bar'): VisualData {
  const component: ChartVisualComponent = {
    kind: 'chart',
    title: 'Saldo',
    subtitle: null,
    chart_type: chartType,
    x_axis: { label: 'Semana' },
    y_axis: { label: 'Saldo', unit: '€' },
    series: [{ name: 'Saldo al cierre', points: [{ x: '19-07-2026', y: 13275.65 }] }],
  }
  return {
    componentId: `balance-${chartType}`,
    fallbackText: 'El saldo al cierre fue 13.275,65 €.',
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
    const description = `19-07-2026, ${value} €`

    expect(markup).toContain('class="chart-data-mark ')
    expect(markup).toContain('tabindex="0"')
    expect(markup).toContain(`aria-label="${description}"`)
    expect(markup).not.toContain('<title>Saldo al cierre')
    if (kind === 'line') {
      expect(markup).toContain('class="chart-line-series"')
      expect(markup).toContain('stroke-width="1.5"')
      expect(markup).toContain('pathLength="1"')
      expect(markup).toContain('class="chart-data-mark chart-line-point"')
      expect(markup).toContain(' r="3"')
    } else {
      expect(markup).toContain('class="chart-data-mark chart-bar"')
      expect(markup).toContain('transform-origin:')
    }
    },
  )

  it('keeps every grouped bar inside the SVG horizontal bounds', () => {
    const presentation = chartPresentation('bar')
    if (presentation.component?.kind !== 'chart') throw new Error('Expected chart fixture')
    presentation.component.series = ['Entrada', 'Salida', 'Caché'].map((name, index) => ({
      name,
      points: [
        { x: 'Turno 1', y: 100 - index * 10 },
        { x: 'Turno 2', y: 140 - index * 10 },
      ],
    }))

    const markup = renderToStaticMarkup(<VisualPresentation presentation={presentation} />)
    const bars = [...markup.matchAll(/<rect[^>]* x="([\d.]+)"[^>]* width="([\d.]+)"/g)]

    expect(bars).toHaveLength(6)
    for (const [, rawX, rawWidth] of bars) {
      const rightEdge = Number(rawX) + Number(rawWidth)
      expect(Number(rawX)).toBeGreaterThanOrEqual(0)
      expect(rightEdge).toBeLessThanOrEqual(640)
    }
  })
})

describe('VisualPresentation transaction list', () => {
  it('shows base savings and separates income from expenses by merchant and amount', () => {
    const component: TransactionListVisualComponent = {
      kind: 'transaction_list',
      title: 'Últimos movimientos',
      subtitle: 'Ingresos y gastos sobre el ahorro base',
      currency: '€',
      base_savings: 10000,
      current_savings: 12109.16,
      total_income: 2450,
      total_expenses: 340.84,
      transactions: [
        {
          booked_at: '22-07-2026 20:14:00+02:00',
          merchant: 'La Tagliatella',
          category: 'comida',
          transaction_type: 'expense',
          amount: 38.6,
          balance_after: 12109.16,
        },
        {
          booked_at: '17-07-2026 09:00:00+02:00',
          merchant: 'Nómina',
          category: 'ingresos',
          transaction_type: 'income',
          amount: 2450,
          balance_after: 12315.84,
        },
      ],
    }

    const markup = renderToStaticMarkup(
      <VisualPresentation
        presentation={{
          componentId: 'recent-transactions',
          fallbackText: 'El ahorro actual es de 12.109,16 €.',
          component,
        }}
      />,
    )

    expect(markup).toContain('Ahorro base')
    expect(markup).toContain('Ahorro actual')
    expect(markup).toContain('La Tagliatella')
    expect(markup).toContain('Nómina')
    expect(markup).toContain('Gasto')
    expect(markup).toContain('Ingreso')
    expect(markup).toContain('Saldo:')
  })
})
