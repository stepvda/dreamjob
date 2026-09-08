/**
 * The board itself (FR-421), the follow-ups strip (FR-327) and the dialog that
 * closes a card.
 *
 * Two things move a card. Reply detection moves it where the reply is
 * unambiguous, and you move it everywhere else — so the drag target is the
 * whole column and the drop posts `trigger: user` by going through
 * `POST /cards/{id}/stage`. A manual move may go backwards; an automatic one
 * never does, which is why the two are told apart on the card's own history.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'

import { HelpTip } from '../../components/Help'
import { Badge, KindBadge, Modal, formatDate } from '../../components/ui'
import { OUTCOMES, OUTCOME_TONE, STAGES, isOverdue } from './shared'

/* --- Follow-ups due (FR-327) ---------------------------------------------- */

/**
 * FR-327: an application with no answer needs a follow-up, and the reminder is
 * only useful before the moment passes. It sits above the board because a
 * board is a place you browse and this is a thing you do today.
 */
export function FollowUpsDue({ due, silent, onOpen }) {
  const rows = due || []
  const quiet = silent || []
  if (!rows.length && !quiet.length) return null

  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div className="card-header">
        <h3>Due now</h3>
        <HelpTip term="next_action_due" />
        <div className="spacer" />
        <Link className="btn btn-sm" to="/mail">
          Follow-up e-mails
        </Link>
      </div>

      {rows.length > 0 && (
        <div className="col" style={{ gap: 8 }}>
          {rows.map((card) => (
            <div className="row row-wrap" key={card.id}>
              <button className="btn btn-sm btn-ghost" onClick={() => onOpen(card.id)}>
                {card.opportunity_title || 'Untitled role'}
              </button>
              <span className="small muted">{card.company_name || 'Unknown company'}</span>
              <Badge tone={isOverdue(card.next_action_due) ? 'danger' : 'warn'}>
                {isOverdue(card.next_action_due) ? 'Overdue' : 'Due'}{' '}
                {formatDate(card.next_action_due)}
              </Badge>
              {isOverdue(card.follow_up_due_at) && (
                <Badge tone="warn">Follow-up e-mail due</Badge>
              )}
              <span className="small muted">{card.next_action}</span>
            </div>
          ))}
        </div>
      )}

      {quiet.length > 0 && (
        /* NFR-305: silence is reported, never acted on. Closing is a decision. */
        <p className="small muted" style={{ margin: rows.length ? '12px 0 0' : 0 }}>
          {quiet.length} application{quiet.length === 1 ? ' has' : 's have'} had no answer for
          three weeks or more. Nothing closes them for you
          <HelpTip term="silent_application" /> — decide whether each is a “no response” and
          close it yourself.
        </p>
      )}
    </div>
  )
}

/* --- The board (FR-421) --------------------------------------------------- */

export function Board({ board, onMove, onOpen, busyCardId }) {
  const [dragging, setDragging] = useState(null)
  const [over, setOver] = useState(null)

  const columns = board?.stages || []

  function drop(stage) {
    const cardId = dragging
    setDragging(null)
    setOver(null)
    if (!cardId) return
    const from = columns.find((c) => c.cards.some((k) => k.id === cardId))
    if (from?.stage === stage) return
    onMove(cardId, stage)
  }

  return (
    <div className="board-scroll">
      <div className="board">
        {STAGES.map((stage) => {
          const column = columns.find((c) => c.stage === stage.key) || { cards: [] }
          return (
            <section
              key={stage.key}
              className={`board-col${over === stage.key ? ' drag-over' : ''}`}
              onDragOver={(e) => {
                e.preventDefault()
                setOver(stage.key)
              }}
              onDragLeave={() => setOver((s) => (s === stage.key ? null : s))}
              onDrop={(e) => {
                e.preventDefault()
                drop(stage.key)
              }}
            >
              <h4>
                {stage.label}
                <span className="badge">{column.cards.length}</span>
                {stage.key === 'closed' && <HelpTip term="pipeline_outcome" />}
              </h4>
              <p className="tiny muted" style={{ margin: '-4px 0 9px' }}>
                {stage.hint}
              </p>

              {column.cards.map((card) => (
                <BoardCard
                  key={card.id}
                  card={card}
                  busy={busyCardId === card.id}
                  onDragStart={() => setDragging(card.id)}
                  onDragEnd={() => {
                    setDragging(null)
                    setOver(null)
                  }}
                  onOpen={() => onOpen(card.id)}
                />
              ))}

              {column.cards.length === 0 && (
                <p className="tiny muted" style={{ margin: 0 }}>
                  Drag a card here to put an application in this stage.
                </p>
              )}
            </section>
          )
        })}
      </div>
    </div>
  )
}

function BoardCard({ card, busy, onDragStart, onDragEnd, onOpen }) {
  const overdue = card.overdue || isOverdue(card.next_action_due)

  return (
    <article
      className={`board-card${overdue ? ' overdue' : ''}${busy ? ' busy' : ''}`}
      draggable
      onDragStart={(e) => {
        e.dataTransfer.effectAllowed = 'move'
        // Firefox refuses to start a drag without payload on the transfer.
        e.dataTransfer.setData('text/plain', card.id)
        onDragStart()
      }}
      onDragEnd={onDragEnd}
      onClick={onOpen}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onOpen()
        }
      }}
      title="Open the card, or drag it to another column"
    >
      <div style={{ fontWeight: 600, marginBottom: 3 }}>
        {card.opportunity_title || 'Untitled role'}
      </div>
      <div className="small muted" style={{ marginBottom: 6 }}>
        {card.company_name || 'Unknown company'}
      </div>

      <div className="row row-wrap" style={{ gap: 5, marginBottom: 6 }}>
        {/* FR-263: a speculative opening stays distinguishable in every view. */}
        {card.opportunity_kind && <KindBadge kind={card.opportunity_kind} />}
        {card.outcome && <Badge tone={OUTCOME_TONE[card.outcome]}>{card.outcome.replace('_', ' ')}</Badge>}
        {busy && <span className="spinner" />}
      </div>

      {card.next_action && (
        <div className="tiny" style={{ color: 'var(--ink-2)' }}>
          {card.next_action}
        </div>
      )}
      {card.next_action_due && (
        <div className="tiny" style={{ marginTop: 3 }}>
          <Badge tone={overdue ? 'danger' : undefined}>
            {overdue ? 'Overdue' : 'Due'} {formatDate(card.next_action_due)}
          </Badge>
        </div>
      )}
    </article>
  )
}

/* --- Closing a card (FR-421, FR-425) -------------------------------------- */

/**
 * Only a closed card carries an outcome, and the outcome is what FR-425 learns
 * from — so a drop into "Closed" asks for it rather than closing the card with
 * an empty result nobody can learn anything from.
 */
export function CloseCardDialog({ card, onCancel, onConfirm, busy }) {
  const [outcome, setOutcome] = useState('rejected')
  const [note, setNote] = useState('')

  return (
    <Modal
      title={`Close “${card.opportunity_title || 'this application'}”`}
      onClose={busy ? undefined : onCancel}
      actions={
        <>
          <button className="btn btn-ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            className="btn btn-primary"
            onClick={() => onConfirm(outcome, note.trim() || undefined)}
            disabled={busy}
          >
            {busy ? <span className="spinner" /> : 'Close the card'}
          </button>
        </>
      }
    >
      <p className="small muted" style={{ marginTop: 0 }}>
        How did it end? This is the only thing “What works” has to learn from, so an honest
        rejection is worth as much as an acceptance.
      </p>

      <div className="field">
        <label>
          Outcome
          <HelpTip term="pipeline_outcome" />
        </label>
        <select value={outcome} onChange={(e) => setOutcome(e.target.value)}>
          {OUTCOMES.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label>Note (optional)</label>
        <textarea
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="What you would want to remember about this one in six months."
        />
        <span className="hint">Appended to the card's notes with today's date.</span>
      </div>
    </Modal>
  )
}
