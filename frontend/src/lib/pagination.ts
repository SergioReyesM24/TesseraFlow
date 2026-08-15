export type CompactPaginationItem = number | 'ellipsis-start' | 'ellipsis-end'

/** Keep the first, last and nearby page numbers visible without an unbounded control row. */
export function buildCompactPagination(
  currentPage: number,
  totalPages: number,
): CompactPaginationItem[] {
  if (totalPages < 1) return []
  if (totalPages <= 4) {
    return Array.from({ length: totalPages }, (_, index) => index + 1)
  }
  if (currentPage <= 2) {
    return [1, 2, 3, 'ellipsis-end', totalPages]
  }
  if (currentPage >= totalPages - 1) {
    return [
      1,
      'ellipsis-start',
      totalPages - 2,
      totalPages - 1,
      totalPages,
    ]
  }
  return [
    1,
    'ellipsis-start',
    currentPage,
    currentPage + 1,
    'ellipsis-end',
    totalPages,
  ]
}
