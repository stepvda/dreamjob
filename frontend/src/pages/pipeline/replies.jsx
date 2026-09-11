/**
 * What came back, and what has been drafted in answer (FR-422, NFR-305).
 *
 * The draft is the reason this file is separate from the rest of the card:
 * FR-422 writes an answer, NFR-305 says a person sends it. So the text is
 * editable here, "Approve" changes a status column and nothing else, and the
 * notice above the buttons says exactly that — an approve button that quietly
 * sent mail would be the single worst bug this product could ship.
 */

import { useEffect, useState } from 'react'

import { Caution, HelpTip } from '../../components/Help'
import { Badge, formatDate, formatPercent } from '../../components/ui'
import { CLASSIFICATION_LABEL, CLASSIFICATION_TONE, DRAFT_TONE } from './shared'

export default function Replies({
  replies,
  drafts,
  busy,
  onProcess,
  onEdit,
  onApprove,
  onDiscard,
  onRespond,
}) {
  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div className="card-header">
        <h3>What came back</h3>
        <HelpTip term="reply_classification" />
        <div className="spacer" />
        <button className="btn btn-sm" onClick={onRespond}>
          Record or correct a response
        </button>
      </div>

      {replies.length === 0 && (
        <p className="small muted" style={{ margin: 0 }}>
          Nothing received against this application yet. Replies to a connected mailbox are
          detected automatically; anything else — a phone call, a portal, a LinkedIn message —
          you record on the Responses screen.
        </p>
      )}

      {replies.map((reply) => {
        const own = drafts.filter((d) => d.incoming_reply_id === reply.id)
        return (
          <div key={reply.id} style={{ marginBottom: 16 }}>
            <div className="row row-wrap" style={{ marginBottom: 6 }}>
              <strong className="small">{reply.subject || '(no subject)'}</strong>
              <span className="small muted">{reply.from_address}</span>
              <span className="small muted">{formatDate(reply.received_at || reply.created_at)}</span>
              <div className="spacer" />
              {reply.classification ? (
                <Badge tone={CLASSIFICATION_TONE[reply.classification]}>
                  {CLASSIFICATION_LABEL[reply.classification] || reply.classification}
                </Badge>
              ) : (
                <Badge tone="warn">Not classified</Badge>
              )}
              {reply.classification_confidence != null && (
                <span className="tiny muted">
                  confidence {formatPercent(reply.classification_confidence, 0)}
                </span>
              )}
            </div>

            {reply.body && (
              <pre
                className="small"
                style={{
                  whiteSpace: 'pre-wrap',
                  background: 'var(--surface-sunk)',
                  padding: 10,
                  borderRadius: 'var(--radius-sm)',
                  margin: '0 0 8px',
                  maxHeight: 200,
                  overflowY: 'auto',
                }}
              >
                {reply.body}
              </pre>
            )}

            {reply.extracted_slots?.phrases?.length > 0 && (
              <p className="tiny muted" style={{ margin: '0 0 8px' }}>
                Times read out of the message: {reply.extracted_slots.phrases.join(' · ')}
              </p>
            )}

            <button
              className="btn btn-sm"
              disabled={busy === `reply:${reply.id}`}
              onClick={() => onProcess(reply.id)}
            >
              {busy === `reply:${reply.id}` ? (
                <span className="spinner" />
              ) : reply.classification ? (
                'Classify again and redraft'
              ) : (
                'Classify and draft an answer'
              )}
            </button>

            {own.map((draft) => (
              <DraftEditor
                key={draft.id}
                draft={draft}
                busy={busy === `draft:${draft.id}`}
                onEdit={onEdit}
                onApprove={onApprove}
                onDiscard={onDiscard}
              />
            ))}
          </div>
        )
      })}
    </div>
  )
}

/**
 * NFR-305 in one component: the text is yours to change, "Approve" only marks
 * it approved, and the notice above the buttons says what approval does and
 * does not do.
 */
function DraftEditor({ draft, busy, onEdit, onApprove, onDiscard }) {
  const [subject, setSubject] = useState(draft.subject || '')
  const [body, setBody] = useState(draft.body || '')

  useEffect(() => {
    setSubject(draft.subject || '')
    setBody(draft.body || '')
  }, [draft.id, draft.subject, draft.body])

  const sent = draft.status === 'sent' || Boolean(draft.sent_at)
  const dirty = body !== (draft.body || '') || subject !== (draft.subject || '')
  // The API refuses an empty body (min_length=1); say so here rather than
  // letting the user find out through a 422.
  const empty = !body.trim()

  return (
    <div className="card" style={{ marginTop: 10 }}>
      <div className="card-header">
        <h3>Drafted answer</h3>
        <HelpTip term="reply_draft" />
        <Badge tone={DRAFT_TONE[draft.status]}>{draft.status}</Badge>
        {draft.generated_by && <span className="tiny muted">written by {draft.generated_by}</span>}
        <div className="spacer" />
        <span className="tiny muted">{draft.language}</span>
      </div>

      <Caution title="Approving is not sending">
        Approval marks this text ready. It is queued for the mail screen, which sends from your
        own mailbox when you tell it to — nothing leaves on the strength of this button
        (NFR-305).
      </Caution>

      <div className="field" style={{ marginTop: 12 }}>
        <label>Subject</label>
        <input type="text" value={subject} onChange={(e) => setSubject(e.target.value)} disabled={sent} />
      </div>
      <div className="field">
        <label>Body</label>
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          style={{ minHeight: 170 }}
          disabled={sent}
        />
        <span className="hint">To: {draft.to_address || 'the address that wrote to you'}</span>
      </div>

      {sent ? (
        <p className="small muted" style={{ margin: 0 }}>
          Sent {formatDate(draft.sent_at)}. A sent draft cannot be re-approved.
        </p>
      ) : (
        <div className="row row-wrap">
          <button
            className="btn btn-sm"
            disabled={!dirty || empty || busy}
            onClick={() => onEdit(draft.id, { body, subject })}
          >
            Save my edits
          </button>
          <button
            className="btn btn-sm btn-primary"
            disabled={empty || busy}
            onClick={() => onApprove(draft.id, dirty ? { body, subject } : {})}
          >
            {busy ? <span className="spinner" /> : 'Approve for sending'}
          </button>
          <button className="btn btn-sm btn-ghost" disabled={busy} onClick={() => onDiscard(draft.id)}>
            Discard
          </button>
          {draft.approved_at && (
            <span className="tiny muted">approved {formatDate(draft.approved_at)}</span>
          )}
        </div>
      )}
    </div>
  )
}
