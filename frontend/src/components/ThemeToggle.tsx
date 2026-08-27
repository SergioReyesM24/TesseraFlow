import { Moon, Sun } from 'lucide-react'

export type ThemeMode = 'light' | 'dark'

interface ThemeToggleProps {
  value: ThemeMode
  onChange: (themeMode: ThemeMode) => void
}

/** Toggle between the two explicit visual themes from the side navigation. */
export function ThemeToggle({ value, onChange }: ThemeToggleProps) {
  const dark = value === 'dark'
  const nextTheme = dark ? 'light' : 'dark'
  const accessibleLabel = dark ? 'Cambiar a tema claro' : 'Cambiar a tema oscuro'

  return (
    <button
      className={`theme-toggle${dark ? ' is-dark' : ''}`}
      type="button"
      role="switch"
      aria-checked={dark}
      aria-label={accessibleLabel}
      title={accessibleLabel}
      onClick={() => onChange(nextTheme)}
    >
      <span className="theme-toggle-label">
        {dark ? <Moon size={17} /> : <Sun size={17} />}
        <span>{dark ? 'Tema oscuro' : 'Tema claro'}</span>
      </span>
      <span className="theme-toggle-track" aria-hidden="true">
        <span className="theme-toggle-knob" />
        <Sun className="theme-toggle-sun" size={13} />
        <Moon className="theme-toggle-moon" size={13} />
      </span>
    </button>
  )
}
