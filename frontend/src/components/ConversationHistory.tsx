import {
  ChevronDown,
  Database,
  MessageSquare,
  RefreshCw,
  Search,
  Wrench,
} from 'lucide-react'
import { type FormEvent, useEffect, useMemo, useState } from 'react'
import { listConversationSessions, loadConversationHistory } from '../lib/api'
import type {
  ConversationHistoryItem,
  ConversationHistoryResponse,
  ConversationListResponse,
} from '../types'

interface ConversationHistoryProps {
  apiBaseUrl: string
  userId: string
  activeSessionUid: string
}

/** Format persisted timestamps in the browser locale while preserving precision. */
function formatTimestamp(value: string | null): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat('es-ES', {
    dateStyle: 'short',
    timeStyle: 'medium',
  }).format(new Date(value))
}

/** Render arbitrary JSON tool inputs and outputs without losing nested structure. */
function JsonBlock({ value }: { value: unknown }) {
  return <pre className="history-json">{JSON.stringify(value, null, 2)}</pre>
}

/** Present one canonical row according to its discriminated domain payload. */
function HistoryRecord({ record }: { record: ConversationHistoryItem }) {
  const { payload } = record
  const label =
    payload.type === 'message'
      ? payload.role === 'user'
        ? 'Mensaje de entrada'
        : 'Respuesta del asistente'
      : payload.type === 'tool_call'
        ? 'Tool call'
        : 'Respuesta de tool'

  return (
    <article className={`history-record history-record-${payload.type}`}>
      <div className="history-record-rail" aria-hidden="true">
        {payload.type === 'message' ? <MessageSquare size={15} /> : <Wrench size={15} />}
      </div>
      <div className="history-record-body">
        <header>
          <div>
            <span className="history-record-type">{label}</span>
            {payload.type === 'message' ? (
              <span className="history-record-detail">{payload.source}</span>
            ) : (
              <code>{payload.call_id}</code>
            )}
          </div>
          <span className="history-sequence">#{record.sequence}</span>
        </header>

        {payload.type === 'message' && (
          <p className="history-message-content">{payload.content || 'Respuesta vacía'}</p>
        )}
        {payload.type === 'tool_call' && (
          <div className="history-tool-content">
            <strong>{payload.tool_name}</strong>
            <span>arguments</span>
            <JsonBlock value={payload.arguments} />
          </div>
        )}
        {payload.type === 'tool_result' && (
          <div className="history-tool-content">
            <span className={`history-result-state ${payload.error ? 'is-error' : ''}`}>
              {payload.error ? 'error' : 'success'}
            </span>
            <span>{payload.error ? 'error' : 'output'}</span>
            <JsonBlock value={payload.error ?? payload.output} />
          </div>
        )}

        <footer>{formatTimestamp(record.created_at)}</footer>
      </div>
    </article>
  )
}

/** Inspect canonical PostgreSQL history by session identifier. */
export function ConversationHistory({
  apiBaseUrl,
  userId,
  activeSessionUid,
}: ConversationHistoryProps) {
  const [draftSessionUid, setDraftSessionUid] = useState(activeSessionUid)
  const [targetSessionUid, setTargetSessionUid] = useState(activeSessionUid)
  const [reloadCount, setReloadCount] = useState(0)
  const [history, setHistory] = useState<ConversationHistoryResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sessionList, setSessionList] = useState<ConversationListResponse | null>(null)
  const [listLoading, setListLoading] = useState(true)
  const [listLoadingMore, setListLoadingMore] = useState(false)
  const [listError, setListError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    void listConversationSessions(apiBaseUrl, userId, 0, 50, controller.signal)
      .then(setSessionList)
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return
        setListError(
          reason instanceof Error ? reason.message : 'No se pudo cargar la lista de sesiones.',
        )
      })
      .finally(() => {
        if (!controller.signal.aborted) setListLoading(false)
      })
    return () => controller.abort()
  }, [apiBaseUrl, userId])

  useEffect(() => {
    const controller = new AbortController()
    void loadConversationHistory(
      apiBaseUrl,
      userId,
      targetSessionUid,
      0,
      50,
      controller.signal,
    )
      .then(setHistory)
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return
        setHistory(null)
        setError(reason instanceof Error ? reason.message : 'No se pudo cargar el historial.')
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [apiBaseUrl, reloadCount, targetSessionUid, userId])

  const turns = useMemo(() => {
    const grouped = new Map<string, ConversationHistoryItem[]>()
    for (const item of history?.items ?? []) {
      const values = grouped.get(item.turn_id) ?? []
      values.push(item)
      grouped.set(item.turn_id, values)
    }
    return [...grouped.entries()]
  }, [history])

  /** Inspect the requested UUID or refresh when it is already selected. */
  const submit = (event: FormEvent) => {
    event.preventDefault()
    const next = draftSessionUid.trim()
    if (!next) return
    setLoading(true)
    setError(null)
    setHistory(null)
    if (next === targetSessionUid) setReloadCount((value) => value + 1)
    else setTargetSessionUid(next)
  }

  /** Select a listed session and load its canonical detail immediately. */
  const selectSession = (sessionUid: string) => {
    setDraftSessionUid(sessionUid)
    setLoading(true)
    setError(null)
    setHistory(null)
    if (sessionUid === targetSessionUid) setReloadCount((value) => value + 1)
    else setTargetSessionUid(sessionUid)
  }

  /** Append another page of session headers to the browser. */
  const loadMoreSessions = async () => {
    if (!sessionList?.has_more || sessionList.next_offset === null) return
    setListLoadingMore(true)
    setListError(null)
    try {
      const next = await listConversationSessions(
        apiBaseUrl,
        userId,
        sessionList.next_offset,
      )
      setSessionList({ ...next, sessions: [...sessionList.sessions, ...next.sessions] })
    } catch (reason) {
      setListError(
        reason instanceof Error ? reason.message : 'No se pudo cargar la página siguiente.',
      )
    } finally {
      setListLoadingMore(false)
    }
  }

  /** Append the next canonical item page without disturbing the current scroll. */
  const loadMore = async () => {
    if (!history?.has_more || history.next_after_sequence === null) return
    setLoadingMore(true)
    setError(null)
    try {
      const next = await loadConversationHistory(
        apiBaseUrl,
        userId,
        targetSessionUid,
        history.next_after_sequence,
      )
      setHistory({ ...next, items: [...history.items, ...next.items] })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'No se pudo cargar la página siguiente.')
    } finally {
      setLoadingMore(false)
    }
  }

  return (
    <section className="history-view">
      <div className="history-toolbar">
        <div>
          <span className="eyebrow">PostgreSQL canónico</span>
          <h1>Historial técnico</h1>
          <p>Mensajes, tool calls y resultados en el orden exacto persistido.</p>
        </div>
        <form className="history-search" onSubmit={submit}>
          <label htmlFor="history-session-id">Session ID</label>
          <div>
            <input
              id="history-session-id"
              value={draftSessionUid}
              onChange={(event) => setDraftSessionUid(event.target.value)}
              placeholder="UUID de la sesión"
              required
            />
            <button type="submit" disabled={loading} aria-label="Consultar sesión">
              {loading ? <RefreshCw className="spin" size={17} /> : <Search size={17} />}
              Consultar
            </button>
          </div>
        </form>
      </div>

      <div className="history-scroll">
        <div className="history-browser">
          <aside className="history-session-browser" aria-label="Sesiones del usuario">
            <header>
              <div>
                <strong>Sesiones</strong>
                <span>{sessionList?.sessions.length ?? 0} cargadas</span>
              </div>
              {listLoading && <RefreshCw className="spin" size={14} aria-label="Cargando" />}
            </header>

            {listError && <div className="history-list-error" role="alert">{listError}</div>}
            {sessionList?.sessions.length === 0 && !listLoading && (
              <div className="history-list-empty">No hay sesiones para este usuario.</div>
            )}
            <div className="history-session-list">
              {sessionList?.sessions.map((session) => (
                <button
                  className={session.session_uid === targetSessionUid ? 'active' : ''}
                  type="button"
                  key={session.session_uid}
                  aria-pressed={session.session_uid === targetSessionUid}
                  onClick={() => selectSession(session.session_uid)}
                >
                  <span className="history-list-title">{session.title}</span>
                  <code>{session.session_uid}</code>
                  <span className="history-list-meta">
                    <span>v{session.version} · {session.last_sequence} registros</span>
                    <time dateTime={session.updated_at}>{formatTimestamp(session.updated_at)}</time>
                  </span>
                </button>
              ))}
            </div>
            {sessionList?.has_more && (
              <button
                className="history-list-more"
                type="button"
                disabled={listLoadingMore}
                onClick={() => void loadMoreSessions()}
              >
                {listLoadingMore ? (
                  <RefreshCw className="spin" size={14} />
                ) : (
                  <ChevronDown size={14} />
                )}
                Más sesiones
              </button>
            )}
          </aside>

          <div className="history-detail">
            {error && <div className="history-error" role="alert">{error}</div>}
            {loading && !history && !error && (
              <div className="history-detail-loading">
                <RefreshCw className="spin" size={18} />
                Cargando registros de la sesión…
              </div>
            )}

            {history && (
              <>
                <div className="history-summary">
              <div className="history-session-heading">
                <Database size={20} />
                <div>
                  <strong>{history.title}</strong>
                  <code>{history.session_uid}</code>
                </div>
                <span className={`history-status history-status-${history.status}`}>
                  {history.status}
                </span>
              </div>
              <dl>
                <div><dt>Versión</dt><dd>{history.version}</dd></div>
                <div><dt>Última secuencia</dt><dd>{history.last_sequence}</dd></div>
                <div><dt>Creada</dt><dd>{formatTimestamp(history.created_at)}</dd></div>
                <div><dt>Último mensaje</dt><dd>{formatTimestamp(history.last_message_at)}</dd></div>
              </dl>
                </div>

                {turns.length === 0 ? (
                  <div className="history-empty">
                    <Database size={24} />
                    <strong>La sesión todavía no tiene registros</strong>
                    <span>Los elementos aparecerán al completar el primer turno.</span>
                  </div>
                ) : (
                  <div className="history-turns">
                    {turns.map(([turnId, records], index) => (
                      <section className="history-turn" key={turnId}>
                        <header className="history-turn-header">
                          <span>Turno {index + 1}</span>
                          <code>{turnId}</code>
                          <small>{records.length} registros</small>
                        </header>
                        <div className="history-records">
                          {records.map((record) => (
                            <HistoryRecord key={record.sequence} record={record} />
                          ))}
                        </div>
                      </section>
                    ))}
                  </div>
                )}

                {history.has_more && (
                  <button
                    className="history-load-more"
                    type="button"
                    disabled={loadingMore}
                    onClick={() => void loadMore()}
                  >
                    {loadingMore ? (
                      <RefreshCw className="spin" size={16} />
                    ) : (
                      <ChevronDown size={16} />
                    )}
                    Cargar más registros
                  </button>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </section>
  )
}
