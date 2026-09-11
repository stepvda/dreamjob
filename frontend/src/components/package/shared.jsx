/**
 * The shared vocabulary and small view helpers of the application-package
 * experience, used by both the Applications screen and the Apply Browser.
 *
 * `components/packageStatus.js` holds the pure labels and rules; this file
 * renders them and re-exports them, so a consumer has one import for both.
 */

import { useState } from 'react'

import { Badge } from '../ui'
import {
  CHECK_TONE,
  CONSISTENCY_LABEL,
  LEAK_LABEL,
  STATE_LABEL,
  STATE_TONE,
} from '../packageStatus'

export * from '../packageStatus'

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

/**
 * A generated PDF, shown rather than described (FR-324, FR-331).
 *
 * The document routes are same-origin, so the browser sends the session cookie
 * with an `<iframe>` request on its own and the file needs no blob juggling in
 * JavaScript. Using the browser's own PDF viewer also means the preview is the
 * real file - the same bytes that would be attached or saved.
 *
 * The frame opens by default: the point of the panel is to let a reviewer read
 * the document, and a document behind a button is one they have to go looking
 * for. The toggle is there to fold it away once read, not to reveal it.
 */
export function PdfViewer({ url, label = 'document', height = 620 }) {
  const [open, setOpen] = useState(true)
  if (!url) return null
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
      {open && <iframe className="pdf-frame" style={{ height }} src={url} title={`${label} (PDF)`} />}
    </div>
  )
}
