import { describe, expect, it, vi } from 'vitest'
import {
  httpUrl,
  listConversationSessions,
  loadConversationGroup,
  loadConversationHistory,
  loadDailyTokenUsage,
  loadSessionTokenUsage,
  normalizeBaseUrl,
} from './api'

describe('API URL composition', () => {
  it('normalizes trailing slashes without altering the origin', () => {
    expect(normalizeBaseUrl(' https://api.example.test/// ')).toBe(
      'https://api.example.test',
    )
  })

  it('keeps relative endpoints when the frontend uses the Vite proxy', () => {
    expect(httpUrl('/v1/sessions', '')).toBe('/v1/sessions')
  })

  it('joins an explicit API origin and endpoint', () => {
    expect(httpUrl('/v1/sessions', 'http://127.0.0.1:8000/')).toBe(
      'http://127.0.0.1:8000/v1/sessions',
    )
  })

  it('requests paginated technical history through its owner scope', async () => {
    const payload = {
      session_uid: 'session-1',
      user_id: 'user-1',
      title: 'Historial',
      status: 'active',
      version: 1,
      last_sequence: 4,
      created_at: '22-07-2026T10:00:00Z',
      updated_at: '22-07-2026T10:01:00Z',
      last_message_at: '22-07-2026T10:01:00Z',
      items: [],
      has_more: false,
      next_after_sequence: null,
    }
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => payload,
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      loadConversationHistory('http://api.test/', 'user-1', 'session-1', 12, 25),
    ).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith(
      'http://api.test/v1/sessions/session-1/history?user_id=user-1&after_sequence=12&limit=25',
      { signal: undefined },
    )

    vi.unstubAllGlobals()
  })

  it('lists sessions independently from loading their histories', async () => {
    const payload = {
      user_id: 'user-1',
      sessions: [],
      has_more: false,
      next_offset: null,
    }
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => payload,
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      listConversationSessions('http://api.test/', 'user-1', 10, 25),
    ).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith(
      'http://api.test/v1/sessions?user_id=user-1&offset=10&limit=25',
      { signal: undefined },
    )

    vi.unstubAllGlobals()
  })

  it('loads the root and isolated worker conversations through owner scope', async () => {
    const payload = {
      user_id: 'user-1',
      root_conversation_id: 'main-1',
      conversations: [
        {
          correlation: {
            conversation_id: 'main-1',
            root_conversation_id: 'main-1',
            parent_conversation_id: null,
            worker_conversation_id: null,
            thread_id: null,
          },
          jobs: [],
        },
      ],
    }
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => payload,
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      loadConversationGroup('http://api.test/', 'user-1', 'main-1'),
    ).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith(
      'http://api.test/v1/sessions/main-1/group?user_id=user-1',
      { signal: undefined },
    )

    vi.unstubAllGlobals()
  })

  it('loads global daily token metrics for the selected period', async () => {
    const payload = { user_id: 'user-1', timezone: 'UTC', days: [] }
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => payload,
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(loadDailyTokenUsage('http://api.test/', 'user-1', 90)).resolves.toEqual(
      payload,
    )
    expect(fetchMock).toHaveBeenCalledWith(
      'http://api.test/v1/metrics/tokens/daily?user_id=user-1&days=90',
      { signal: undefined },
    )

    vi.unstubAllGlobals()
  })

  it('loads root-plus-worker costs for the selected session', async () => {
    const payload = {
      user_id: 'user-1',
      root_conversation_id: 'main-1',
      usage: { total_tokens: 0 },
      cost: null,
      turn_count: 0,
      model_call_count: 0,
      fully_priced: true,
      models: [],
      conversations: [],
    }
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => payload,
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      loadSessionTokenUsage('http://api.test/', 'user-1', 'main-1'),
    ).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith(
      'http://api.test/v1/sessions/main-1/usage?user_id=user-1',
      { signal: undefined },
    )

    vi.unstubAllGlobals()
  })
})
