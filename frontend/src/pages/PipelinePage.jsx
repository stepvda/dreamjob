/**
 * Application pipeline — the post-application board (FR-421..425, FR-444).
 *
 * This is the screen for everything that happens after an application leaves.
 * Five columns, one card per sent application, and two things that move a card
 * between them: reply detection where the reply is unambiguous (FR-422), and
 * you dragging it everywhere else. The card's own history keeps the two apart,
 * because a board whose automatic moves cannot be told from its manual ones is
 * a board that has to be re-checked by hand.
 *
 * Three rules shape the whole screen and are worth stating once:
 *
 *   Nothing sends.      A drafted answer is text until you approve it, and
 *                       approval queues it rather than sending it (NFR-305).
 *   Nothing closes.     A silent application is reported as silent; whether
 *                       that is a "no response" is your decision.
 *   Nothing is hidden.  Every figure in the negotiation brief names where it
 *                       came from, and rehearsal feedback says whether the
 *                       model judged the answer or only its shape.
 *
 * Endpoints: GET /pipeline/board, /pipeline/summary, /pipeline/cards/due,
 * /pipeline/cards/silent, POST /pipeline/sync, POST /pipeline/cards/{id}/stage
 * and /outcome. The card detail, rehearsal and brief fetch their own.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { Caution, FirstRun, HelpTip, ScreenIntro } from '../components/Help'
import WorkflowMap from '../components/WorkflowMap'
import { ErrorBox, Loading, useFetch } from '../components/ui'
import { Board, CloseCardDialog, FollowUpsDue } from './pipeline/board'
import CardDetail from './pipeline/detail'
import NegotiationBrief from './pipeline/negotiation'
import MockInterview from './pipeline/rehearse'
import { STAGES } from './pipeline/shared'
import ResponseSheet from './responses/ResponseSheet'

/** FR-327 nags before the moment passes, not after it. */
const DUE_HORIZON_DAYS = 3

export default function PipelinePage() {
  const journey = useFetch(() => api.get('/overview/journey'))
  const board = useFetch(() => api.get('/pipeline/board?include_closed=true'))
  const summary = useFetch(() => api.get('/pipeline/summary'))
  const due = useFetch(() => api.get(`/pipeline/cards/due?within_days=${DUE_HORIZON_DAYS}`))
  const silent = useFetch(() => api.get('/pipeline/cards/silent'))

  const [openCardId, setOpenCardId] = useState(null)
  const [responding, setResponding] = useState(null)
  const [rehearsing, setRehearsing] = useState(null)
  const [negotiating, setNegotiating] = useState(null)
  const [closing, setClosing] = useState(null)
  const [busyCardId, setBusyCardId] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [notice, setNotice] = useState(null)

  const total = board.data
    ? board.data.stages.reduce((sum, stage) => sum + stage.cards.length, 0)
    : 0

  function refresh() {
    board.reload()
    summary.reload()
    due.reload()
    silent.reload()
  }

  function findCard(cardId) {
    for (const column of board.data?.stages || []) {
      const hit = column.cards.find((c) => c.id === cardId)
      if (hit) return hit
    }
    return null
  }

  /**
   * FR-421: a manual move may go in any direction, including backwards,
   * because you know things the mailbox does not. Only `closed` needs more
   * than a stage — it is the one column that carries an outcome, and the
   * outcome is the whole of what FR-425 has to learn from.
   */
  async function move(cardId, stage) {
    if (stage === 'closed') {
      setClosing(findCard(cardId))
      return
    }
    setBusyCardId(cardId)
    setActionError(null)
    try {
      await api.post(`/pipeline/cards/${cardId}/stage`, { stage })
      refresh()
    } catch (err) {
      setActionError(err)
    } finally {
      setBusyCardId(null)
    }
  }

  async function closeCard(outcome, note) {
    const cardId = closing.id
    setBusyCardId(cardId)
    setActionError(null)
    try {
      await api.post(`/pipeline/cards/${cardId}/outcome`, { outcome, note })
      setClosing(null)
      refresh()
    } catch (err) {
      setActionError(err)
    } finally {
      setBusyCardId(null)
    }
  }

  async function sync() {
    setActionError(null)
    try {
      const res = await api.post('/pipeline/sync', {})
      setNotice(
        res.created
          ? `Opened ${res.created} card${res.created === 1 ? '' : 's'} for applications that had none.`
          : 'Every sent application already has a card.',
      )
      refresh()
    } catch (err) {
      setActionError(err)
    }
  }

  return (
    <div className="content-wide">
      <WorkflowMap journey={journey.data?.journey || {}} compact current="pipeline" />
      <ScreenIntro pathname="/pipeline" />

      {/* NFR-305: the two decisions this screen refuses to make for you. */}
      <Caution title="Nothing here is sent, and nothing here is closed, without you">
        Replies move a card only where they are unambiguous. A drafted answer stays a draft
        until you approve it, and approving it queues it for the mail screen rather than
        sending it. An application that has gone quiet is reported as quiet — whether that
        counts as a “no response” is yours to say.
      </Caution>

      {actionError && <ErrorBox error={actionError} onRetry={refresh} />}
      {notice && (
        <div className="alert alert-ok">
          <div style={{ flex: 1 }}>{notice}</div>
          <button className="btn btn-sm btn-ghost" onClick={() => setNotice(null)}>
            ✕
          </button>
        </div>
      )}

      <div className="row row-wrap" style={{ margin: '4px 0 14px' }}>
        <button className="btn btn-sm" onClick={() => setResponding({ scope: null })}>
          Record or correct a response
        </button>
        <Link className="btn btn-sm" to="/applications">
          Applications
        </Link>
        <Link className="btn btn-sm" to="/insights">
          What works
        </Link>
        <div className="spacer" />
        <button className="btn btn-sm btn-ghost" onClick={sync} title="Open a card for every sent application that has none">
          Reconcile with sent applications
        </button>
      </div>

      {summary.data && total > 0 && <Summary summary={summary.data} />}

      {board.loading && <Loading rows={5} />}
      {board.error && <ErrorBox error={board.error} onRetry={board.reload} />}

      {!board.loading && !board.error && total === 0 && (
        <FirstRun
          pathname="/pipeline"
          action={
            <div className="row row-wrap">
              <button className="btn btn-primary" onClick={sync}>
                Open cards for my sent applications
              </button>
              <Link className="btn" to="/applications">
                Nothing sent yet — go to Applications
              </Link>
            </div>
          }
        >
          The board fills itself from what you have actually sent: one card per dispatched
          application. If you have sent applications and see nothing here, reconciling opens the
          missing cards.
        </FirstRun>
      )}

      {!board.loading && !board.error && total > 0 && (
        <>
          <FollowUpsDue due={due.data} silent={silent.data} onOpen={setOpenCardId} />
          <Board
            board={board.data}
            onMove={move}
            onOpen={setOpenCardId}
            busyCardId={busyCardId}
          />
          <p className="small muted" style={{ marginTop: 12 }}>
            Drag a card to another column to override the detected stage
            <HelpTip term="automatic_transition" />. Click a card to open the reply, the drafted
            answer, the rehearsal and the negotiation brief.
          </p>
        </>
      )}

      {openCardId && (
        <CardDetail
          cardId={openCardId}
          onClose={() => setOpenCardId(null)}
          onChanged={refresh}
          onRespond={(card) => {
            setOpenCardId(null)
            setResponding({ scope: card })
          }}
          onRehearse={(card) => {
            setOpenCardId(null)
            setRehearsing(card)
          }}
          onNegotiate={(card) => {
            setOpenCardId(null)
            setNegotiating(card)
          }}
        />
      )}

      {responding && (
        <ResponseSheet
          key={responding.scope?.id || 'all'}
          scope={responding.scope}
          onClose={() => setResponding(null)}
          onChanged={refresh}
        />
      )}
      {rehearsing && <MockInterview card={rehearsing} onClose={() => setRehearsing(null)} />}
      {negotiating && (
        <NegotiationBrief card={negotiating} onClose={() => setNegotiating(null)} />
      )}
      {closing && (
        <CloseCardDialog
          card={closing}
          busy={busyCardId === closing.id}
          onCancel={() => setClosing(null)}
          onConfirm={closeCard}
        />
      )}
    </div>
  )
}

/* --- Counts across the top ------------------------------------------------ */

function Summary({ summary }) {
  return (
    <div className="grid grid-4" style={{ marginBottom: 14 }}>
      {STAGES.map((stage) => (
        <div className="stat-tile" key={stage.key}>
          <div className="stat-value">{summary.stages?.[stage.key] ?? 0}</div>
          <div className="stat-label">{stage.label}</div>
        </div>
      ))}
      <div className="stat-tile">
        <div className="stat-value">{summary.due_now ?? 0}</div>
        <div className="stat-label">
          Due now
          <HelpTip term="next_action_due" />
        </div>
      </div>
      <div className="stat-tile">
        <div className="stat-value">{summary.silent ?? 0}</div>
        <div className="stat-label">
          Gone quiet
          <HelpTip term="silent_application" />
        </div>
      </div>
    </div>
  )
}
