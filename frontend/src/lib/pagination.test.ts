import { describe, expect, it } from 'vitest'
import { buildCompactPagination } from './pagination'

describe('compact pagination', () => {
  it('keeps first, last and nearby pages visible in large result sets', () => {
    expect(buildCompactPagination(1, 8_839)).toEqual([
      1,
      2,
      3,
      'ellipsis-end',
      8_839,
    ])
    expect(buildCompactPagination(100, 8_839)).toEqual([
      1,
      'ellipsis-start',
      100,
      101,
      'ellipsis-end',
      8_839,
    ])
  })

  it('shows at most four numeric page buttons', () => {
    expect(buildCompactPagination(2, 4)).toEqual([1, 2, 3, 4])
    expect(buildCompactPagination(8_838, 8_839)).toEqual([
      1,
      'ellipsis-start',
      8_837,
      8_838,
      8_839,
    ])
  })
})
