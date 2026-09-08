/**
 * Small formatting helpers shared by the five administration tabs.
 *
 * `formatMoney` in components/ui rounds to whole euros, which is right for a
 * salary and wrong here: a single LLM call costs €0.0016 and would render as
 * "€0". Cost on this screen is therefore formatted with enough decimals to
 * stay truthful at both ends of the range.
 */

/** Euro amounts that span four orders of magnitude (FR-361, FR-362). */
export function eur(value) {
  if (value == null || Number.isNaN(Number(value))) return '–'
  const v = Number(value)
  const digits = v === 0 ? 2 : Math.abs(v) < 0.01 ? 4 : Math.abs(v) < 100 ? 2 : 0
  return new Intl.NumberFormat('en-BE', {
    style: 'currency',
    currency: 'EUR',
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(v)
}

/**
 * Thousands separators, and a dash rather than a bare 0 for "unknown".
 * en-GB, not en-BE: the Belgian locale groups with a full stop, so 5084 reads
 * as "5.084" and an English reader sees a decimal.
 */
export function num(value) {
  if (value == null) return '–'
  return new Intl.NumberFormat('en-GB').format(value)
}

/** Compact token counts: 5 084 tokens is fine, 12 400 000 is not. */
export function tokens(value) {
  if (value == null) return '–'
  const v = Number(value)
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(v >= 10_000_000 ? 0 : 1)}M`
  if (v >= 10_000) return `${(v / 1000).toFixed(0)}k`
  return num(v)
}

/**
 * source_catalogue.coverage_countries, .coverage_industries and
 * .query_capabilities, and audit_event.detail, are TEXT columns holding JSON.
 * The repository returns sqlite rows verbatim, so they reach the browser as
 * strings and have to be parsed here rather than read as objects.
 */
export function parseJson(value, fallback = null) {
  if (value == null || value === '') return fallback
  if (typeof value === 'object') return value
  try {
    return JSON.parse(value)
  } catch {
    return fallback
  }
}

/** Date and time — the audit trail and the call log both need the clock. */
export function stamp(iso) {
  if (!iso) return '–'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('en-GB', {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** YYYY-MM-DD in local time, for grouping the usage chart by day. */
export function dayKey(iso) {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(
    d.getDate(),
  ).padStart(2, '0')}`
}
