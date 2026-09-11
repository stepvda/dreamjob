/**
 * What works, and where to redirect (FR-285, FR-425, CR-408).
 *
 * The product owner's ask, in their words: *if a lot of rejections are received
 * for a certain type of job then the AI should advise on how to redirect the
 * type of job/company to get higher acceptance rates.*
 *
 * The screen is that ask in three parts, in the order they have to be read:
 *
 *   1. The figures  - GET /api/learning/patterns. Outcome rate per kind of job
 *      and company, each with its sample size and Wilson interval.
 *   2. The advice   - GET/POST /api/learning/advice. Proposals derived from
 *      those figures, each carrying the numbers it rests on.
 *   3. The history  - what was applied or dismissed, so a change of direction
 *      can be traced and undone.
 *
 * NFR-305 governs the whole screen: nothing here acts on its own. Advice is a
 * proposal, applying one writes a NEW directive-set version, and the previous
 * version stays intact so the change can be reverted.
 *
 * Two honesty rules come out of postapp/segments.py and are enforced in the
 * layout rather than the copy: the sample size is as prominent as the
 * percentage, and `analysis.caveats` is shown above the tables instead of
 * beneath them - it is the part that stops three data points being read as a
 * finding.
 */

import { useCallback, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { HelpTip } from '../components/Help'
import { Badge, Empty, ErrorBox, Loading, Modal, Tabs, useFetch } from '../components/ui'
import MarketShell from './intelligence/MarketShell'
import AdviceCard from './insights/AdviceCard'
import AdviceHistory from './insights/AdviceHistory'
import FiguresCard from './insights/FiguresCard'

/** segments.MIN_SEGMENT_TO_ADVISE — below this the backend refuses to advise. */
const MIN_TO_ADVISE = 6

const OUTCOME_TABS = [
  { key: 'reply', label: 'Any reply' },
  { key: 'interview', label: 'Interview' },
  { key: 'offer', label: 'Offer' },
]

const OUTCOME_NOUN = { reply: 'reply', interview: 'interview', offer: 'offer' }

/** Reasons worth offering, so dismissing takes a sentence rather than a paragraph. */
const DISMISS_REASONS = [
  'The sample is too small to act on',
  'This is central to the job I actually want',
  'The segment is wrong — those applications differed in another way',
  'I already changed this myself',
  'Not now — I want to see more responses first',
]

export default function InsightsPage() {
  const [outcome, setOutcome] = useState('reply')

  /*
   * GET /api/learning/patterns serves the last stored run unless ?refresh=true,
   * because the figures only move when a response is recorded. The ref carries
   * the intent of a single click without making "recompute" a sticky mode.
   */
  const recomputeRef = useRef(false)
  const [recomputeToken, setRecomputeToken] = useState(0)
  const patterns = useFetch(
    useCallback(() => {
      const refresh = recomputeRef.current
      recomputeRef.current = false
      return api.get(`/learning/patterns?outcome=${outcome}${refresh ? '&refresh=true' : ''}`)
    }, [outcome]),
    [outcome, recomputeToken],
  )

  /* Unfiltered on purpose: a proposal made for replies still matters while the
     interview tab is open, and hiding it behind a tab would lose it silently. */
  const advice = useFetch(useCallback(() => api.get('/learning/advice'), []), [])
  const awaiting = useFetch(
    useCallback(() => api.get('/learning/responses/awaiting'), []),
    [],
  )
  const journey = useFetch(useCallback(() => api.get('/overview/journey'), []), [])

  const [generating, setGenerating] = useState(false)
  const [genResult, setGenResult] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [busyId, setBusyId] = useState(null)
  const [appliedById, setAppliedById] = useState({})
  const [confirmApply, setConfirmApply] = useState(null)
  const [dismissing, setDismissing] = useState(null)
  const [dismissReason, setDismissReason] = useState('')

  /*
   * The conflict flags a proposal carries — conflicts_with_dream_job,
   * conflict_note, extrapolated_target — are produced by redirection.generate
   * but have no column in `redirection_advice`, so they are not in what
   * GET /api/learning/advice returns. Keep them from the generate response and
   * overlay them onto the listed proposals by id.
   */
  const [conflictById, setConflictById] = useState({})

  const resolved = patterns.data?.resolved_size ?? 0
  const shortfall = Math.max(0, MIN_TO_ADVISE - resolved)
  const outstanding = awaiting.data?.length ?? 0
  const noun = OUTCOME_NOUN[outcome] || outcome

  const open = advice.data?.open || []
  const history = advice.data?.history || []

  async function generate() {
    setActionError(null)
    setGenerating(true)
    try {
      const res = await api.post(`/learning/advice/generate?outcome=${outcome}`)
      setGenResult(res)
      const overlay = {}
      for (const p of res.proposals || []) {
        if (p.id) {
          overlay[p.id] = {
            conflicts_with_dream_job: p.conflicts_with_dream_job,
            conflict_note: p.conflict_note,
            extrapolated_target: p.extrapolated_target,
          }
        }
      }
      setConflictById((prev) => ({ ...prev, ...overlay }))
      advice.reload()
      patterns.reload()
    } catch (err) {
      setActionError(err)
    } finally {
      setGenerating(false)
    }
  }

  /* FR-148: accepting writes a new directive-set version, never an edit in place. */
  async function apply(item) {
    setActionError(null)
    setBusyId(item.id)
    try {
      const res = await api.post(`/learning/advice/${item.id}/apply`)
      /* Reloading moves the proposal into the history, so the confirmation and
         the link to the new version have to survive outside the card. */
      setAppliedById((prev) => ({
        ...prev,
        [item.id]: { ...(res.directive_set || {}), headline: item.headline },
      }))
      advice.reload()
    } catch (err) {
      setActionError(err)
    } finally {
      setBusyId(null)
      setConfirmApply(null)
    }
  }

  async function dismiss(item, reason) {
    setActionError(null)
    setBusyId(item.id)
    try {
      await api.post(`/learning/advice/${item.id}/dismiss`, { reason })
      advice.reload()
    } catch (err) {
      setActionError(err)
    } finally {
      setBusyId(null)
      setDismissing(null)
      setDismissReason('')
    }
  }

  return (
    <MarketShell route="insights" journey={journey.data?.journey || {}}>
      <Tabs tabs={OUTCOME_TABS} active={outcome} onChange={setOutcome} />

      {actionError && <ErrorBox error={actionError} onRetry={() => setActionError(null)} />}

      {/* --- 1. The figures ---------------------------------------------- */}

      <FiguresCard
        patterns={patterns}
        noun={noun}
        minToAdvise={MIN_TO_ADVISE}
        outstanding={outstanding}
        onRecompute={() => {
          recomputeRef.current = true
          setRecomputeToken((t) => t + 1)
        }}
      />

      {/* --- 2. The advice ------------------------------------------------ */}

      <div className="card">
        <div className="card-header">
          <h3>
            Where to redirect
            <HelpTip term="redirection_advice" />
          </h3>
          <div className="spacer" />
          <button className="btn btn-primary btn-sm" disabled={generating} onClick={generate}>
            {generating ? <span className="spinner" /> : `Analyse ${noun} outcomes`}
          </button>
        </div>

        {generating && (
          <p className="muted small">
            Computing the segment figures, then asking the model to turn them into advice. The
            figures are calculated first and the model is given them — it is never asked to find
            the pattern itself.
          </p>
        )}

        {shortfall > 0 && (
          <div className="alert alert-info">
            <div>
              Advice is withheld until {MIN_TO_ADVISE} applications have resolved. You have{' '}
              {resolved}; {shortfall} more {shortfall === 1 ? 'needs' : 'need'} to resolve.{' '}
              <Link to="/responses">Record the responses you have</Link> to close the gap.
            </div>
          </div>
        )}

        {genResult?.summary && (
          <div className={`alert ${genResult.insufficient_data ? 'alert-warn' : 'alert-info'}`}>
            <div>
              <strong>{genResult.insufficient_data ? 'Not enough to go on yet.' : 'Summary.'}</strong>{' '}
              {genResult.summary}
              {genResult.insufficient_data && (
                <div style={{ marginTop: 8 }}>
                  <Link className="btn btn-sm" to="/responses">
                    Record outstanding responses{outstanding ? ` (${outstanding})` : ''}
                  </Link>
                </div>
              )}
            </div>
          </div>
        )}

        {/* FR-148: what an accepted proposal actually produced, and where to revert it. */}
        {Object.entries(appliedById).map(([id, set]) => (
          <div className="alert alert-ok" key={id}>
            <div>
              <strong>Applied.</strong> “{set.headline}” created a new version of directive set{' '}
              “{set.name || 'your directives'}”
              {set.version != null ? `, now at version ${set.version}` : ''}. The version you were
              using is untouched — open it to compare or revert.{' '}
              <Link to="/directives">Open directives</Link>
            </div>
          </div>
        ))}

        {advice.loading && <Loading rows={3} />}
        {advice.error && <ErrorBox error={advice.error} onRetry={advice.reload} />}

        {!advice.loading && !advice.error && open.length === 0 && !generating && (
          <Empty title="No open proposals">
            {resolved >= MIN_TO_ADVISE
              ? `Nothing is open. Run the analysis to see whether the ${noun} rates separate by kind of job or company.`
              : 'Once enough applications have resolved, proposals appear here with the figures they rest on.'}
          </Empty>
        )}

        {open.map((item) => (
          <AdviceCard
            key={item.id}
            advice={item}
            conflict={conflictById[item.id]}
            applied={appliedById[item.id]}
            busy={busyId === item.id}
            onApply={() => setConfirmApply(item)}
            onDismiss={() => {
              setDismissReason('')
              setDismissing(item)
            }}
          />
        ))}
      </div>

      {/* --- 3. The history ----------------------------------------------- */}

      <div className="card">
        <div className="card-header">
          <h3>What you already decided</h3>
          <div className="spacer" />
          <Badge>{history.length}</Badge>
        </div>
        {advice.loading ? <Loading rows={2} /> : <AdviceHistory history={history} />}
      </div>

      {/* Applying is an outward-facing change to how the search runs, so it confirms. */}
      {confirmApply && (
        <Modal
          title="Apply this redirection?"
          onClose={() => setConfirmApply(null)}
          actions={
            <>
              <button className="btn" onClick={() => setConfirmApply(null)}>
                Cancel
              </button>
              <button
                className="btn btn-primary"
                disabled={busyId === confirmApply.id}
                onClick={() => apply(confirmApply)}
              >
                Create the new version
              </button>
            </>
          }
        >
          <p>{confirmApply.headline}</p>
          <p className="small muted">
            This writes a <strong>new version</strong> of your directive set (FR-148). The
            version you are using now is left exactly as it is, so a campaign that ran under it
            stays explainable and you can revert by restoring it on the directives screen.
            Nothing is re-collected or re-scored by this change on its own.
          </p>
        </Modal>
      )}

      {dismissing && (
        <Modal
          title="Dismiss this proposal"
          onClose={() => setDismissing(null)}
          actions={
            <>
              <button className="btn" onClick={() => setDismissing(null)}>
                Cancel
              </button>
              <button
                className="btn btn-danger"
                disabled={!dismissReason.trim() || busyId === dismissing.id}
                onClick={() => dismiss(dismissing, dismissReason.trim())}
              >
                Dismiss
              </button>
            </>
          }
        >
          <p>{dismissing.headline}</p>
          <p className="small muted">
            Why are you setting this aside? The reason is kept — it is the only feedback the
            analysis gets about advice that missed.
          </p>
          <div className="chips" style={{ marginBottom: 10 }}>
            {DISMISS_REASONS.map((r) => (
              <span
                key={r}
                className={`chip clickable${dismissReason === r ? ' on' : ''}`}
                onClick={() => setDismissReason(r)}
              >
                {r}
              </span>
            ))}
          </div>
          <textarea
            rows={3}
            value={dismissReason}
            placeholder="Or write your own reason"
            onChange={(e) => setDismissReason(e.target.value)}
          />
        </Modal>
      )}
    </MarketShell>
  )
}
