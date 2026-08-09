const ISO_DATE_PREFIX = /^(\d{4})-(\d{2})-(\d{2})(.*)$/
const DISPLAY_TIMESTAMP = /^(\d{2})-(\d{2})-(\d{4})T(.*)$/

/** Convert an ISO date prefix to the product-wide DD-MM-YYYY display format. */
export function formatDate(value: string): string {
  const match = ISO_DATE_PREFIX.exec(value)
  if (!match) return value
  const [, year, month, day, suffix] = match
  return `${day}-${month}-${year}${suffix}`
}

/** Format an API timestamp with an exact date order and stable two-digit fields. */
export function formatTimestamp(value: string | null): string {
  if (!value) return '—'
  const displayMatch = DISPLAY_TIMESTAMP.exec(value)
  const parseableValue = displayMatch
    ? `${displayMatch[3]}-${displayMatch[2]}-${displayMatch[1]}T${displayMatch[4]}`
    : value
  const timestamp = new Date(parseableValue)
  if (Number.isNaN(timestamp.getTime())) return formatDate(value)
  const parts = new Intl.DateTimeFormat('es-ES', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).formatToParts(timestamp)
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((item) => item.type === type)?.value ?? ''
  return `${part('day')}-${part('month')}-${part('year')}, ${part('hour')}:${part('minute')}:${part('second')}`
}
