/**
 * The one control that changes what a response actually was (FR-425, NFR-305).
 *
 * This is the correction surface both routes render: the table on
 * "Responses received" and the response sheet opened from the pipeline board
 * use this exact component, so a hand-entered response is corrected the same
 * way wherever the person started from.
 *
 * A detected reply has no hand-entered record to correct, so the control is
 * replaced by the one sentence that says where its reading is revised. What
 * the person states always overrides the model's classification.
 */

import { useState } from 'react'

import { OUTCOMES, normaliseOutcome } from './vocabulary'

export default function CorrectOutcome({ row, modelReading, busy, onCorrect }) {
  const [draft, setDraft] = useState('')

  const stated = normaliseOutcome(row.stated_outcome)
  const model = normaliseOutcome(modelReading?.classification ?? row.classification)
  const effective = stated || model

  if (!row.manual_response_id) {
    return (
      <span className="tiny muted">
        Detected replies are corrected on the pipeline board, not here.
      </span>
    )
  }

  return (
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
  )
}
