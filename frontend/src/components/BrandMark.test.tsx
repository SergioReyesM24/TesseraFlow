import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { BrandMark } from './BrandMark'

describe('BrandMark', () => {
  it('renders the shared decorative logo asset', () => {
    const markup = renderToStaticMarkup(<BrandMark large />)

    expect(markup).toContain('src="/tesseraflow-mark.svg"')
    expect(markup).toContain('class="brand-mark large"')
    expect(markup).toContain('aria-hidden="true"')
    expect(markup).toContain('alt=""')
  })
})
