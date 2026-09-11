/**
 * One card, opened (FR-421).
 *
 * Everything about a single application that does not fit on the board face:
 * when it entered each stage, what you decided to do next, your notes, and how
 * the card came to be where it is — which reply or which drag moved it, and
 * on what date.
 *
 * The replies and their drafted answers are large enough to live in their own
 * file; everything else about a card is here.
 */

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import {
  Badge,
  ErrorBox,
  KindBadge,
  Loading,
  Modal,
  formatDate,
  useFetch,
} from '../../components/ui'
import Replies from './replies'
import {
  NEGOTIABLE_STAGES,
  OUTCOME_TONE,
  STAGES,
  STAGE_LABEL,
  TRIGGER_LABEL,
  fromDateInput,
  isOverdue,
  toDateInput,
} from './shared'

export default function CardDetail({
  cardId,
  onClose,
  onChanged,
  onRespond,
  onRehearse,
  onNegotiate,
}) {
  const detail = useFetch(() => api.get(`/pipeline/cards/${cardId}`), [cardId])
  const [busy, setBusy] = useState(null)
  const [actionError, setActionError] = useState(null)

  const card = detail.data?.card
  const replies = detail.data?.replies || []
  const drafts = detail.data?.drafts || []
  const events = detail.data?.events || []

  async function run(key, fn) {
    setBusy(key)
    setActionError(null)
    try {
      await fn()
      detail.reload()
      onChanged?.()
    } catch (err) {
      setActionError(err)
    } finally {
      setBusy(null)
    }
  }

  return (
    <Modal
      wide
      title={card ? card.opportunity_title || 'Application' : 'Application'}
      onClose={onClose}
      actions={
        <>
          <button className="btn" disabled={!card} onClick={() => onRespond?.(card)}>
            Record or correct a response
          </button>
          <button className="btn btn-ghost" onClick={onClose}>
            Close
          </button>
        </>
      }
    >
      {detail.loading && <Loading rows={4} />}
      {detail.error && <ErrorBox error={detail.error} onRetry={detail.reload} />}
      {actionError && <ErrorBox error={actionError} />}

      {card && (
        <>
          <div className="row row-wrap" style={{ marginBottom: 14 }}>
            <strong>{card.company_name || 'Unknown company'}</strong>
            {card.opportunity_kind && <KindBadge kind={card.opportunity_kind} />}
            <Badge tone="accent">{STAGE_LABEL[card.stage] || card.stage}</Badge>
            {card.outcome && (
              <Badge tone={OUTCOME_TONE[card.outcome]}>{card.outcome.replace('_', ' ')}</Badge>
            )}
            {card.delivery_status && (
              <span className="small muted">Delivery: {card.delivery_status}</span>
            )}
            <div className="spacer" />
            {card.opportunity_id && (
              <Link className="btn btn-sm btn-ghost" to={`/opportunities/${card.opportunity_id}`}>
                The opportunity
              </Link>
            )}
          </div>

          <StageDates card={card} />

          <NextAction
            card={card}
            busy={busy === 'next'}
            onSave={(values) =>
              run('next', () => api.patch(`/pipeline/cards/${card.id}`, values))
            }
          />

          <Notes
            card={card}
            busy={busy === 'notes'}
            onSave={(notes) => run('notes', () => api.patch(`/pipeline/cards/${card.id}`, { notes }))}
          />

          <Replies
            replies={replies}
            drafts={drafts}
            busy={busy}
            onProcess={(replyId) =>
              run(`reply:${replyId}`, () =>
                /* FR-422: classify, move the card, draft an answer. Nothing sends. */
                api.post(`/pipeline/replies/${replyId}/process`, {
                  use_llm: true,
                  draft: true,
                  move_card: true,
                }),
              )
            }
            onEdit={(draftId, values) =>
              run(`draft:${draftId}`, () => api.patch(`/pipeline/drafts/${draftId}`, values))
            }
            onApprove={(draftId, values) =>
              run(`draft:${draftId}`, () =>
                api.post(`/pipeline/drafts/${draftId}/approve`, values),
              )
            }
            onDiscard={(draftId) =>
              run(`draft:${draftId}`, () => api.post(`/pipeline/drafts/${draftId}/discard`, {}))
            }
            onRespond={() => onRespond?.(card)}
          />

          <div className="card" style={{ marginTop: 14 }}>
            <div className="card-header">
              <h3>Prepare</h3>
            </div>
            <div className="row row-wrap">
              <button className="btn" onClick={() => onRehearse(card)}>
                Mock interview
              </button>
              <HelpTip term="mock_interview" />
              {NEGOTIABLE_STAGES.includes(card.stage) ? (
                <>
                  <button className="btn" onClick={() => onNegotiate(card)}>
                    Salary negotiation brief
                  </button>
                  <HelpTip term="negotiation_brief" />
                </>
              ) : (
                <span className="small muted">
                  The negotiation brief opens from the interview stage onwards (FR-444).
                </span>
              )}
              {card.briefing_pdf_path && card.application_package_id && (
                <button
                  className="btn btn-sm"
                  onClick={() =>
                    api
                      .download(
                        `/applications/${card.application_package_id}/documents/briefing`,
                        'company-briefing.pdf',
                      )
                      .catch(setActionError)
                  }
                >
                  Company briefing (PDF)
                </button>
              )}
            </div>
          </div>

          <History events={events} />
        </>
      )}
    </Modal>
  )
}

/* --- Stage dates ---------------------------------------------------------- */

function StageDates({ card }) {
  const dates = card.stage_dates || {}
  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div className="card-header">
        <h3>Stage dates</h3>
        <HelpTip term="pipeline_stage" />
      </div>
      <div className="grid grid-4">
        {STAGES.map((stage) => (
          <div key={stage.key}>
            <div className="tiny muted">{stage.label}</div>
            <div className="small">{dates[stage.key] ? formatDate(dates[stage.key]) : '–'}</div>
          </div>
        ))}
        <div>
          <div className="tiny muted">Sent to</div>
          <div className="small">{card.recipient_email || '–'}</div>
        </div>
      </div>
    </div>
  )
}

/* --- Next action (FR-421) ------------------------------------------------- */

function NextAction({ card, busy, onSave }) {
  const [action, setAction] = useState(card.next_action || '')
  const [due, setDue] = useState(toDateInput(card.next_action_due))

  useEffect(() => {
    setAction(card.next_action || '')
    setDue(toDateInput(card.next_action_due))
  }, [card.id, card.next_action, card.next_action_due])

  const dirty = action !== (card.next_action || '') || due !== toDateInput(card.next_action_due)

  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div className="card-header">
        <h3>Next action</h3>
        <HelpTip term="next_action_due" />
        <div className="spacer" />
        {isOverdue(card.next_action_due) && <Badge tone="danger">Overdue</Badge>}
      </div>
      <div className="grid grid-2">
        <div className="field">
          <label>What happens next</label>
          <input
            type="text"
            value={action}
            onChange={(e) => setAction(e.target.value)}
            placeholder="Send a follow-up e-mail"
          />
          <span className="hint">
            Set for you when the card moves stage; yours to overrule at any point.
          </span>
        </div>
        <div className="field">
          <label>Due</label>
          <input type="date" value={due} onChange={(e) => setDue(e.target.value)} />
          <span className="hint">A due date past today puts the card in “Due now”.</span>
        </div>
      </div>
      <button
        className="btn btn-sm btn-primary"
        disabled={!dirty || busy}
        onClick={() => onSave({ next_action: action, next_action_due: fromDateInput(due) })}
      >
        {busy ? <span className="spinner" /> : 'Save'}
      </button>
    </div>
  )
}

/* --- Notes ---------------------------------------------------------------- */

function Notes({ card, busy, onSave }) {
  const [notes, setNotes] = useState(card.notes || '')
  useEffect(() => setNotes(card.notes || ''), [card.id, card.notes])

  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div className="card-header">
        <h3>Notes</h3>
      </div>
      <textarea
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
        placeholder="Who you spoke to, what they asked, what you promised to send."
      />
      <button
        className="btn btn-sm btn-primary"
        style={{ marginTop: 8 }}
        disabled={notes === (card.notes || '') || busy}
        onClick={() => onSave(notes)}
      >
        {busy ? <span className="spinner" /> : 'Save notes'}
      </button>
    </div>
  )
}

/* --- History (FR-421) ----------------------------------------------------- */

/** A board whose automatic moves cannot be told from its manual ones has to be
 *  re-checked by hand, so every event names its trigger. */
function History({ events }) {
  if (!events.length) return null
  return (
    <div className="card">
      <div className="card-header">
        <h3>How this card moved</h3>
        <HelpTip term="automatic_transition" />
      </div>
      <div className="col" style={{ gap: 6 }}>
        {events.map((event) => (
          <div className="row row-wrap small" key={event.id}>
            <span className="muted nowrap">{formatDate(event.created_at)}</span>
            <span>
              {event.from_stage ? `${STAGE_LABEL[event.from_stage] || event.from_stage} → ` : ''}
              {STAGE_LABEL[event.to_stage] || event.to_stage}
            </span>
            <Badge tone={event.trigger === 'reply_detection' ? 'info' : undefined}>
              {TRIGGER_LABEL[event.trigger] || event.trigger}
            </Badge>
            {event.note && <span className="muted">{event.note}</span>}
          </div>
        ))}
      </div>
    </div>
  )
}
