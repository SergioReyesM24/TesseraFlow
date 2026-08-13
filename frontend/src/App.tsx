import { SlidersHorizontal } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { SettingsDialog, type ThemeMode, Workspace } from './components/Workspace'
import { createSession } from './lib/api'
import type { Mode } from './types'

interface InitialSettings {
  userId: string
  apiBaseUrl: string
}

/** Create a device-local anonymous owner identifier for the unauthenticated demo API. */
function defaultUserId(): string {
  const stored = window.localStorage.getItem('tesseraflow.userId')
  if (stored) return stored
  const generated = `web-${crypto.randomUUID().slice(0, 8)}`
  window.localStorage.setItem('tesseraflow.userId', generated)
  return generated
}

/** Read a trusted theme preference while tolerating older localStorage values. */
function defaultThemeMode(): ThemeMode {
  const stored = window.localStorage.getItem('tesseraflow.themeMode')
  return stored === 'light' || stored === 'dark' || stored === 'system' ? stored : 'system'
}

/** Read stable browser settings once for the lifetime of the application. */
function initialSettings(): InitialSettings {
  return {
    userId: defaultUserId(),
    apiBaseUrl:
      window.localStorage.getItem('tesseraflow.apiBaseUrl') ??
      import.meta.env.VITE_API_BASE_URL ??
      '',
  }
}

/** Bootstrap a backend session and coordinate global client preferences. */
export default function App() {
  const [settings] = useState(initialSettings)
  const [userId, setUserId] = useState(settings.userId)
  const [apiBaseUrl, setApiBaseUrl] = useState(settings.apiBaseUrl)
  const [sessionUid, setSessionUid] = useState<string | null>(null)
  const [mode, setMode] = useState<Mode>('text')
  const [themeMode, setThemeMode] = useState<ThemeMode>(defaultThemeMode)
  const [creatingSession, setCreatingSession] = useState(true)
  const [sessionError, setSessionError] = useState<string | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const latestProvision = useRef(0)

  const provisionSession = useCallback(
    async (nextBaseUrl: string, nextUserId: string) => {
      const requestId = latestProvision.current + 1
      latestProvision.current = requestId
      setCreatingSession(true)
      setSessionError(null)
      try {
        const uid = await createSession(nextBaseUrl, nextUserId)
        if (latestProvision.current === requestId) setSessionUid(uid)
      } catch (error) {
        if (latestProvision.current !== requestId) return
        setSessionUid(null)
        setSessionError(error instanceof Error ? error.message : 'No se pudo iniciar la sesión.')
      } finally {
        if (latestProvision.current === requestId) setCreatingSession(false)
      }
    },
    [],
  )

  useEffect(() => {
    const requestId = latestProvision.current + 1
    latestProvision.current = requestId
    void createSession(settings.apiBaseUrl, settings.userId)
      .then((uid) => {
        if (latestProvision.current === requestId) setSessionUid(uid)
      })
      .catch((error: unknown) => {
        if (latestProvision.current !== requestId) return
        setSessionError(
          error instanceof Error ? error.message : 'No se pudo iniciar la sesión.',
        )
      })
      .finally(() => {
        if (latestProvision.current === requestId) setCreatingSession(false)
      })
    return () => {
      latestProvision.current += 1
    }
  }, [settings])

  useEffect(() => {
    window.localStorage.setItem('tesseraflow.themeMode', themeMode)
    if (themeMode === 'system') {
      delete document.documentElement.dataset.theme
      return
    }
    document.documentElement.dataset.theme = themeMode
  }, [themeMode])

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
              <button
                className="button-primary"
                type="button"
                onClick={() => void provisionSession(apiBaseUrl, userId)}
              >
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
        themeMode={themeMode}
        onModeChange={setMode}
        onThemeModeChange={setThemeMode}
        onNewSession={() => void provisionSession(apiBaseUrl, userId)}
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
