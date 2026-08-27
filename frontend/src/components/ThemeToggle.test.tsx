import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { ThemeToggle } from './ThemeToggle'

describe('ThemeToggle', () => {
  it('exposes light mode as an off switch that changes to dark mode', () => {
    const markup = renderToStaticMarkup(<ThemeToggle value="light" onChange={vi.fn()} />)

    expect(markup).toContain('role="switch"')
    expect(markup).toContain('aria-checked="false"')
    expect(markup).toContain('aria-label="Cambiar a tema oscuro"')
    expect(markup).toContain('Tema claro')
    expect(markup).not.toContain('Sistema')
  })

  it('exposes dark mode as an on switch that changes to light mode', () => {
    const markup = renderToStaticMarkup(<ThemeToggle value="dark" onChange={vi.fn()} />)

    expect(markup).toContain('aria-checked="true"')
    expect(markup).toContain('aria-label="Cambiar a tema claro"')
    expect(markup).toContain('Tema oscuro')
  })
})
