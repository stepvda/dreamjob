/**
 * Mock interview (FR-424).
 *
 * One question at a time, because that is the only way a rehearsal is worth
 * anything: you answer without seeing what comes next, and the feedback lands
 * before the next question is asked. The session is stored, numbered by round,
 * and a repeat round opens on the weak spots the last one found — so "store
 * and repeat" is a property of the data, not a button.
 *
 * Feedback arrives in one of two ways and says which. With the model it judges
 * what the answer says; without it, `method: "structural"` checks only that the
 * answer has a situation, a figure and an outcome. The distinction is shown
 * rather than smoothed over.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import { Badge, ErrorBox, Loading, Meter, Modal, formatDate, useFetch } from '../../components/ui'

const FOCUS = [
  { value: 'mixed', label: 'Mixed — the role and how you work' },
  { value: 'role', label: 'The role — the work itself' },
  { value: 'behavioural', label: 'Behavioural — situations and decisions' },
]

const SEVERITY_TONE = { high: 'danger', medium: 'warn', low: undefined }

export default function MockInterview({ card, onClose }) {
  const history = useFetch(
    () => api.get(`/pipeline/mock-interviews?opportunity_id=${card.opportunity_id}`),
    [card.opportunity_id],
  )

  const [session, setSession] = useState(null)
  const [focus, setFocus] = useState('mixed')
  const [questions, setQuestions] = useState(8)
  const [answer, setAnswer] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function call(fn) {
    setBusy(true)
    setError(null)
    try {
      const next = await fn()
      setSession(next)
      setAnswer('')
      return next
    } catch (err) {
      setError(err)
      return null
    } finally {
      setBusy(false)
    }
  }

  const open = session && session.status === 'open'
  const finished = session && session.status === 'finished'

  return (
    <Modal
      wide
      title={`Mock interview — ${card.opportunity_title || 'this role'}`}
      onClose={onClose}
      actions={
        <>
          {open && (
            <button
              className="btn"
              disabled={busy}
              onClick={() =>
                call(() =>
                  api.post(`/pipeline/mock-interviews/${session.id}/finish`, {}),
                ).then((s) => s && history.reload())
              }
            >
              End and show the weak spots
            </button>
          )}
          <button className="btn btn-ghost" onClick={onClose}>
            Close
          </button>
        </>
      }
    >
      {error && <ErrorBox error={error} />}

      {!session && (
        <>
          <p className="section-intro">
            A rehearsal against this specific role — the vacancy text, the company briefing and
            your own profile. It pushes back on thin answers; the point is to be told here
            rather than in the room.
          </p>

          <div className="grid grid-2">
            <div className="field">
              <label>
                Focus
                <HelpTip
                  title="Focus"
                  align="right"
                >
                  “Role” asks about the work in the vacancy. “Behavioural” asks for situations
                  you were actually in. “Mixed” alternates, which is what most real interviews
                  do.
                </HelpTip>
              </label>
              <select value={focus} onChange={(e) => setFocus(e.target.value)}>
                {FOCUS.map((f) => (
                  <option key={f.value} value={f.value}>
                    {f.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Questions</label>
              <input
                type="number"
                min={3}
                max={20}
                value={questions}
                onChange={(e) => setQuestions(Number(e.target.value))}
              />
              <span className="hint">Between 3 and 20. Eight is about twenty minutes.</span>
            </div>
          </div>

          <button
            className="btn btn-primary"
            disabled={busy}
            onClick={() =>
              call(() =>
                api.post('/pipeline/mock-interviews', {
                  opportunity_id: card.opportunity_id,
                  focus,
                  questions,
                  use_llm: true,
                }),
              ).then((s) => s && history.reload())
            }
          >
            {busy ? <span className="spinner" /> : 'Start the session'}
          </button>

          <PreviousRounds history={history} />
        </>
      )}

      {session && (
        <>
          <div className="row row-wrap" style={{ marginBottom: 14 }}>
            <Badge tone="accent">Round {session.round_number}</Badge>
            <span className="small muted">
              {session.questions_asked} of {session.questions_planned} questions
            </span>
            <Badge tone={session.generated_by === 'llm' ? 'info' : 'warn'}>
              {session.generated_by === 'llm' ? 'model-written' : 'template questions'}
            </Badge>
            {session.generated_by !== 'llm' && (
              <HelpTip term="rehearsal_feedback" />
            )}
          </div>

          {session.turns.map((turn) => (
            <Turn key={turn.index} turn={turn} />
          ))}

          {open && session.awaiting_answer && (
            <div className="field" style={{ marginTop: 10 }}>
              <label>Your answer</label>
              <textarea
                value={answer}
                onChange={(e) => setAnswer(e.target.value)}
                style={{ minHeight: 150 }}
                placeholder="One situation, what you did, one number, how it ended."
                autoFocus
              />
              <span className="hint">
                Answer as you would out loud. Feedback comes back before the next question.
              </span>
              <button
                className="btn btn-primary"
                style={{ marginTop: 8, alignSelf: 'flex-start' }}
                disabled={busy || !answer.trim()}
                onClick={() =>
                  call(() =>
                    api.post(`/pipeline/mock-interviews/${session.id}/answer`, {
                      answer,
                      use_llm: true,
                    }),
                  ).then((s) => s?.status === 'finished' && history.reload())
                }
              >
                {busy ? <span className="spinner" /> : 'Answer and continue'}
              </button>
            </div>
          )}

          {finished && <Closing session={session} onAgain={() => setSession(null)} />}
        </>
      )}
    </Modal>
  )
}

/* --- One question, its answer and its feedback ---------------------------- */

function Turn({ turn }) {
  return (
    <div className="qa-turn">
      <div className="qa-question">
        {turn.index + 1}. {turn.question}
        {turn.kind && (
          <span className="badge" style={{ marginLeft: 8 }}>
            {turn.kind.replace(/_/g, ' ')}
          </span>
        )}
      </div>
      {turn.rationale && <p className="tiny muted" style={{ margin: '0 0 6px' }}>{turn.rationale}</p>}
      {turn.answer && <div className="qa-answer">{turn.answer}</div>}
      {turn.feedback && <Feedback feedback={turn.feedback} />}
    </div>
  )
}

function Feedback({ feedback }) {
  return (
    <div className="card">
      <div className="row row-wrap" style={{ marginBottom: 8 }}>
        <strong className="small">Feedback</strong>
        <Meter value={feedback.score} max={5} width={120} />
        <span className="tiny muted">{feedback.score} of 5</span>
        <div className="spacer" />
        <Badge tone={feedback.method === 'llm' ? 'info' : 'warn'}>{feedback.method}</Badge>
      </div>

      {feedback.note && (
        <p className="tiny muted" style={{ margin: '0 0 8px' }}>
          {feedback.note}
        </p>
      )}

      {feedback.strengths?.length > 0 && (
        <>
          <h4>What worked</h4>
          <ul className="help-tips">
            {feedback.strengths.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        </>
      )}
      {feedback.gaps?.length > 0 && (
        <>
          <h4>What an interviewer would push on</h4>
          <ul className="help-tips">
            {feedback.gaps.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        </>
      )}
      {feedback.improved_answer && (
        <>
          <h4>The same answer, tightened</h4>
          <p className="small" style={{ margin: 0 }}>
            {feedback.improved_answer}
          </p>
        </>
      )}
    </div>
  )
}

/* --- The closing list (FR-424) -------------------------------------------- */

function Closing({ session, onAgain }) {
  return (
    <div className="card" style={{ marginTop: 14 }}>
      <div className="card-header">
        <h3>Weak spots to rehearse</h3>
        <HelpTip term="weak_spot" />
        <div className="spacer" />
        <button className="btn btn-sm" onClick={onAgain}>
          Run another round
        </button>
      </div>

      {session.summary && <p className="small">{session.summary}</p>}

      {(session.weak_spots || []).length === 0 && (
        <p className="small muted" style={{ margin: 0 }}>
          Nothing stood out as weak in this round.
        </p>
      )}

      {(session.weak_spots || []).map((spot, i) => (
        <div key={i} style={{ marginBottom: 12 }}>
          <div className="row row-wrap" style={{ marginBottom: 3 }}>
            <strong className="small">{spot.topic}</strong>
            <Badge tone={SEVERITY_TONE[spot.severity]}>{spot.severity}</Badge>
          </div>
          <p className="small muted" style={{ margin: '0 0 3px' }}>
            {spot.why}
          </p>
          <p className="small" style={{ margin: 0 }}>
            {spot.rehearse}
          </p>
        </div>
      ))}

      <p className="tiny muted" style={{ margin: 0 }}>
        The next round for this role opens on these, so repeating the session is not repeating
        the same questions.
      </p>
    </div>
  )
}

/* --- Earlier rounds ------------------------------------------------------- */

function PreviousRounds({ history }) {
  if (history.loading) return <Loading rows={2} />
  if (history.error) return <ErrorBox error={history.error} onRetry={history.reload} />
  const rows = history.data || []
  if (!rows.length) return null

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div className="card-header">
        <h3>Earlier rounds</h3>
      </div>
      {rows.map((row) => (
        <div key={row.id} style={{ marginBottom: 10 }}>
          <div className="row row-wrap">
            <Badge tone="accent">Round {row.round_number}</Badge>
            <Badge tone={row.status === 'finished' ? 'ok' : 'warn'}>{row.status}</Badge>
            <span className="small muted">{formatDate(row.finished_at || row.created_at)}</span>
            <span className="small muted">{row.focus}</span>
          </div>
          {row.summary && (
            <p className="small muted" style={{ margin: '4px 0 0' }}>
              {row.summary}
            </p>
          )}
          {(row.weak_spots || []).length > 0 && (
            <p className="tiny muted" style={{ margin: '4px 0 0' }}>
              Weak spots: {(row.weak_spots || []).map((s) => s.topic).join(' · ')}
            </p>
          )}
        </div>
      ))}
    </div>
  )
}
