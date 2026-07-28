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

describe('VisualPresentation transaction list', () => {
  it('shows base savings and separates income from expenses by merchant and amount', () => {
    const component: TransactionListVisualComponent = {
      kind: 'transaction_list',
      title: 'Últimos movimientos',
      subtitle: 'Ingresos y gastos sobre el ahorro base',
      currency: 'EUR',
      base_savings: 10000,
      current_savings: 12109.16,
      total_income: 2450,
      total_expenses: 340.84,
      transactions: [
        {
          booked_at: '2026-07-22T20:14:00+02:00',
          merchant: 'La Tagliatella',
          category: 'comida',
          transaction_type: 'expense',
          amount: 38.6,
          balance_after: 12109.16,
        },
        {
          booked_at: '2026-07-17T09:00:00+02:00',
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
          fallbackText: 'El ahorro actual es de 12.109,16 EUR.',
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
