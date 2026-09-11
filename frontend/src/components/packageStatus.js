/**
 * The vocabulary both application-package routes share.
 *
 * The Applications screen and the Apply Browser are two views over the same
 * review/approve/send pipeline, and they have to agree on what a row's state
 * means, on which of a package's blockers may be overridden, and on how a
 * refusal is worded. Deciding that once here is what keeps a "fail" on one
 * route from meaning something subtly different on the other.
 *
 * This file is deliberately free of JSX so it can be imported by a page, a
 * panel or a plain helper; the badge components that render these labels live
 * in `components/package/shared.jsx`.
 */

/** The stored package statuses, as the API reports them. */
export const PACKAGE_STATUS_TONE = {
  draft: undefined,
  approved: 'ok',
  sent: 'info',
  discarded: 'danger',
  dry_run: 'warn',
}

export const PACKAGE_STATUS_LABEL = {
  draft: 'Draft',
  approved: 'Approved for dispatch',
  dry_run: 'Assembled, not sent',
  sent: 'Sent',
  discarded: 'Discarded',
}

/** The Apply Browser's finer-grained row states, in the order work moves. */
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
export const CHECK_TONE = { pass: 'ok', fail: 'danger', review: 'warn', not_run: undefined }

export const CONSISTENCY_LABEL = {
  pass: 'Claims supported',
  fail: 'Claims unsupported',
  review: 'Needs your reading',
  not_run: 'Not checked yet',
}

export const LEAK_LABEL = {
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

/** FR-321: the four artefacts, and which of them ever leaves the machine. */
export const PART_LABEL = {
  cv: 'Tailored CV',
  briefing: 'Company and job briefing',
  motivation: 'Motivation and fit',
  email: 'Introduction email',
}

/** preview().never_sent returns column names; these are their human labels. */
export const NEVER_SENT_LABEL = {
  briefing_pdf_path: 'the briefing',
  motivation_pdf_path: 'the motivation document',
}

/** The label a downloaded document is named after. */
export const DOCUMENT_LABEL = {
  cv_pdf: 'CV',
  cv_docx: 'CV',
  briefing: 'briefing',
  motivation: 'motivation',
}

export function isSpeculative(pkg) {
  return (pkg?.opportunity_kind ?? pkg?.kind) === 'speculative'
}

/** NFR-206: a leak, an objection or a missing document cannot be overridden. */
export function hardBlockers(pkg) {
  return (pkg?.blockers || []).filter((b) => !b.overridable)
}

/** FR-322: a consistency failure can be, with a reason the audit log keeps. */
export function softBlockers(pkg) {
  return (pkg?.blockers || []).filter((b) => b.overridable)
}

export function canApprove(pkg) {
  return pkg?.status === 'draft' && hardBlockers(pkg).length === 0
}

/**
 * FR-106, mirrored from `Disclosure.photo_blocked` in the CV generator.
 *
 * The API returns the job seeker's do-not-disclose paths but not whether the
 * generated CV ended up carrying a photograph, so the CV tab reconstructs the
 * same decision the generator made rather than leaving the question open.
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

/** A file name a browser will accept, built from the company it belongs to. */
export function documentName(pkg, kind, extension) {
  const company = (pkg?.company_name || pkg?.company?.name || 'application').replace(/[/\\]/g, '-')
  const label = DOCUMENT_LABEL[kind] || kind
  return `${company} - ${label}.${extension}`
}

/** An ApiError, a plain Error or a string, as one sentence. */
export function errorText(error) {
  if (!error) return ''
  if (typeof error === 'string') return error
  return error.message || 'Something went wrong.'
}

/**
 * Whether a row would be attempted by "Send all".
 *
 * Deliberately *not* the same question as "will it succeed": the send path runs
 * and reports either way, and the FR-325 rails are evaluated on the server at
 * the moment of sending. This decides only what the confirmation modal puts in
 * its table and what it puts in its "these will not go" list, so it checks the
 * four things that make a message possible at all - a human approved it, there
 * is somebody to send it to who has not objected, there is a CV file, and there
 * is a letter. It reads either the Apply Browser's row shape or the flat
 * package preview, because both routes feed the same modal.
 */
export function isSendable(row) {
  const status = row?.package_status ?? row?.status
  const email = row?.contact_email ?? row?.contact?.email
  const objected = row?.contact_objected ?? row?.contact?.objected
  const hasCv = row?.has_cv ?? row?.documents?.cv_pdf
  const hasEmail = row?.has_email ?? Boolean(row?.email_body)
  return (
    status === 'approved' &&
    Boolean(email) &&
    !objected &&
    Boolean(hasCv) &&
    Boolean(hasEmail)
  )
}

/** Why a chosen row was left out, in the words the modal shows. */
export function exclusionReason(row) {
  const status = row?.package_status ?? row?.status
  const email = row?.contact_email ?? row?.contact?.email
  const objected = row?.contact_objected ?? row?.contact?.objected
  const hasCv = row?.has_cv ?? row?.documents?.cv_pdf
  const hasEmail = row?.has_email ?? Boolean(row?.email_body)
  if (status !== 'approved')
    return `the package is ${status || 'not generated'}, and only an approved one is dispatched`
  if (objected) return 'the contact objected, so this address is blocked permanently'
  if (!email) return 'there is no address to write to'
  if (!hasCv) return 'there is no CV file to attach'
  if (!hasEmail) return 'there is no email text'
  return 'it did not meet the conditions for dispatch'
}

/**
 * Normalise either route's row into the shape `SendAllModal` renders.
 *
 * The modal names the recipient, the company, the role and the subject of each
 * message, so every one of those has to be present regardless of which screen
 * the row came from.
 */
export function toSendRow(entity) {
  if (!entity) return entity
  return {
    package_id: entity.package_id ?? entity.id,
    opportunity_id: entity.opportunity_id,
    company_name: entity.company_name ?? entity.company?.name,
    title: entity.title ?? entity.opportunity_title,
    contact_name: entity.contact_name ?? entity.contact?.name,
    contact_email: entity.contact_email ?? entity.contact?.email,
    email_subject: entity.email_subject ?? entity.email?.subject,
    package_status: entity.package_status ?? entity.status,
    contact_objected: entity.contact_objected ?? entity.contact?.objected,
    has_cv: entity.has_cv ?? entity.documents?.cv_pdf,
    has_email: entity.has_email ?? Boolean(entity.email_body),
  }
}
