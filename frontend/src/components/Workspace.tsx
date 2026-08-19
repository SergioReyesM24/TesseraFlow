import {
  AudioLines,
  ChevronDown,
  Database,
  MessageCircle,
  Mic,
  Monitor,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Settings,
  Square,
  Sun,
  Volume2,
  X,
} from 'lucide-react'
import {
  type FormEvent,
  type ReactNode,
  useEffect,
  useRef,
  useState,
} from 'react'
import { useAgentSocket } from '../hooks/useAgentSocket'
import { useRealtimeSocket } from '../hooks/useRealtimeSocket'
import { collectDockedVisuals } from '../lib/visuals'
import type { Mode } from '../types'
import { BrandMark } from './BrandMark'
import { Composer } from './Composer'
import { ConversationHistory } from './ConversationHistory'
import { MessageList } from './MessageList'
import { StatusPill } from './StatusPill'
import { VisualInspector } from './VisualInspector'

export type ThemeMode = 'system' | 'light' | 'dark'

interface WorkspaceProps {
  apiBaseUrl: string
  sessionUid: string
  userId: string
  mode: Mode
  themeMode: ThemeMode
  onModeChange: (mode: Mode) => void
  onThemeModeChange: (themeMode: ThemeMode) => void
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

/** Small segmented control for the two explicit themes plus the OS preference. */
function ThemeSwitch({
  value,
  onChange,
}: {
  value: ThemeMode
  onChange: (themeMode: ThemeMode) => void
}) {
  const options: Array<{ value: ThemeMode; label: string; icon: ReactNode }> = [
    { value: 'system', label: 'Sistema', icon: <Monitor size={15} /> },
    { value: 'light', label: 'Claro', icon: <Sun size={15} /> },
    { value: 'dark', label: 'Oscuro', icon: <Moon size={15} /> },
  ]

  return (
    <div className="theme-switch" aria-label="Tema visual">
      {options.map((option) => (
        <button
          className={value === option.value ? 'active' : ''}
          type="button"
          key={option.value}
          onClick={() => onChange(option.value)}
          aria-pressed={value === option.value}
          title={`Tema ${option.label.toLowerCase()}`}
        >
          {option.icon}
          <span>{option.label}</span>
        </button>
      ))}
    </div>
  )
}
interface ModeSelectorProps {
  mode: Mode
  onSelect: (mode: Mode) => void
}
const modeOptions: Array<{
  value: Mode
  label: string
  description: string
  icon: ReactNode
}> = [
  {
    value: 'text',
    label: 'Texto',
    description: 'Chat WebSocket persistente',
    icon: <MessageCircle size={17} />,
  },
  {
    value: 'voice',
    label: 'Realtime',
    description: 'Audio bidireccional',
    icon: <AudioLines size={17} />,
  },
  {
    value: 'history',
    label: 'Historial',
    description: 'Costes, sesiones y registros',
    icon: <Database size={17} />,
  },
]

/** Switch between the main app views from the topbar without duplicating mode state. */
function ModeSelector({ mode, onSelect }: ModeSelectorProps) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement | null>(null)
  const activeOption = modeOptions.find((option) => option.value === mode) ?? modeOptions[0]

  useEffect(() => {
    if (!open) return

    const closeFromOutside = (event: PointerEvent) => {
      if (rootRef.current?.contains(event.target as Node)) return
      setOpen(false)
    }
    const closeFromKeyboard = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }

    document.addEventListener('pointerdown', closeFromOutside)
    document.addEventListener('keydown', closeFromKeyboard)
    return () => {
      document.removeEventListener('pointerdown', closeFromOutside)
      document.removeEventListener('keydown', closeFromKeyboard)
    }
  }, [open])

  const selectMode = (nextMode: Mode) => {
    onSelect(nextMode)
    setOpen(false)
  }

  return (
    <div className="mode-selector-dropdown" ref={rootRef}>
      <button
        className={`model-selector${open ? ' open' : ''}`}
        type="button"
        aria-label="Cambiar vista"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        TesseraFlow <span>{activeOption.label}</span>
        <ChevronDown size={15} />
      </button>
      {open && (
        <div className="mode-selector-menu" role="menu" aria-label="Vistas de TesseraFlow">
          {modeOptions.map((option) => (
            <button
              className={option.value === mode ? 'active' : ''}
              type="button"
              role="menuitemradio"
              aria-checked={option.value === mode}
              key={option.value}
              onClick={() => selectMode(option.value)}
            >
              <span className="mode-selector-menu-icon" aria-hidden="true">
                {option.icon}
              </span>
              <span>
                <strong>{option.label}</strong>
                <small>{option.description}</small>
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
/** Edit connection settings and start a fresh owned session after saving. */
export function SettingsDialog({
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
        onKeyDown={(event) => {
          if (event.key === 'Escape') onClose()
        }}
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
              autoFocus
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

/** Hold both conversation transports so switching modes preserves visible transcripts. */
export function Workspace({
  apiBaseUrl,
  sessionUid,
  userId,
  mode,
  themeMode,
  onModeChange,
  onThemeModeChange,
  onNewSession,
  onOpenSettings,
  creatingSession,
}: WorkspaceProps) {
  const [navigationExpanded, setNavigationExpanded] = useState(() => {
    return window.localStorage.getItem('tesseraflow.navigationExpanded') !== 'false'
  })
  const [dismissedVisualGroupKey, setDismissedVisualGroupKey] = useState<string | null>(null)
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
  const visualCollection = collectDockedVisuals(activeMessages)
  const activeVisuals =
    visualCollection && visualCollection.key !== dismissedVisualGroupKey
      ? visualCollection.presentations
      : null

  /** Enter voice mode while the click still grants browser audio permission. */
  const selectVoiceMode = () => {
    void voice.activateAudio().catch(() => undefined)
    onModeChange('voice')
  }

  /** Return a dismissed visual to its original inline message position. */
  const closeVisual = () => {
    if (!activeVisuals || !visualCollection) return
    setDismissedVisualGroupKey(visualCollection.key)
  }

  /** Keep the side navigation preference stable across reloads. */
  const toggleNavigation = () => {
    setNavigationExpanded((expanded) => {
      const nextExpanded = !expanded
      window.localStorage.setItem('tesseraflow.navigationExpanded', String(nextExpanded))
      return nextExpanded
    })
  }

  return (
    <div
      className={`app-shell${activeVisuals ? ' has-visual' : ''}${
        navigationExpanded ? ' sidebar-expanded' : ' sidebar-collapsed'
      }`}
    >
      <aside className="sidebar">
        <div className="brand-row">
          <BrandMark />
          <div className="brand-copy">
            <strong>TesseraFlow</strong>
            <span>Agente multimodal</span>
          </div>
        </div>

        <button
          className="new-chat-button"
          type="button"
          onClick={onNewSession}
          disabled={creatingSession}
        >
          <Plus size={18} />
          <span>Nueva conversación</span>
        </button>

        <nav className="mode-nav" aria-label="Modos de conversación">
          <span className="nav-caption">Conversar con</span>
          <button
            className={mode === 'text' ? 'active' : ''}
            type="button"
            onClick={() => onModeChange('text')}
            title="Chat de texto"
            aria-current={mode === 'text' ? 'page' : undefined}
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
            title="Voz realtime"
            aria-current={mode === 'voice' ? 'page' : undefined}
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
            title="Historial técnico"
            aria-current={mode === 'history' ? 'page' : undefined}
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
        <button
          className="sidebar-collapse-toggle"
          type="button"
          aria-label={navigationExpanded ? 'Contraer navegación' : 'Desplegar navegación'}
          aria-expanded={navigationExpanded}
          title={navigationExpanded ? 'Contraer navegación' : 'Desplegar navegación'}
          onClick={toggleNavigation}
        >
          {navigationExpanded ? <PanelLeftClose size={18} /> : <PanelLeftOpen size={18} />}
          <span>{navigationExpanded ? 'Contraer' : 'Menú'}</span>
        </button>
        <button className="settings-button" type="button" onClick={onOpenSettings}>
          <Settings size={18} />
          <span>Configuración</span>
        </button>
      </aside>

      <main className="main-panel">
        <header className="topbar">
          <div className="mobile-brand">
            <BrandMark />
          </div>
          <ModeSelector
            mode={mode}
            onSelect={(nextMode) => {
              if (nextMode === 'voice') selectVoiceMode()
              else onModeChange(nextMode)
            }}
          />
          <div className="topbar-actions">
            <ThemeSwitch value={themeMode} onChange={onThemeModeChange} />
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
          </div>
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
                showVisuals={!activeVisuals}
              >
                <div className="welcome-mark" aria-hidden="true">
                  <MessageCircle size={20} />
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
                emptyTitle=""
                emptyDescription=""
                showVisuals={!activeVisuals}
              />

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

      {activeVisuals && (
        <VisualInspector
          key={visualCollection?.key}
          presentations={activeVisuals}
          initialActiveIndex={visualCollection?.activeIndex}
          onClose={closeVisual}
        />
      )}

      <div className="mobile-mode-switch" aria-label="Cambiar modo">
        <button
          className={mode === 'text' ? 'active' : ''}
          type="button"
          onClick={() => onModeChange('text')}
          aria-current={mode === 'text' ? 'page' : undefined}
        >
          <MessageCircle size={18} /> Texto
        </button>
        <button
          className={mode === 'voice' ? 'active' : ''}
          type="button"
          onClick={selectVoiceMode}
          aria-current={mode === 'voice' ? 'page' : undefined}
        >
          <Mic size={18} /> Voz
        </button>
        <button
          className={mode === 'history' ? 'active' : ''}
          type="button"
          onClick={() => onModeChange('history')}
          aria-current={mode === 'history' ? 'page' : undefined}
        >
          <Database size={18} /> Historial
        </button>
      </div>
    </div>
  )
}
