import type { ConnectionState } from '../types'

interface StatusPillProps {
  state: ConnectionState
  label?: string
}

const statusLabels: Record<ConnectionState, string> = {
  connecting: 'Conectando',
  connected: 'En línea',
  disconnected: 'Sin conexión',
  error: 'Error',
}

/** Show socket health without exposing transport implementation details. */
export function StatusPill({ state, label }: StatusPillProps) {
  return (
    <span className={`status-pill status-${state}`}>
      <span className="status-dot" aria-hidden="true" />
      {label ?? statusLabels[state]}
    </span>
  )
}
