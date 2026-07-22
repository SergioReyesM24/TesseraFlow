import { ArrowUp, Mic } from 'lucide-react'
import { type KeyboardEvent, useState } from 'react'

interface ComposerProps {
  onSend: (value: string) => boolean
  disabled: boolean
  placeholder: string
  voiceShortcut?: () => void
}

/** Provide a multiline composer with familiar Enter and Shift+Enter behavior. */
export function Composer({
  onSend,
  disabled,
  placeholder,
  voiceShortcut,
}: ComposerProps) {
  const [value, setValue] = useState('')

  /** Submit non-empty text and retain it when the socket is unavailable. */
  const submit = () => {
    if (onSend(value)) setValue('')
  }

  /** Send on Enter while preserving Shift+Enter for a newline. */
  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      submit()
    }
  }

  return (
    <div className="composer-wrap">
      <div className="composer">
        <textarea
          rows={1}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          disabled={disabled}
          aria-label={placeholder}
        />
        {voiceShortcut && (
          <button
            className="composer-icon secondary-action"
            type="button"
            onClick={voiceShortcut}
            aria-label="Cambiar al modo de voz"
          >
            <Mic size={19} />
          </button>
        )}
        <button
          className="composer-icon send-action"
          type="button"
          onClick={submit}
          disabled={disabled || !value.trim()}
          aria-label="Enviar mensaje"
        >
          <ArrowUp size={18} strokeWidth={2.4} />
        </button>
      </div>
      <small>TesseraFlow puede cometer errores. Comprueba la información importante.</small>
    </div>
  )
}
