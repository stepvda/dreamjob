/**
 * One row of "what has come back" (FR-422, FR-425, NFR-305).
 *
 * Where the model's reading of a reply differs from what the person stated,
 * both are on the row and the correction is the visible control beside them
 * (PATCH /api/learning/responses/{manual_response_id}). A misread rejection is
 * not a cosmetic problem: it is counted in every rate on /insights.
 */

import { useState } from 'react'

import { HelpTip } from '../../components/Help'
import { Badge, KindBadge, formatDate } from '../../components/ui'
import { OUTCOMES, channelLabel, normaliseOutcome, outcomeLabel } from './vocabulary'

/**
 * One recorded response. Where the model read the reply differently from what
 * the person stated, both readings are on the row and the correction is the
 * visible control next to them (PATCH /learning/responses/{manual_response_id}).
 */
export default function ResponseRow({ row, modelReading, onCorrect, onAnother, busy }) {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState('')

  const stated = normaliseOutcome(row.stated_outcome)
  const model = normaliseOutcome(modelReading?.classification ?? row.classification)
  const differs = Boolean(stated && model && stated !== model)
  const effective = stated || model
  const detected = !row.manual_response_id
  const confidence = modelReading?.confidence ?? row.classification_confidence

  return (
    <>
      <tr>
        <td className="nowrap">
          <div>{formatDate(row.received_at || row.created_at)}</div>
          {detected ? (
            <span className="tiny muted">
              detected
              <HelpTip term="detected_reply" />
            </span>
          ) : (
            <span className="tiny muted">{channelLabel(row.channel)}</span>
          )}
        </td>

        <td>
          <div className="row row-wrap" style={{ gap: 6 }}>
            <strong>{row.company_name || row.from_address || 'Unknown company'}</strong>
            {row.opportunity_kind && <KindBadge kind={row.opportunity_kind} />}
          </div>
          <div className="small muted">{row.opportunity_title || row.subject || '–'}</div>
          {(row.body || row.notes) && (
            <button className="btn btn-sm btn-ghost" onClick={() => setOpen((v) => !v)}>
              {open ? 'Hide the text' : 'Read the text'}
            </button>
          )}
        </td>

        <td>
          <div className="row row-wrap" style={{ gap: 6 }}>
            <Badge>{outcomeLabel(effective) || 'not classified'}</Badge>
            {stated && <span className="tiny muted">your reading</span>}
          </div>
          {differs && (
            <div className="small" style={{ marginTop: 4 }}>
              {/* NFR-305: the model's reading is shown, never substituted. */}
              The model read this as <strong>{outcomeLabel(model)}</strong>
              {confidence != null && ` · ${Math.round(confidence * 100)}% confident`}
              {modelReading?.reason ? ` — ${modelReading.reason}` : ''}
            </div>
          )}
          {!differs && confidence != null && !stated && (
            <div className="tiny muted" style={{ marginTop: 2 }}>
              {Math.round(confidence * 100)}% confident
              <HelpTip term="classification_confidence" />
            </div>
          )}
        </td>

        <td>
          {row.manual_response_id ? (
            <div className="row" style={{ gap: 6 }}>
              <select
                value={draft || effective || ''}
                onChange={(e) => setDraft(e.target.value)}
                aria-label="What this response actually was"
                style={{ minWidth: 170 }}
              >
                <option value="">Choose…</option>
                {OUTCOMES.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
              <button
                className="btn btn-sm"
                disabled={busy || !draft || draft === effective}
                onClick={() => onCorrect(row, draft)}
              >
                {busy ? <span className="spinner" /> : 'Correct'}
              </button>
            </div>
          ) : (
            <span className="tiny muted">
              Detected replies are corrected on the pipeline board, not here.
            </span>
          )}
          <div style={{ marginTop: 6 }}>
            <button className="btn btn-sm btn-ghost" onClick={() => onAnother(row)}>
              Another response to this one
            </button>
          </div>
        </td>
      </tr>

      {open && (
        <tr>
          <td colSpan={4} style={{ background: 'var(--surface-2)' }}>
            {row.subject && (
              <div className="small" style={{ marginBottom: 6 }}>
                <strong>{row.subject}</strong>
              </div>
            )}
            {/* Untrusted outside text (NFR-205). Rendered as text, never markup. */}
            <div className="small" style={{ whiteSpace: 'pre-wrap', lineHeight: 1.55 }}>
              {row.body || <span className="muted">No text was kept for this one.</span>}
            </div>
            {row.notes && (
              <div className="small muted" style={{ marginTop: 8 }}>
                Your note: {row.notes}
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  )
}
