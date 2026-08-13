import {
  ArrowLeft,
  Bot,
  ChevronDown,
  Database,
  MessageSquare,
  RefreshCw,
  Search,
  Wrench,
} from 'lucide-react'
import { type FormEvent, useEffect, useMemo, useState } from 'react'
import {
  listConversationSessions,
  loadConversationGroup,
  loadConversationHistory,
  loadDailyTokenUsage,
  loadSessionTokenUsage,
} from '../lib/api'
import type {
  ConversationGroupResponse,
  ConversationHistoryItem,
  ConversationHistoryResponse,
  ConversationListResponse,
  DailyTokenUsageReport,
  SessionUsageReport,
} from '../types'
import { formatTimestamp } from '../lib/dates'
import { DailyTokenAnalytics } from './DailyTokenAnalytics'
import { SessionCostAnalytics } from './SessionCostAnalytics'
import { TokenUsage } from './TokenUsage'

interface ConversationHistoryProps {
  apiBaseUrl: string
  userId: string
  activeSessionUid: string
}

type HistoryMode = 'overview' | 'session'

/** Render arbitrary JSON tool inputs and outputs without losing nested structure. */
function JsonBlock({ value }: { value: unknown }) {
  return <pre className="history-json">{JSON.stringify(value, null, 2)}</pre>
}

interface HistoryRecordProps {
  record: ConversationHistoryItem
  toolResult?: ConversationHistoryItem
}

/** Present one canonical row according to its discriminated domain payload. */
export function HistoryRecord({ record, toolResult }: HistoryRecordProps) {
  const { payload } = record
  if (payload.type === 'tool_call') {
    const resultPayload =
      toolResult?.payload.type === 'tool_result' ? toolResult.payload : null
    return (
      <article className="history-record history-record-tool_call">
        <details className="history-tool-call">
          <summary>
            <span className="history-tool-call-icon" aria-hidden="true">
              <Wrench size={14} />
            </span>
            <strong>{payload.tool_name}</strong>
            <ChevronDown className="history-tool-call-chevron" size={14} aria-hidden="true" />
          </summary>
          <div className="history-tool-call-detail">
            <dl>
              <div>
                <dt>Call ID</dt>
                <dd><code>{payload.call_id}</code></dd>
              </div>
              <div>
                <dt>Secuencia</dt>
                <dd>#{record.sequence}</dd>
              </div>
              <div>
                <dt>Fecha</dt>
                <dd>{formatTimestamp(record.created_at)}</dd>
              </div>
            </dl>
            <span>Argumentos</span>
            <JsonBlock value={payload.arguments} />
            {resultPayload && (
              <div className="history-tool-response">
                <div>
                  <span>Respuesta</span>
                  <span className={`history-result-state ${resultPayload.error ? 'is-error' : ''}`}>
                    {resultPayload.error ? 'error' : 'success'}
                  </span>
                </div>
                <JsonBlock value={resultPayload.error ?? resultPayload.output} />
              </div>
            )}
          </div>
        </details>
      </article>
    )
  }

  if (payload.type === 'tool_result') {
    return (
      <article className="history-record history-record-tool_result">
        <details className="history-tool-call history-orphan-tool-result">
          <summary>
            <span className="history-tool-call-icon" aria-hidden="true">
              <Wrench size={14} />
            </span>
            <strong>Respuesta de herramienta</strong>
            <ChevronDown className="history-tool-call-chevron" size={14} aria-hidden="true" />
          </summary>
          <div className="history-tool-call-detail">
            <dl>
              <div>
                <dt>Call ID</dt>
                <dd><code>{payload.call_id}</code></dd>
              </div>
              <div>
                <dt>Secuencia</dt>
                <dd>#{record.sequence}</dd>
              </div>
              <div>
                <dt>Estado</dt>
                <dd>{payload.error ? 'Error' : 'Success'}</dd>
              </div>
            </dl>
            <span>Respuesta</span>
            <JsonBlock value={payload.error ?? payload.output} />
          </div>
        </details>
      </article>
    )
  }

  const label = payload.role === 'user' ? 'Mensaje de entrada' : 'Respuesta del asistente'
  const alignmentClass = ` history-record-message-${payload.role}`

  return (
    <article className={`history-record history-record-${payload.type}${alignmentClass}`}>
      <div className="history-record-rail" aria-hidden="true">
        {payload.type === 'message' ? <MessageSquare size={15} /> : <Wrench size={15} />}
      </div>
      <div className="history-record-body">
        <header>
          <div>
            <span className="history-record-type">{label}</span>
            <span className="history-record-detail">{payload.source}</span>
          </div>
          <span className="history-sequence">#{record.sequence}</span>
        </header>

        <p className="history-message-content">{payload.content || 'Respuesta vacía'}</p>
        {payload.metrics && (
          <TokenUsage
            usage={payload.metrics.usage}
            cost={payload.metrics.cost}
            calls={payload.metrics.calls.length}
            compact
          />
        )}

        <footer>{formatTimestamp(record.created_at)}</footer>
      </div>
    </article>
  )
}

/** Pair tool results with their call so one disclosure contains the complete exchange. */
export function TurnRecords({ records }: { records: ConversationHistoryItem[] }) {
  const toolResults = new Map<string, ConversationHistoryItem>()
  const toolCallIds = new Set<string>()
  for (const record of records) {
    if (record.payload.type === 'tool_call') toolCallIds.add(record.payload.call_id)
    if (record.payload.type === 'tool_result') {
      toolResults.set(record.payload.call_id, record)
    }
  }

  return records.map((record) => {
    if (record.payload.type === 'tool_result' && toolCallIds.has(record.payload.call_id)) {
      return null
    }
    const result =
      record.payload.type === 'tool_call'
        ? toolResults.get(record.payload.call_id)
        : undefined
    return <HistoryRecord key={record.sequence} record={record} toolResult={result} />
  })
}

/** Show the token overview first and switch to a focused session KPI view on demand. */
export function ConversationHistory({
  apiBaseUrl,
  userId,
  activeSessionUid,
}: ConversationHistoryProps) {
  const [mode, setMode] = useState<HistoryMode>('overview')
  const [draftSessionUid, setDraftSessionUid] = useState(activeSessionUid)
  const [targetSessionUid, setTargetSessionUid] = useState<string | null>(null)
  const [selectedConversationUid, setSelectedConversationUid] = useState<string | null>(null)
  const [reloadCount, setReloadCount] = useState(0)
  const [sessionList, setSessionList] = useState<ConversationListResponse | null>(null)
  const [listLoading, setListLoading] = useState(true)
  const [listLoadingMore, setListLoadingMore] = useState(false)
  const [listError, setListError] = useState<string | null>(null)
  const [dailyRange, setDailyRange] = useState(7)
  const [dailyUsage, setDailyUsage] = useState<DailyTokenUsageReport | null>(null)
  const [dailyUsageLoading, setDailyUsageLoading] = useState(true)
  const [dailyUsageError, setDailyUsageError] = useState<string | null>(null)
  const [sessionUsage, setSessionUsage] = useState<SessionUsageReport | null>(null)
  const [sessionUsageLoading, setSessionUsageLoading] = useState(false)
  const [sessionUsageError, setSessionUsageError] = useState<string | null>(null)
  const [group, setGroup] = useState<ConversationGroupResponse | null>(null)
  const [groupLoading, setGroupLoading] = useState(false)
  const [groupError, setGroupError] = useState<string | null>(null)
  const [history, setHistory] = useState<ConversationHistoryResponse | null>(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyLoadingMore, setHistoryLoadingMore] = useState(false)
  const [historyError, setHistoryError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    void loadDailyTokenUsage(apiBaseUrl, userId, dailyRange, controller.signal)
      .then(setDailyUsage)
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return
        setDailyUsageError(
          reason instanceof Error
            ? reason.message
            : 'No se pudieron cargar las métricas globales.',
        )
      })
      .finally(() => {
        if (!controller.signal.aborted) setDailyUsageLoading(false)
      })
    return () => controller.abort()
  }, [apiBaseUrl, dailyRange, userId])

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
    if (mode !== 'session' || !targetSessionUid) {
      return
    }

    const controller = new AbortController()
    void loadSessionTokenUsage(apiBaseUrl, userId, targetSessionUid, controller.signal)
      .then(setSessionUsage)
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return
        setSessionUsage(null)
        setSessionUsageError(
          reason instanceof Error
            ? reason.message
            : 'No se pudieron cargar los costes de la sesión.',
        )
      })
      .finally(() => {
        if (!controller.signal.aborted) setSessionUsageLoading(false)
      })
    return () => controller.abort()
  }, [apiBaseUrl, mode, reloadCount, targetSessionUid, userId])

  useEffect(() => {
    if (mode !== 'session' || !targetSessionUid) return

    const controller = new AbortController()
    void loadConversationGroup(apiBaseUrl, userId, targetSessionUid, controller.signal)
      .then((nextGroup) => {
        setGroup(nextGroup)
        const requestedMember = nextGroup.conversations.find(
          (member) => member.correlation.conversation_id === targetSessionUid,
        )
        setSelectedConversationUid(
          requestedMember?.correlation.conversation_id ?? nextGroup.root_conversation_id,
        )
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return
        setGroup(null)
        setGroupError(
          reason instanceof Error
            ? reason.message
            : 'No se pudo cargar el grupo de conversaciones.',
        )
      })
      .finally(() => {
        if (!controller.signal.aborted) setGroupLoading(false)
      })
    return () => controller.abort()
  }, [apiBaseUrl, mode, reloadCount, targetSessionUid, userId])

  useEffect(() => {
    if (mode !== 'session' || !selectedConversationUid) return

    const controller = new AbortController()
    void loadConversationHistory(
      apiBaseUrl,
      userId,
      selectedConversationUid,
      0,
      50,
      controller.signal,
    )
      .then(setHistory)
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return
        setHistory(null)
        setHistoryError(
          reason instanceof Error ? reason.message : 'No se pudo cargar el historial.',
        )
      })
      .finally(() => {
        if (!controller.signal.aborted) setHistoryLoading(false)
      })
    return () => controller.abort()
  }, [apiBaseUrl, mode, reloadCount, selectedConversationUid, userId])

  const turns = useMemo(() => {
    const grouped = new Map<string, ConversationHistoryItem[]>()
    for (const item of history?.items ?? []) {
      const records = grouped.get(item.turn_id) ?? []
      records.push(item)
      grouped.set(item.turn_id, records)
    }
    return [...grouped.entries()]
  }, [history])

  const selectDailyRange = (days: number) => {
    setDailyUsageLoading(true)
    setDailyUsageError(null)
    setDailyRange(days)
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const next = draftSessionUid.trim()
    if (!next) return
    openSession(next)
  }

  const openSession = (sessionUid: string) => {
    setDraftSessionUid(sessionUid)
    setMode('session')
    setSessionUsageLoading(true)
    setSessionUsageError(null)
    setSessionUsage(null)
    setGroupLoading(true)
    setGroupError(null)
    setGroup(null)
    setHistoryLoading(true)
    setHistoryError(null)
    setHistory(null)
    setSelectedConversationUid(sessionUid)
    if (sessionUid === targetSessionUid) setReloadCount((value) => value + 1)
    else setTargetSessionUid(sessionUid)
  }

  const showOverview = () => {
    setMode('overview')
    setTargetSessionUid(null)
    setSessionUsage(null)
    setSessionUsageError(null)
    setSessionUsageLoading(false)
    setSelectedConversationUid(null)
    setGroup(null)
    setGroupError(null)
    setGroupLoading(false)
    setHistory(null)
    setHistoryError(null)
    setHistoryLoading(false)
  }

  /** Switch between the root and worker histories without merging their records. */
  const selectConversation = (conversationId: string) => {
    if (conversationId === selectedConversationUid) return
    setHistoryLoading(true)
    setHistoryError(null)
    setHistory(null)
    setSelectedConversationUid(conversationId)
  }

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

  /** Append the next canonical page while retaining already visible turns. */
  const loadMoreHistory = async () => {
    if (
      !history?.has_more ||
      history.next_after_sequence === null ||
      !selectedConversationUid
    ) {
      return
    }
    setHistoryLoadingMore(true)
    setHistoryError(null)
    try {
      const next = await loadConversationHistory(
        apiBaseUrl,
        userId,
        selectedConversationUid,
        history.next_after_sequence,
      )
      setHistory({ ...next, items: [...history.items, ...next.items] })
    } catch (reason) {
      setHistoryError(
        reason instanceof Error
          ? reason.message
          : 'No se pudo cargar la página siguiente.',
      )
    } finally {
      setHistoryLoadingMore(false)
    }
  }

  return (
    <section className="history-view">
      <div className="history-toolbar">
        <div>
          <span className="eyebrow">Costes y uso</span>
          <h1>{mode === 'overview' ? 'Histórico de tokens' : 'Detalle de sesión'}</h1>
          <p>
            {mode === 'overview'
              ? 'Resumen de los últimos siete días con acceso rápido a cada sesión.'
              : 'Costes, turnos y actividad técnica de la sesión principal y sus workers.'}
          </p>
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
            <button
              type="submit"
              disabled={sessionUsageLoading || groupLoading || historyLoading}
              aria-label="Consultar sesión"
            >
              {sessionUsageLoading || groupLoading || historyLoading ? (
                <RefreshCw className="spin" size={17} />
              ) : (
                <Search size={17} />
              )}
              Consultar
            </button>
          </div>
        </form>
      </div>

      <div className="history-scroll">
        {mode === 'overview' ? (
          <>
            <DailyTokenAnalytics
              report={dailyUsage}
              rangeDays={dailyRange}
              loading={dailyUsageLoading}
              error={dailyUsageError}
              onRangeChange={selectDailyRange}
            />
            <div className="history-section-divider" role="separator">
              <span>Selecciona una sesión</span>
            </div>
            <section className="history-session-picker" aria-label="Sesiones del usuario">
              <header>
                <div>
                  <span className="eyebrow">Detalle bajo demanda</span>
                  <strong>Sesiones principales</strong>
                  <p>Abre una sesión para ver sus costes, turnos y tool calls.</p>
                </div>
                <span>
                  {listLoading && <RefreshCw className="spin" size={14} aria-label="Cargando" />}
                  {sessionList?.sessions.length ?? 0} cargadas
                </span>
              </header>

              {listError && <div className="history-list-error" role="alert">{listError}</div>}
              {sessionList?.sessions.length === 0 && !listLoading && (
                <div className="history-list-empty">No hay sesiones para este usuario.</div>
              )}
              <div className="history-session-list">
                {sessionList?.sessions.map((session) => {
                  const isActive = session.session_uid === targetSessionUid
                  return (
                    <button
                      className={isActive ? 'active' : ''}
                      type="button"
                      key={session.session_uid}
                      aria-pressed={isActive}
                      onClick={() => openSession(session.session_uid)}
                    >
                      <span className="history-list-title">{session.title}</span>
                      <code>{session.session_uid}</code>
                      <span className="history-list-meta">
                        <span>v{session.version} · {session.last_sequence} registros</span>
                        <time dateTime={session.updated_at}>
                          {formatTimestamp(session.updated_at)}
                        </time>
                      </span>
                    </button>
                  )
                })}
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
            </section>
          </>
        ) : (
          <div className="history-session-focus">
            <button className="history-back-button" type="button" onClick={showOverview}>
              <ArrowLeft size={16} />
              Volver al histórico de 7 días
            </button>
            <SessionCostAnalytics
              report={sessionUsage}
              loading={sessionUsageLoading}
              error={sessionUsageError}
            />

            <div className="history-section-divider" role="separator">
              <span>Turnos y actividad técnica</span>
            </div>

            <div className="history-detail">
              {groupError && <div className="history-error" role="alert">{groupError}</div>}
              {historyError && <div className="history-error" role="alert">{historyError}</div>}
              {(groupLoading || historyLoading) && !history && !historyError && (
                <div className="history-detail-loading">
                  <RefreshCw className="spin" size={18} />
                  Cargando registros de la sesión…
                </div>
              )}

              {group && (
                <section
                  className="history-conversation-group"
                  aria-label="Conversaciones relacionadas"
                >
                  <header>
                    <div>
                      <span className="eyebrow">Conversación de usuario</span>
                      <strong>Historiales aislados</strong>
                    </div>
                    <span>{group.conversations.length} conversaciones</span>
                  </header>
                  <div className="history-conversation-tabs">
                    {group.conversations.map((member, index) => {
                      const correlation = member.correlation
                      const isRoot = correlation.parent_conversation_id === null
                      const isActive =
                        correlation.conversation_id === selectedConversationUid
                      return (
                        <button
                          className={isActive ? 'active' : ''}
                          type="button"
                          key={correlation.conversation_id}
                          aria-pressed={isActive}
                          onClick={() => selectConversation(correlation.conversation_id)}
                        >
                          <span className="history-conversation-icon" aria-hidden="true">
                            {isRoot ? <MessageSquare size={16} /> : <Bot size={16} />}
                          </span>
                          <span className="history-conversation-copy">
                            <strong>{isRoot ? 'Principal' : `Agente interno ${index}`}</strong>
                            <code>{correlation.conversation_id}</code>
                            {!isRoot && (
                              <small>
                                {member.jobs.length} {member.jobs.length === 1 ? 'job' : 'jobs'}
                              </small>
                            )}
                          </span>
                        </button>
                      )
                    })}
                  </div>
                </section>
              )}

              {history && (
                <>
                  <div className="history-summary">
                    <div className="history-session-heading">
                      {history.correlation.parent_conversation_id === null ? (
                        <Database size={20} />
                      ) : (
                        <Bot size={20} />
                      )}
                      <div>
                        <span className="history-session-kind">
                          {history.correlation.parent_conversation_id === null
                            ? 'Conversación principal'
                            : 'Conversación interna aislada'}
                        </span>
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
                      <div>
                        <dt>Último mensaje</dt>
                        <dd>{formatTimestamp(history.last_message_at)}</dd>
                      </div>
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
                            <TurnRecords records={records} />
                          </div>
                        </section>
                      ))}
                    </div>
                  )}

                  {history.has_more && (
                    <button
                      className="history-load-more"
                      type="button"
                      disabled={historyLoadingMore}
                      onClick={() => void loadMoreHistory()}
                    >
                      {historyLoadingMore ? (
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
        )}
      </div>
    </section>
  )
}
