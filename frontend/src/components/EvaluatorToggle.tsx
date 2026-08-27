import { ShieldCheck, ShieldOff } from 'lucide-react'

interface EvaluatorToggleProps {
  enabled: boolean | null
  pending: boolean
  error: string | null
  onToggle: () => void
}

/** Present one global control for the interactive and worker evaluators. */
export function EvaluatorToggle({
  enabled,
  pending,
  error,
  onToggle,
}: EvaluatorToggleProps) {
  const active = enabled === true
  const label = error
    ? 'Reintentar evaluadores'
    : pending
      ? 'Consultando evaluadores'
      : active
        ? 'Desactivar evaluadores'
        : 'Activar evaluadores'

  return (
    <button
      className={`evaluator-toggle${active ? ' active' : ''}${error ? ' has-error' : ''}`}
      type="button"
      role="switch"
      aria-checked={active}
      aria-label={label}
      title={error ?? label}
      disabled={pending}
      onClick={onToggle}
    >
      {active ? <ShieldCheck size={18} /> : <ShieldOff size={18} />}
      <span className="evaluator-toggle-copy">
        <strong>Evaluadores</strong>
        <small>{error ? 'Sin conexión' : active ? 'Activos' : 'Inactivos'}</small>
      </span>
      <span className="evaluator-toggle-state" aria-hidden="true">
        {active ? 'ON' : 'OFF'}
      </span>
    </button>
  )
}
