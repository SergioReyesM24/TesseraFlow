import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { ConversationMessage } from '../types'
import { MessageList } from './MessageList'

const messages: ConversationMessage[] = [
  {
    id: 'assistant-visual',
    role: 'assistant',
    content: 'Aquí tienes el resumen.',
    visuals: [
      {
        componentId: 'summary-metrics',
        fallbackText: 'Resumen de métricas',
        component: {
          kind: 'metric_group',
          title: 'Resumen visual',
          subtitle: null,
          metrics: [{ label: 'Total', value: '42', unit: null, detail: null }],
        },
      },
    ],
  },
]

const messageWithMetrics: ConversationMessage = {
  id: 'assistant-accounting',
  role: 'assistant',
  content: 'Respuesta con métricas internas.',
  metrics: {
    usage: {
      input_tokens: 100,
      output_tokens: 20,
      total_tokens: 120,
      cached_input_tokens: 40,
      uncached_input_tokens: 60,
      cached_input_audio_tokens: 0,
      reasoning_tokens: 0,
      input_audio_tokens: 0,
      output_audio_tokens: 0,
    },
    cost: null,
    calls: [],
  },
}

describe('MessageList docked visuals', () => {
  it('keeps the visual out of the message while the inspector is open', () => {
    const markup = renderToStaticMarkup(
      <MessageList
        messages={messages}
        emptyTitle="Sin mensajes"
        emptyDescription="No hay actividad"
        showVisuals={false}
      />,
    )

    expect(markup).toContain('Aquí tienes el resumen.')
    expect(markup).not.toContain('Resumen visual')
  })

  it('restores the visual inline after closing the inspector', () => {
    const markup = renderToStaticMarkup(
      <MessageList
        messages={messages}
        emptyTitle="Sin mensajes"
        emptyDescription="No hay actividad"
      />,
    )

    expect(markup).toContain('Resumen visual')
    expect(markup).toContain('Total')
  })

  it('keeps technical token accounting out of the regular conversation', () => {
    const markup = renderToStaticMarkup(
      <MessageList
        messages={[messageWithMetrics]}
        emptyTitle="Sin mensajes"
        emptyDescription="No hay actividad"
      />,
    )

    expect(markup).toContain('Respuesta con métricas internas.')
    expect(markup).not.toContain('Uso de tokens')
    expect(markup).not.toContain('En caché')
  })
})
