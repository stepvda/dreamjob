/**
 * Vocabulary shared by the application review screens.
 *
 * The list, the detail pane and the bulk-approval modal all have to agree on
 * what "fail" means and on which of a package's blockers can be overridden -
 * FR-322 lets the job seeker override a consistency failure with a recorded
 * reason, and NFR-206 lets nobody override a leak. Those two rules are read
 * off `blockers[].overridable`, which the backend computes, and are decoded
 * here once rather than in three places.
 */

import { useState } from 'react'

import { Badge } from '../../components/ui'

export const PACKAGE_STATUS_TONE = {
  draft: undefined,
  approved: 'ok',
  sent: 'info',
  discarded: 'danger',
}

export const PACKAGE_STATUS_LABEL = {
  draft: 'Draft',
  approved: 'Approved for dispatch',
  sent: 'Sent',
  discarded: 'Discarded',
}

/** FR-322 / NFR-206 verdicts, as stored in consistency_status / leak_scan_status. */
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

export function ConsistencyBadge({ status }) {
  const key = status || 'not_run'
  return <Badge tone={CHECK_TONE[key]}>{CONSISTENCY_LABEL[key] || key}</Badge>
}

export function LeakBadge({ status }) {
  const key = status || 'not_run'
  return <Badge tone={CHECK_TONE[key]}>{LEAK_LABEL[key] || key}</Badge>
}

export function isSpeculative(pkg) {
  return pkg?.opportunity_kind === 'speculative'
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
  const company = (pkg?.company_name || 'application').replace(/[/\\]/g, '-')
  return `${company} - ${kind}.${extension}`
}


/**
 * The document itself, in the page (FR-324, FR-331).
 *
 * A reviewer approving an application has to read the words, and the panels
 * could only describe a document and offer it as a download - which is a round
 * trip through the file system to read something already on the server. The
 * frame loads lazily, because a package holds three PDFs and rendering all of
 * them on open would fetch a few hundred kilobytes nobody asked to see.
 *
 * The endpoint serves the file inline; the download buttons still force a save,
 * so nothing about the existing behaviour changes.
 */
export function PdfViewer({ packageId, kind, label = 'document', height = 620 }) {
  // Open by default: the point of the panel is to let a reviewer read the
  // document, and a document behind a button is one they have to go looking
  // for. The toggle is there to fold it away once read, not to reveal it.
  const [open, setOpen] = useState(true)
  if (!packageId || !kind) return null
  const url = `/api/applications/${packageId}/documents/${kind}`
  return (
    <div className="pdf-viewer">
      <div className="pdf-viewer-bar">
        <button className="btn btn-sm" onClick={() => setOpen((v) => !v)}>
          {open ? 'Hide the document' : `Read the ${label}`}
        </button>
        <a className="btn btn-sm btn-ghost" href={url} target="_blank" rel="noreferrer">
          Open in a new tab
        </a>
        <span className="small muted">
          Rendered here from the generated PDF; nothing is downloaded.
        </span>
      </div>
      {open && (
        <iframe
          className="pdf-frame"
          style={{ height }}
          src={url}
          title={`${label} (PDF)`}
        />
      )}
    </div>
  )
}
