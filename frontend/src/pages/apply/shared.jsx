/**
 * Vocabulary shared by the Apply Browser's parts.
 *
 * The list, the detail pane, the confirmation modal and the send report all
 * have to agree on what a row's state means, on which of a package's blockers
 * can be overridden, and on how a refusal is worded. Deciding that once here
 * is what keeps a "fail" on the left of the screen from meaning something
 * subtly different on the right.
 *
 * Two of the rules are requirements rather than presentation choices, so they
 * are read off what the backend computed rather than re-derived:
 * `blockers[].overridable` (FR-322 lets a consistency failure be overridden
 * with a recorded reason; NFR-206 lets nobody override a leak), and the
 * `never_sent` marking on a document (FR-321).
 */

import { Badge } from '../../components/ui'

/** The states the backend reports for a row, in the order work moves through. */
export const STATE_LABEL = {
  not_generated: 'Nothing generated',
  draft: 'Draft',
  approved: 'Approved',
  dry_run: 'Assembled, not sent',
  sent: 'Sent',
  discarded: 'Discarded',
}

export const STATE_TONE = {
  not_generated: undefined,
  draft: undefined,
  approved: 'ok',
  dry_run: 'warn',
  sent: 'info',
  discarded: 'danger',
}

/** FR-322 / NFR-206 verdicts, as stored on the package. */
const CHECK_TONE = { pass: 'ok', fail: 'danger', review: 'warn', not_run: undefined }

const CONSISTENCY_LABEL = {
  pass: 'Claims supported',
  fail: 'Claims unsupported',
  review: 'Needs your reading',
  not_run: 'Not checked yet',
}

const LEAK_LABEL = {
  pass: 'Nothing unattributable',
  fail: 'Unattributable content',
  review: 'Needs your reading',
  not_run: 'Not scanned yet',
}

export const SEVERITY_TONE = { high: 'danger', medium: 'warn', low: 'info' }

/** NFR-501: the content languages the document generators actually support. */
export const LANGUAGES = [
  { value: 'en', label: 'English' },
  { value: 'nl', label: 'Nederlands' },
  { value: 'fr', label: 'Français' },
  { value: 'de', label: 'Deutsch' },
]

export function StateBadge({ state }) {
  return <Badge tone={STATE_TONE[state]}>{STATE_LABEL[state] || state}</Badge>
}

export function ConsistencyBadge({ status }) {
  const key = status || 'not_run'
  return <Badge tone={CHECK_TONE[key]}>{CONSISTENCY_LABEL[key] || key}</Badge>
}

export function LeakBadge({ status }) {
  const key = status || 'not_run'
  return <Badge tone={CHECK_TONE[key]}>{LEAK_LABEL[key] || key}</Badge>
}

/** NFR-206, NFR-302: a leak, an objection or a missing file cannot be waived. */
export function hardBlockers(pkg) {
  return (pkg?.blockers || []).filter((b) => !b.overridable)
}

/** FR-322: a consistency failure can be, with a reason the audit trail keeps. */
export function softBlockers(pkg) {
  return (pkg?.blockers || []).filter((b) => b.overridable)
}

/**
 * Whether this list row would be attempted by "Send all".
 *
 * Deliberately *not* the same question as "will it succeed": the send path
 * runs and reports either way, and the FR-325 rails are evaluated on the
 * server at the moment of sending, not here. This decides only what the
 * confirmation modal puts in its table and what it puts in its "these will not
 * go" list, so it checks the four things that make a message possible at all —
 * a human approved it, there is somebody to send it to who has not objected,
 * there is a CV file, and there is a letter.
 */
export function isSendable(row) {
  return (
    row?.package_status === 'approved' &&
    !!row?.contact_email &&
    !row?.contact_objected &&
    !!row?.has_cv &&
    !!row?.has_email
  )
}

/** Why a chosen row was left out, in the words the modal shows. */
export function exclusionReason(row) {
  if (row?.package_status !== 'approved')
    return `the package is ${row?.package_status || 'not generated'}, and only an approved one is dispatched`
  if (row?.contact_objected) return 'the contact objected, so this address is blocked permanently'
  if (!row?.contact_email) return 'there is no address to write to'
  if (!row?.has_cv) return 'there is no CV file to attach'
  if (!row?.has_email) return 'there is no email text'
  return 'it did not meet the conditions for dispatch'
}

/** A file name a browser will accept, built from the company it belongs to. */
export function documentName(row, kind, extension) {
  const name = row?.company_name || row?.company?.name || 'application'
  return `${String(name).replace(/[/\\]/g, '-')} - ${kind}.${extension}`
}

/** An ApiError, a plain Error or a string, as one sentence. */
export function errorText(error) {
  if (!error) return ''
  if (typeof error === 'string') return error
  return error.message || 'Something went wrong.'
}

/**
 * FR-106, mirrored from `Disclosure.photo_blocked` in the CV generator.
 *
 * The API returns the job seeker's do-not-disclose paths but not whether the
 * generated CV ended up carrying a photograph, so the CV tab reconstructs the
 * same decision the generator made rather than leaving the question open. A
 * photograph on a CV is the field people most often mean to suppress and are
 * most alarmed to find.
 */
const PHOTO_KEYS = ['photo', 'photo_path', 'contact.photo', 'profile.photo', 'picture']

export function photoBlocked(paths) {
  const blocked = (paths || [])
    .map((p) =>
      String(p || '')
        .trim()
        .toLowerCase()
        .replace(/^\/+/, '')
        .replace(/\//g, '.')
        .replace(/^sections\./, ''),
    )
    .filter(Boolean)
  return PHOTO_KEYS.some((key) =>
    blocked.some((b) => key === b || key.startsWith(`${b}.`) || key.startsWith(`${b}[`)),
  )
}
