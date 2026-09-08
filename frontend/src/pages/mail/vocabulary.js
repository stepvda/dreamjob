/**
 * Wording and formatters shared by the mail screens.
 *
 * The mail API answers with the database's own vocabulary - a
 * `delivery_status` of `queued`, a raw Google scope URL - and every one of
 * those strings is shown to a person somewhere on this screen. Translating
 * them in one place is what keeps the send log, the mailbox card and the
 * follow-up list saying the same word for the same thing.
 */

/** dispatch.delivery_status, as written by the dispatcher (FR-326). */
export const DELIVERY_LABEL = {
  queued: 'Queued',
  sent: 'Sent',
  delivered: 'Delivered',
  bounced: 'Bounced',
  failed: 'Failed',
}

export const DELIVERY_TONE = {
  queued: 'info',
  sent: 'accent',
  delivered: 'ok',
  bounced: 'danger',
  failed: 'danger',
}

export function deliveryLabel(status) {
  return DELIVERY_LABEL[status] || status || 'unknown'
}

/**
 * NFR-204 asks that the granted access be shown, not merely stored. Two scopes
 * are ever requested; the third is recognised because a mailbox authorised
 * elsewhere may already carry it.
 */
const SCOPE_LABEL = {
  'https://www.googleapis.com/auth/gmail.send': 'Send mail as you',
  'https://www.googleapis.com/auth/gmail.readonly':
    'Read your mail — this is what finds replies and bounces',
  'https://mail.google.com/': 'Full mailbox access over IMAP — never requested by Dream Job',
}

export function scopeLabel(scope) {
  return SCOPE_LABEL[scope] || scope
}

/** Short name for a scope URL, for the badge in front of the explanation. */
export function scopeName(scope) {
  const tail = String(scope || '').split('/').filter(Boolean).pop() || scope
  return tail === 'mail.google.com' ? 'mail.google.com' : tail
}

/** Date and time. `formatDate` in ui.jsx drops the clock, and here it matters. */
export function formatWhen(iso) {
  if (!iso) return '–'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('en-GB', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function isPast(iso) {
  if (!iso) return false
  const d = new Date(iso)
  return !Number.isNaN(d.getTime()) && d.getTime() < Date.now()
}

/** Whole days between now and an ISO instant; negative when it is overdue. */
export function daysUntil(iso) {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  return Math.round((d.getTime() - Date.now()) / 86400000)
}

/**
 * The webhook address to paste into Resend. The backend reports the path only,
 * because it is the browser that knows which host this installation answers on.
 */
export function webhookUrl(path) {
  if (!path) return ''
  if (typeof window === 'undefined') return path
  return `${window.location.origin}${path}`
}

export function domainOf(address) {
  const at = String(address || '').split('@')
  return at.length === 2 ? at[1] : ''
}

/** Best-effort clipboard write; the caller decides what to say about it. */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}
