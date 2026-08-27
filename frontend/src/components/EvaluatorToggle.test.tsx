import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { EvaluatorToggle } from './EvaluatorToggle'

describe('EvaluatorToggle', () => {
  it('offers to disable active evaluators', () => {
    const markup = renderToStaticMarkup(
      <EvaluatorToggle enabled pending={false} error={null} onToggle={vi.fn()} />,
    )

    expect(markup).toContain('role="switch"')
    expect(markup).toContain('aria-checked="true"')
    expect(markup).toContain('aria-label="Desactivar evaluadores"')
    expect(markup).toContain('Activos')
  })

  it('offers to activate bypassed evaluators', () => {
    const markup = renderToStaticMarkup(
      <EvaluatorToggle enabled={false} pending={false} error={null} onToggle={vi.fn()} />,
    )

    expect(markup).toContain('aria-checked="false"')
    expect(markup).toContain('aria-label="Activar evaluadores"')
    expect(markup).toContain('Inactivos')
  })
})
