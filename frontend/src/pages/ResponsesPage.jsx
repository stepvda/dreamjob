/**
 * Responses received (extends FR-326; feeds FR-421, FR-422, FR-425).
 *
 * Automatic detection only sees replies that arrive in a mailbox the system
 * can poll. A good share of them do not: a phone call, a LinkedIn message, an
 * ATS portal, or any reply at all when the application went out through
 * Resend, which has no inbox. Those responses are the most informative rows
 * the pipeline has, so this screen exists to type them in - and a hand-entered
 * response then travels exactly the same path as a detected one.
 *
 * Two decisions are deliberate here:
 *
 * - A rejection is recorded with the same weight and the same styling as
 *   every other outcome. It is data, and the segment analysis (FR-425) is
 *   worthless without it.
 * - Where the model's reading of a reply differs from the person's, both are
 *   shown and the correction is one visible control, not a menu. A misread
 *   rejection silently distorts every rate it is counted in (NFR-305).
 */

import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { Caution, FirstRun, HelpTip, ScreenIntro } from '../components/Help'
import WorkflowMap from '../components/WorkflowMap'
import { Empty, ErrorBox, Loading, useFetch } from '../components/ui'
import ResponseRow from './responses/ResponseRow'
import { AwaitingPicker, RecordForm } from './responses/RecordResponse'
import { MIN_TO_ANALYSE, SILENCE_DAYS, daysSince, normaliseOutcome, outcomeLabel } from './responses/vocabulary'

/* --- The screen ----------------------------------------------------------- */

export default function ResponsesPage() {
  const awaiting = useFetch(() => api.get('/learning/responses/awaiting'))
  const responses = useFetch(() => api.get('/learning/responses'))
  const journey = useFetch(() => api.get('/overview/journey').catch(() => null))

  const [target, setTarget] = useState(null)
  const [notice, setNotice] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [correcting, setCorrecting] = useState(null)

  /**
   * The model's reading of a reply the person also described is returned by
   * POST /learning/responses but is not stored — the row keeps the stated
   * outcome only. Holding it here keeps the disagreement visible for as long
   * as the session that recorded it.
   */
  const [modelReadings, setModelReadings] = useState({})

  const rows = responses.data || []
  const waiting = awaiting.data || []

  const summary = useMemo(() => {
    const counted = rows.map((r) => normaliseOutcome(r.stated_outcome || r.classification))
    const silent = waiting.filter((r) => (daysSince(r.sent_at) ?? 0) >= SILENCE_DAYS).length
    return {
      total: rows.length,
      interviews: counted.filter((o) => o === 'interview').length,
      rejections: counted.filter((o) => o === 'rejection').length,
      waiting: waiting.length,
      silent,
      // What the segment analysis would call resolved: answered, or silent
      // past the window. Approximate — the backend counts pipeline cards.
      resolved: rows.length + silent,
    }
  }, [rows, waiting])

  async function correct(row, stated) {
    setActionError(null)
    setCorrecting(row.manual_response_id)
    try {
      await api.patch(`/learning/responses/${row.manual_response_id}`, {
        stated_outcome: stated,
      })
      setModelReadings((m) => {
        const next = { ...m }
        delete next[row.manual_response_id]
        return next
      })
      setNotice(
        `Corrected to “${outcomeLabel(stated)}”. Every rate this response is counted in has moved with it.`,
      )
      responses.reload()
    } catch (err) {
      setActionError(err)
    } finally {
      setCorrecting(null)
    }
  }

  function recorded(result) {
    setTarget(null)
    const stated = normaliseOutcome(result.stated_outcome)
    const model = normaliseOutcome(result.classification?.classification)
    if (result.manual_response_id && result.classification) {
      setModelReadings((m) => ({ ...m, [result.manual_response_id]: result.classification }))
    }
    const moved = result.stage_moved_to
      ? ` The application moved to “${result.stage_moved_to}” on the pipeline board.`
      : ''
    setNotice(
      stated && model && stated !== model
        ? `Recorded as “${outcomeLabel(stated)}”. The model read the same text as “${outcomeLabel(model)}”; yours is what was kept.${moved}`
        : `Recorded.${moved}`,
    )
    responses.reload()
    awaiting.reload()
  }

  // Each half owns its own three states: one list failing must not hide the
  // other, and an empty list is not the same thing as a failed one.
  const empty = !responses.loading && !responses.error && rows.length === 0

  return (
    <div className="content-wide">
      <WorkflowMap journey={journey.data?.journey || {}} compact current="responses" />
      <ScreenIntro pathname="/responses" />

      {/* NFR-305 / FR-425: the classification is advisory, and a wrong one is
          not a cosmetic problem — it is counted in every rate on /insights. */}
      <Caution title="A wrong outcome distorts every rate it is counted in">
        A rejection filed as “interest”, or an automatic acknowledgement filed as a real
        reply, changes the reply rate for the kind of work, the seniority and the company
        size it belongs to. Correcting one here corrects it everywhere. Nothing on this
        screen decides anything on its own — what you state always wins over what the
        model read.
      </Caution>

      {actionError && <ErrorBox error={actionError} />}
      {notice && (
        <div className="alert alert-ok">
          <div style={{ flex: 1 }}>{notice}</div>
          <button className="btn btn-sm btn-ghost" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </div>
      )}

      {/* --- Summary --- */}
      <div className="grid grid-4" style={{ marginBottom: 16 }}>
        <div className="stat-tile">
          <div className="stat-value">{summary.total}</div>
          <div className="stat-label">Responses recorded</div>
          <div className="stat-sub">detected and entered by hand</div>
        </div>
        <div className="stat-tile">
          <div className="stat-value">{summary.interviews}</div>
          <div className="stat-label">Interview invitations</div>
          <div className="stat-sub">{summary.rejections} rejections recorded</div>
        </div>
        <div className="stat-tile">
          <div className="stat-value">{summary.waiting}</div>
          <div className="stat-label">
            Still waiting
            <HelpTip term="resolved" />
          </div>
          <div className="stat-sub">
            {summary.silent} silent for more than {SILENCE_DAYS} days
          </div>
        </div>
        <div className="stat-tile">
          <div className="stat-value">{summary.resolved}</div>
          <div className="stat-label">
            Resolved applications
            <HelpTip term="sample_size" />
          </div>
          <div className="stat-sub">
            {summary.resolved >= MIN_TO_ANALYSE ? (
              <Link to="/insights">See what works →</Link>
            ) : (
              `${MIN_TO_ANALYSE - summary.resolved} more before the analysis says anything`
            )}
          </div>
        </div>
      </div>

      {/* --- 1. Record a response --- */}
      <div className="card">
        <div className="card-header">
          <h3>Record a response</h3>
          <div className="spacer" />
          <span className="small muted">
            Anything automatic detection cannot see — a call, a LinkedIn message, an ATS
            portal, or any reply to mail sent through Resend.
          </span>
        </div>

        {target ? (
          <RecordForm target={target} onCancel={() => setTarget(null)} onRecorded={recorded} />
        ) : (
          <>
            {awaiting.loading && <Loading rows={3} />}
            {awaiting.error && <ErrorBox error={awaiting.error} onRetry={awaiting.reload} />}
            {!awaiting.loading &&
              !awaiting.error &&
              (waiting.length ? (
                <AwaitingPicker rows={waiting} onPick={setTarget} />
              ) : (
                <Empty title="Every sent application has a response recorded">
                  Nothing is waiting. When the next application goes out it appears here, and
                  a response that arrives anywhere but a polled mailbox is recorded from this
                  list.
                </Empty>
              ))}
          </>
        )}
      </div>

      {/* --- 2. What has come back --- */}
      <div className="card">
        <div className="card-header">
          <h3>What has come back</h3>
          <div className="spacer" />
          {rows.length > 0 && <span className="small muted">{rows.length} responses</span>}
        </div>

        {responses.loading && <Loading rows={4} />}
        {responses.error && <ErrorBox error={responses.error} onRetry={responses.reload} />}

        {empty && (
          <FirstRun
            pathname="/responses"
            action={
              waiting.length ? (
                <button className="btn btn-primary" onClick={() => setTarget(waiting[0])}>
                  Record a response to {waiting[0].company_name || 'your first application'}
                </button>
              ) : (
                <Link className="btn btn-primary" to="/applications">
                  Send an application first
                </Link>
              )
            }
          >
            Nothing has come back yet — or nothing has been recorded. Replies to a connected
            Gmail account appear here on their own; everything else reaches this screen only
            because you put it here, and the analysis on “What works” can only count what is
            on this list.
          </FirstRun>
        )}

        {!responses.loading && !responses.error && rows.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>
                    Arrived
                    <HelpTip term="response_channel" />
                  </th>
                  <th>Company and role</th>
                  <th>
                    What it was
                    <HelpTip term="stated_outcome" />
                  </th>
                  <th>Correct it</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <ResponseRow
                    key={row.id}
                    row={row}
                    modelReading={modelReadings[row.manual_response_id]}
                    busy={correcting === row.manual_response_id}
                    onCorrect={correct}
                    onAnother={(r) => {
                      setTarget({
                        dispatch_id: r.dispatch_id,
                        opportunity_id: r.opportunity_id,
                        company_name: r.company_name,
                        opportunity_title: r.opportunity_title,
                        opportunity_kind: r.opportunity_kind,
                        subject: r.subject,
                      })
                      window.scrollTo({ top: 0, behavior: 'smooth' })
                    }}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

