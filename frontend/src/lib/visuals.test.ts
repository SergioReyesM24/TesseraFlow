import { describe, expect, it } from 'vitest'
import type { ConversationMessage, VisualPresentation } from '../types'
import { collectDockedVisuals, mergeVisual, parseVisualPresentation } from './visuals'

describe('visual presentation protocol', () => {
  it('parses the closed chart schema', () => {
    const visual = parseVisualPresentation({
      schema: 'tesseraflow.visual',
      version: 1,
      component_id: 'weekly-balance',
      fallback_text: 'El saldo termina en 120 €.',
      component: {
        kind: 'chart',
        title: 'Saldo semanal',
        subtitle: null,
        chart_type: 'line',
        x_axis: { label: 'Semana', min: '01-07-2026', max: '08-07-2026' },
        y_axis: { label: 'Saldo', unit: '€', min: 0, max: 150 },
        series: [
          {
            name: 'Saldo',
            points: [
              { x: '01-07-2026', y: 100 },
              { x: '08-07-2026', y: 120 },
            ],
          },
        ],
      },
    })

    expect(visual?.component?.kind).toBe('chart')
    if (visual?.component?.kind === 'chart') {
      expect(visual.component.x_axis.min).toBe('01-07-2026')
      expect(visual.component.x_axis.max).toBe('08-07-2026')
      expect(visual.component.y_axis.min).toBe(0)
      expect(visual.component.y_axis.max).toBe(150)
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
      placement: 'append',
    })
  })

  it('parses savings and categorized transactions', () => {
    const visual = parseVisualPresentation({
      schema: 'tesseraflow.visual',
      version: 1,
      component_id: 'recent-transactions',
      fallback_text: 'El ahorro actual es de 12.109,16 €.',
      component: {
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
        ],
      },
    })

    expect(visual?.component?.kind).toBe('transaction_list')
    if (visual?.component?.kind === 'transaction_list') {
      expect(visual.component.base_savings).toBe(10000)
      expect(visual.component.transactions[0].merchant).toBe('La Tagliatella')
      expect(visual.component.transactions[0].transaction_type).toBe('expense')
    }
  })

  it('replaces the latest matching component only when requested explicitly', () => {
    const first = {
      componentId: 'summary',
      fallbackText: 'Primero',
      component: null,
      placement: 'append' as const,
    }
    const updated = { ...first, fallbackText: 'Actualizado', placement: 'replace' as const }

    expect(mergeVisual(mergeVisual([], first), updated)).toEqual([updated])
  })

  it('appends inside one turn even when the component id is repeated', () => {
    const visual: VisualPresentation = {
      componentId: 'summary',
      fallbackText: 'Resumen',
      component: null,
      placement: 'append',
    }

    expect(mergeVisual(mergeVisual([], visual), visual)).toEqual([visual, visual])
  })

  it('appends visuals across turns even when their component ids match', () => {
    const first: VisualPresentation = {
      componentId: 'summary',
      fallbackText: 'Primero',
      component: null,
      placement: 'append',
    }
    const second: VisualPresentation = {
      ...first,
      fallbackText: 'Segundo',
    }
    const messages: ConversationMessage[] = [
      { id: 'assistant-1', role: 'assistant', content: '', visuals: [first] },
      { id: 'assistant-2', role: 'assistant', content: '', visuals: [second] },
    ]

    expect(collectDockedVisuals(messages)?.presentations).toEqual([first, second])
  })

  it('replaces only the matching visual when placement explicitly requests it', () => {
    const first: VisualPresentation = {
      componentId: 'balance',
      fallbackText: 'Barras',
      component: null,
      placement: 'append',
    }
    const other: VisualPresentation = {
      componentId: 'transactions',
      fallbackText: 'Movimientos',
      component: null,
      placement: 'append',
    }
    const replacement: VisualPresentation = {
      ...first,
      fallbackText: 'Líneas',
      placement: 'replace',
    }
    const messages: ConversationMessage[] = [
      { id: 'assistant-1', role: 'assistant', content: '', visuals: [first, other] },
      { id: 'assistant-2', role: 'assistant', content: '', visuals: [replacement] },
    ]

    expect(collectDockedVisuals(messages)).toMatchObject({
      presentations: [replacement, other],
      activeIndex: 0,
    })
  })
})
