import { useCallback, useEffect, useState } from 'react'
import { loadEvaluatorStatus, updateEvaluatorStatus } from '../lib/api'

interface EvaluatorControlState {
  enabled: boolean | null
  pending: boolean
  error: string | null
  toggle: () => void
}

/** Synchronize the global evaluator switch with the backend runtime. */
export function useEvaluatorControl(apiBaseUrl: string): EvaluatorControlState {
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [pending, setPending] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      setPending(true)
      setError(null)
      try {
        const status = await loadEvaluatorStatus(apiBaseUrl, signal)
        setEnabled(status.enabled)
      } catch (reason) {
        if (signal?.aborted) return
        setError(
          reason instanceof Error ? reason.message : 'No se pudo consultar los evaluadores.',
        )
      } finally {
        if (!signal?.aborted) setPending(false)
      }
    },
    [apiBaseUrl],
  )

  useEffect(() => {
    const controller = new AbortController()
    void loadEvaluatorStatus(apiBaseUrl, controller.signal)
      .then((status) => {
        setEnabled(status.enabled)
        setError(null)
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return
        setError(
          reason instanceof Error ? reason.message : 'No se pudo consultar los evaluadores.',
        )
      })
      .finally(() => {
        if (!controller.signal.aborted) setPending(false)
      })
    return () => controller.abort()
  }, [apiBaseUrl])

  const toggle = useCallback(() => {
    if (pending) return
    if (enabled === null) {
      void refresh()
      return
    }
    setPending(true)
    setError(null)
    void updateEvaluatorStatus(apiBaseUrl, !enabled)
      .then((status) => setEnabled(status.enabled))
      .catch((reason: unknown) => {
        setError(
          reason instanceof Error ? reason.message : 'No se pudo actualizar los evaluadores.',
        )
      })
      .finally(() => setPending(false))
  }, [apiBaseUrl, enabled, pending, refresh])

  return { enabled, pending, error, toggle }
}
