import {
  AudioLines,
  Bot,
  ChevronDown,
  Database,
  LoaderCircle,
  MessageCircle,
  Mic,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Settings,
  SlidersHorizontal,
  Square,
  Volume2,
  X,
} from 'lucide-react'
import { type FormEvent, useCallback, useEffect, useState } from 'react'
import { Composer } from './components/Composer'
import { ConversationHistory } from './components/ConversationHistory'
import { MessageList } from './components/MessageList'
import { StatusPill } from './components/StatusPill'
import { VisualPresentation } from './components/VisualPresentation'
import { useAgentSocket } from './hooks/useAgentSocket'
import { useRealtimeSocket } from './hooks/useRealtimeSocket'
import { createSession } from './lib/api'
import type { Mode, VisualPresentation as VisualPresentationData } from './types'

const VISUAL_GENERATION_DELAY_MS = 1050

interface WorkspaceProps {
  apiBaseUrl: string
  sessionUid: string
  userId: string
  mode: Mode
  onModeChange: (mode: Mode) => void
  onNewSession: () => void
  onOpenSettings: () => void
  creatingSession: boolean
}

interface SettingsDialogProps {
  apiBaseUrl: string
  userId: string
  onClose: () => void
  onSave: (apiBaseUrl: string, userId: string) => void
}

interface VisualInspectorProps {
  presentation: VisualPresentationData
  onClose: () => void
}

/** Create a device-local anonymous owner identifier for the unauthenticated demo API. */
function defaultUserId(): string {
  const stored = window.localStorage.getItem('tesseraflow.userId')
  if (stored) return stored
  const generated = `web-${crypto.randomUUID().slice(0, 8)}`
  window.localStorage.setItem('tesseraflow.userId', generated)
  return generated
}

/** Edit connection settings and start a fresh owned session after saving. */
function SettingsDialog({
  apiBaseUrl,
  userId,
  onClose,
  onSave,
}: SettingsDialogProps) {
  const [draftBaseUrl, setDraftBaseUrl] = useState(apiBaseUrl)
  const [draftUserId, setDraftUserId] = useState(userId)

  /** Validate the minimal client-side settings before handing them to the app. */
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!draftUserId.trim()) return
    onSave(draftBaseUrl.trim(), draftUserId.trim())
  }

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="settings-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="dialog-header">
          <div>
            <span className="eyebrow">Conexión</span>
            <h2 id="settings-title">Configurar TesseraFlow</h2>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="Cerrar">
            <X size={19} />
          </button>
        </div>
        <form onSubmit={submit}>
          <label>
            Identificador de usuario
            <input
              value={draftUserId}
              onChange={(event) => setDraftUserId(event.target.value)}
              maxLength={128}
              required
            />
            <small>Se usa como propietario de las sesiones en esta demo sin autenticación.</small>
          </label>
          <label>
            URL de la API
            <input
              value={draftBaseUrl}
              onChange={(event) => setDraftBaseUrl(event.target.value)}
              placeholder="Vacío para usar el mismo origen"
              inputMode="url"
            />
            <small>En desarrollo puedes dejarla vacía: Vite redirige la API local.</small>
          </label>
          <div className="dialog-actions">
            <button className="button-ghost" type="button" onClick={onClose}>
              Cancelar
            </button>
            <button className="button-primary" type="submit">
              Guardar y crear sesión
            </button>
          </div>
        </form>
      </section>
    </div>
  )
}

/** Grow a visual result out of an assistant-style message before revealing its contents. */
function VisualInspector({ presentation, onClose }: VisualInspectorProps) {
  const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  const [isReady, setIsReady] = useState(prefersReducedMotion)

  useEffect(() => {
    if (prefersReducedMotion) return

    const timer = window.setTimeout(() => setIsReady(true), VISUAL_GENERATION_DELAY_MS)
    return () => window.clearTimeout(timer)
  }, [prefersReducedMotion, presentation.componentId])

  return (
    <aside
      className={`visual-inspector ${isReady ? 'is-ready' : 'is-generating'}`}
      aria-label="Componente visual del agente"
      aria-busy={!isReady}
    >
      <header className="visual-inspector-header">
        <div className="visual-assistant-message" aria-live="polite">
          <span className="visual-assistant-avatar" aria-hidden="true">
            <Bot size={16} />
          </span>
          <span className="visual-assistant-copy">
            <span>TesseraFlow</span>
            <strong>
              {isReady ? 'He preparado esta vista para ti' : 'Generando componente visual…'}
            </strong>
          </span>
          {!isReady && <LoaderCircle className="spin" size={15} aria-hidden="true" />}
        </div>
        <button
          className="icon-button"
          type="button"
          onClick={onClose}
          aria-label="Cerrar vista visual"
        >
          <X size={19} />
        </button>
      </header>
      <div className="visual-inspector-content">
        {isReady ? (
          <div className="visual-generated-content">
            <VisualPresentation presentation={presentation} />
          </div>
        ) : (
          <div className="visual-generating-placeholder" aria-hidden="true">
            <span className="visual-skeleton-title" />
            <span className="visual-skeleton-subtitle" />
            <div className="visual-skeleton-canvas">
              <i />
              <i />
              <i />
              <i />
            </div>
          </div>
        )}
      </div>
    </aside>
  )
}

/** Hold both conversation transports so switching modes preserves visible transcripts. */
function Workspace({
  apiBaseUrl,
  sessionUid,
  userId,
  mode,
  onModeChange,
  onNewSession,
  onOpenSettings,
  creatingSession,
}: WorkspaceProps) {
  const [navigationPreference, setNavigationPreference] = useState({
    visualKey: null as string | null,
    expanded: true,
  })
  const [dismissedVisualKey, setDismissedVisualKey] = useState<string | null>(null)
  const text = useAgentSocket({
    apiBaseUrl,
    sessionUid,
    userId,
    enabled: mode === 'text',
  })
  const voice = useRealtimeSocket({
    apiBaseUrl,
    sessionUid,
    userId,
    enabled: mode === 'voice',
  })
  const activeConnection =
    mode === 'history' ? 'connected' : mode === 'text' ? text.connection : voice.connection
  const activeError = mode === 'history' ? null : mode === 'text' ? text.error : voice.error
  const activeMessages = mode === 'text' ? text.messages : mode === 'voice' ? voice.messages : []
  const latestVisualEntry = (() => {
    for (let index = activeMessages.length - 1; index >= 0; index -= 1) {
      const visuals = activeMessages[index].visuals
      if (visuals?.length) {
        const presentation = visuals[visuals.length - 1]
        return {
          presentation,
          key: `${activeMessages[index].id}:${presentation.componentId}`,
        }
      }
    }
    return null
  })()
  const latestVisual = latestVisualEntry?.presentation ?? null
  const latestVisualKey = latestVisualEntry?.key ?? null
  const activeVisual =
    latestVisual && latestVisualKey !== dismissedVisualKey ? latestVisual : null
  const navigationExpanded =
    !activeVisual ||
    (navigationPreference.visualKey === latestVisualKey &&
      navigationPreference.expanded)

  /** Enter voice mode while the click still grants browser audio permission. */
  const selectVoiceMode = () => {
    void voice.activateAudio().catch(() => undefined)
    onModeChange('voice')
  }

  /** Return a dismissed visual to its original inline message position. */
  const closeVisual = () => {
    if (!activeVisual || !latestVisualKey) return
    setDismissedVisualKey(latestVisualKey)
  }

  return (
    <div
      className={`app-shell${activeVisual ? ' has-visual' : ''}${
        navigationExpanded ? ' sidebar-expanded' : ' sidebar-collapsed'
      }`}
    >
      <aside className="sidebar">
        <div className="brand-row">
          <div className="brand-mark" aria-hidden="true">T</div>
          <div className="brand-copy">
            <strong>TesseraFlow</strong>
            <span>Agente multimodal</span>
          </div>
        </div>

        <button
          className="sidebar-collapse-toggle"
          type="button"
          aria-label={navigationExpanded ? 'Contraer navegación' : 'Desplegar navegación'}
          aria-expanded={navigationExpanded}
          onClick={() =>
            setNavigationPreference({
              visualKey: latestVisualKey,
              expanded: !navigationExpanded,
            })
          }
        >
          {navigationExpanded ? <PanelLeftClose size={18} /> : <PanelLeftOpen size={18} />}
          <span>{navigationExpanded ? 'Contraer' : 'Menú'}</span>
        </button>

        <button
          className="new-chat-button"
          type="button"
          onClick={onNewSession}
          disabled={creatingSession}
        >
          <Plus size={18} />
          Nueva conversación
        </button>

        <nav className="mode-nav" aria-label="Modos de conversación">
          <span className="nav-caption">Conversar con</span>
          <button
            className={mode === 'text' ? 'active' : ''}
            type="button"
            onClick={() => onModeChange('text')}
          >
            <MessageCircle size={18} />
            <span>
              Chat de texto
              <small>WebSocket persistente</small>
            </span>
          </button>
          <button
            className={mode === 'voice' ? 'active' : ''}
            type="button"
            onClick={selectVoiceMode}
          >
            <AudioLines size={18} />
            <span>
              Voz realtime
              <small>Audio bidireccional</small>
            </span>
          </button>
          <button
            className={mode === 'history' ? 'active' : ''}
            type="button"
            onClick={() => onModeChange('history')}
          >
            <Database size={18} />
            <span>
              Historial técnico
              <small>Registros PostgreSQL</small>
            </span>
          </button>
        </nav>

        <div className="sidebar-spacer" />
        <div className="session-card">
          <span>Sesión activa</span>
          <code title={sessionUid}>{sessionUid.slice(0, 8)}…{sessionUid.slice(-4)}</code>
        </div>
        <button className="settings-button" type="button" onClick={onOpenSettings}>
          <Settings size={18} />
          Configuración
        </button>
      </aside>

      <main className="main-panel">
        <header className="topbar">
          <div className="mobile-brand">
            <div className="brand-mark" aria-hidden="true">T</div>
          </div>
          <button className="model-selector" type="button" aria-label="Agente seleccionado">
            TesseraFlow{' '}
            <span>
              {mode === 'text' ? 'Texto' : mode === 'voice' ? 'Realtime' : 'Historial'}
            </span>
            <ChevronDown size={15} />
          </button>
          <StatusPill
            state={activeConnection}
            label={
              mode === 'history'
                ? 'Persistido'
                : mode === 'voice' && voice.ready
                  ? 'Audio listo'
                  : undefined
            }
          />
        </header>

        <div className={`conversation-view ${mode === 'voice' ? 'voice-view' : ''}`}>
          {activeError && (
            <div className="error-banner" role="alert">
              <span>{activeError}</span>
              <button type="button" onClick={onOpenSettings}>Revisar conexión</button>
            </div>
          )}

          {mode === 'history' ? (
            <ConversationHistory
              apiBaseUrl={apiBaseUrl}
              userId={userId}
              activeSessionUid={sessionUid}
            />
          ) : mode === 'text' ? (
            <>
              <MessageList
                messages={text.messages}
                emptyTitle="¿En qué puedo ayudarte?"
                emptyDescription="Pregunta, delega una tarea o continúa un trabajo anterior."
                showVisuals={!activeVisual}
              >
                <div className="welcome-mark" aria-hidden="true">
                  <MessageCircle size={28} />
                </div>
              </MessageList>
              <Composer
                onSend={text.sendMessage}
                disabled={text.connection !== 'connected'}
                placeholder="Escribe un mensaje a TesseraFlow"
                voiceShortcut={selectVoiceMode}
              />
            </>
          ) : (
            <>
              <MessageList
                messages={voice.messages}
                emptyTitle={voice.recording ? 'Te escucho…' : 'Habla con TesseraFlow'}
                emptyDescription={
                  voice.recording
                    ? 'Puedes interrumpir la respuesta en cualquier momento.'
                    : 'Una conversación natural, con transcripción y audio en tiempo real.'
                }
                showVisuals={!activeVisual}
              >
                <div className={`voice-orb ${voice.recording ? 'is-listening' : ''}`}>
                  <span />
                  <AudioLines size={34} aria-hidden="true" />
                </div>
              </MessageList>

              <div className="voice-controls">
                <div className="voice-state">
                  <Volume2 size={16} />
                  <span>
                    {voice.recording
                      ? 'Micrófono activo'
                      : voice.ready
                        ? 'Pulsa para hablar'
                        : 'Preparando audio'}
                  </span>
                </div>
                <button
                  className={`record-button ${voice.recording ? 'recording' : ''}`}
                  type="button"
                  disabled={!voice.ready}
                  onClick={() =>
                    void (voice.recording ? voice.stopRecording() : voice.startRecording())
                  }
                  aria-label={voice.recording ? 'Detener micrófono' : 'Activar micrófono'}
                >
                  {voice.recording ? <Square size={22} fill="currentColor" /> : <Mic size={24} />}
                  <span className="record-ring" />
                </button>
                <span className="voice-hint">
                  {voice.recording
                    ? 'Pulsa para terminar tu turno'
                    : `PCM16 · ${voice.inputSampleRate / 1000} kHz`}
                </span>
              </div>

              <Composer
                onSend={voice.sendText}
                disabled={!voice.ready || voice.recording}
                placeholder="O escribe si prefieres…"
              />
            </>
          )}
        </div>
      </main>

      {activeVisual && (
        <VisualInspector
          key={latestVisualKey}
          presentation={activeVisual}
          onClose={closeVisual}
        />
      )}

      <div className="mobile-mode-switch" aria-label="Cambiar modo">
        <button
          className={mode === 'text' ? 'active' : ''}
          type="button"
          onClick={() => onModeChange('text')}
        >
          <MessageCircle size={18} /> Texto
        </button>
        <button
          className={mode === 'voice' ? 'active' : ''}
          type="button"
          onClick={selectVoiceMode}
        >
          <Mic size={18} /> Voz
        </button>
        <button
          className={mode === 'history' ? 'active' : ''}
          type="button"
          onClick={() => onModeChange('history')}
        >
          <Database size={18} /> Historial
        </button>
      </div>
    </div>
  )
}

/** Bootstrap a backend session and coordinate global client preferences. */
export default function App() {
  const [initialSettings] = useState(() => ({
    userId: defaultUserId(),
    apiBaseUrl:
      window.localStorage.getItem('tesseraflow.apiBaseUrl') ??
      import.meta.env.VITE_API_BASE_URL ??
      '',
  }))
  const [userId, setUserId] = useState(initialSettings.userId)
  const [apiBaseUrl, setApiBaseUrl] = useState(initialSettings.apiBaseUrl)
  const [sessionUid, setSessionUid] = useState<string | null>(null)
  const [mode, setMode] = useState<Mode>('text')
  const [creatingSession, setCreatingSession] = useState(true)
  const [sessionError, setSessionError] = useState<string | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)

  const provisionSession = useCallback(
    async (nextBaseUrl = apiBaseUrl, nextUserId = userId) => {
      setCreatingSession(true)
      setSessionError(null)
      try {
        const uid = await createSession(nextBaseUrl, nextUserId)
        setSessionUid(uid)
      } catch (error) {
        setSessionUid(null)
        setSessionError(error instanceof Error ? error.message : 'No se pudo iniciar la sesión.')
      } finally {
        setCreatingSession(false)
      }
    },
    [apiBaseUrl, userId],
  )

  useEffect(() => {
    let active = true
    void createSession(initialSettings.apiBaseUrl, initialSettings.userId)
      .then((uid) => {
        if (active) setSessionUid(uid)
      })
      .catch((error: unknown) => {
        if (!active) return
        setSessionError(
          error instanceof Error ? error.message : 'No se pudo iniciar la sesión.',
        )
      })
      .finally(() => {
        if (active) setCreatingSession(false)
      })
    return () => {
      active = false
    }
  }, [initialSettings])

  /** Persist settings locally and establish a clean session with the new identity. */
  const saveSettings = (nextBaseUrl: string, nextUserId: string) => {
    window.localStorage.setItem('tesseraflow.apiBaseUrl', nextBaseUrl)
    window.localStorage.setItem('tesseraflow.userId', nextUserId)
    setApiBaseUrl(nextBaseUrl)
    setUserId(nextUserId)
    setSettingsOpen(false)
    void provisionSession(nextBaseUrl, nextUserId)
  }

  if (!sessionUid) {
    return (
      <main className="session-gate">
        <div className="gate-card">
          <div className="brand-mark large" aria-hidden="true">T</div>
          <span className="eyebrow">TesseraFlow</span>
          <h1>{creatingSession ? 'Preparando tu conversación' : 'No pudimos conectar'}</h1>
          <p>
            {creatingSession
              ? 'Estamos creando una sesión segura para texto y voz.'
              : sessionError}
          </p>
          {creatingSession ? (
            <div className="gate-loader"><span /><span /><span /></div>
          ) : (
            <div className="gate-actions">
              <button className="button-primary" type="button" onClick={() => void provisionSession()}>
                Reintentar
              </button>
              <button className="button-ghost" type="button" onClick={() => setSettingsOpen(true)}>
                <SlidersHorizontal size={17} /> Configurar
              </button>
            </div>
          )}
        </div>
        {settingsOpen && (
          <SettingsDialog
            apiBaseUrl={apiBaseUrl}
            userId={userId}
            onClose={() => setSettingsOpen(false)}
            onSave={saveSettings}
          />
        )}
      </main>
    )
  }

  return (
    <>
      <Workspace
        key={sessionUid}
        apiBaseUrl={apiBaseUrl}
        sessionUid={sessionUid}
        userId={userId}
        mode={mode}
        onModeChange={setMode}
        onNewSession={() => void provisionSession()}
        onOpenSettings={() => setSettingsOpen(true)}
        creatingSession={creatingSession}
      />
      {settingsOpen && (
        <SettingsDialog
          apiBaseUrl={apiBaseUrl}
          userId={userId}
          onClose={() => setSettingsOpen(false)}
          onSave={saveSettings}
        />
      )}
    </>
  )
}
