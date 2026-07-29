import { describe, expect, it } from 'vitest'
import type { ConversationMessage } from '../types'
import { completeStreamingMessages } from './useRealtimeSocket'

describe('realtime transcript finalization', () => {
  it('completes interrupted bubbles while preserving the new active turn', () => {
    const messages: ConversationMessage[] = [
      {
        id: 'voice-assistant-old-turn',
        role: 'assistant',
        content: 'Respuesta parcial',
        status: 'streaming',
      },
      {
        id: 'voice-user-new-turn',
        role: 'user',
        content: 'Nueva pregunta',
        status: 'streaming',
      },
    ]

    expect(completeStreamingMessages(messages, 'new-turn')).toEqual([
      { ...messages[0], status: 'complete' },
      messages[1],
    ])
  })

  it('completes every remaining bubble when the websocket closes', () => {
    const messages: ConversationMessage[] = [
      {
        id: 'voice-assistant-turn-1',
        role: 'assistant',
        content: 'Respuesta parcial',
        status: 'streaming',
      },
    ]

    expect(completeStreamingMessages(messages)).toEqual([
      { ...messages[0], status: 'complete' },
    ])
  })
})
