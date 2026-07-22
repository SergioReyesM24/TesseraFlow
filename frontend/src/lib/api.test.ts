import { describe, expect, it } from 'vitest'
import { httpUrl, normalizeBaseUrl } from './api'

describe('API URL composition', () => {
  it('normalizes trailing slashes without altering the origin', () => {
    expect(normalizeBaseUrl(' https://api.example.test/// ')).toBe(
      'https://api.example.test',
    )
  })

  it('keeps relative endpoints when the frontend uses the Vite proxy', () => {
    expect(httpUrl('/v1/sessions', '')).toBe('/v1/sessions')
  })

  it('joins an explicit API origin and endpoint', () => {
    expect(httpUrl('/v1/sessions', 'http://127.0.0.1:8000/')).toBe(
      'http://127.0.0.1:8000/v1/sessions',
    )
  })
})
