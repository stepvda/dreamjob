/**
 * The dream-job statement and the structured model it produces (FR-109, FR-128).
 *
 * FR-109 asks for free text in the job seeker's own words and own language,
 * with no length limit. That is why the editor here is a page-sized writing
 * surface rather than a form field: the quality of this text decides the
 * quality of every match, every score and every letter written later, and a
 * three-line box asks for three lines.
 *
 * FR-128 is the other half: the text is turned into a structure that campaign
 * planning (FR-162), company discovery (FR-224), speculative openings (FR-262)
 * and scoring (FR-281, FR-383) all read directly. Because those slices act on
 * it, the job seeker sees it, corrects it and confirms it before it counts —
 * and is told plainly when the model was built from text that has since
 * changed.
 *
 * The statement itself is stored with the profile (`PUT /api/profile/dream-job`
 * versions it, FR-109); the model is stored in `dream_job_model`. Saving here
 * writes both, in that order, so the two never silently diverge.
 */

import { useMemo, useRef, useState } from 'react'

import { api } from '../api/client'
import { FirstRun, HelpTip, ScreenIntro } from '../components/Help'
import Icon from '../components/Icon'
import WorkflowMap, { deriveJourney } from '../components/WorkflowMap'
import { Badge, Empty, ErrorBox, Loading, Modal, formatDate, useFetch } from '../components/ui'
import ModelEditor from './dreamjob/ModelEditor'

const absent = (e) => {
  if (e.status === 404) return null
  throw e
}

/* --- Page ----------------------------------------------------------------- */

export default function DreamJobPage() {
  const { data, error, loading, reload } = useFetch(async () => {
    const [statement, model] = await Promise.all([
      api.get('/profile/dream-job').catch(absent),
      // No model yet is an empty screen, not a failure.
      api.get('/enrichment/dream-job').catch(absent),
    ])
    return { statement: statement?.statement || '', model }
  }, [])

  const [draft, setDraft] = useState(null)
  const [busy, setBusy] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [confirming, setConfirming] = useState(false)
  const editorRef = useRef(null)

  const saved = data?.statement ?? ''
  const model = data?.model ?? null
  const text = draft ?? saved

  const unsaved = text.trim() !== saved.trim()
  // FR-128: the model is only meaningful against the text it was built from.
  const stale = Boolean(model && (model.statement || '').trim() !== saved.trim())

  const words = useMemo(() => text.trim().split(/\s+/).filter(Boolean).length, [text])

  async function run(key, fn) {
    setBusy(key)
    setActionError(null)
    try {
      await fn()
    } catch (e) {
      setActionError(e.message)
    } finally {
      setBusy(null)
    }
  }

  const saveStatement = () =>
    run('save', async () => {
      await api.put('/profile/dream-job', { statement: text })
      setDraft(null)
      reload()
    })

  const build = () =>
    run('build', async () => {
      // Save first, so the model and the FR-109 text always agree afterwards.
      if (unsaved) await api.put('/profile/dream-job', { statement: text })
      await api.post('/enrichment/dream-job', {
        statement: text,
        use_composite: true,
        language: 'en',
      })
      setDraft(null)
      reload()
    })

  if (loading) return <Loading rows={6} />
  if (error) return <ErrorBox error={error} onRetry={reload} />

  return (
    <div className="content-wide">
      <ScreenIntro pathname="/dream-job" />

      <WorkflowMap compact current="dream_job" journey={deriveJourney({ dreamJob: model })} />

      {!saved && !model && (
        <div style={{ marginTop: 14 }}>
          <FirstRun
            pathname="/dream-job"
            action={
              <button
                className="btn btn-primary"
                onClick={() => editorRef.current?.focus()}
              >
                <Icon name="edit" />
                Start writing
              </button>
            }
          />
        </div>
      )}

      {actionError && (
        <div className="alert alert-danger" style={{ marginTop: 12 }}>
          <Icon name="error" size={16} />
          <div>{actionError}</div>
        </div>
      )}

      {/* FR-109: free text, no length limit, in whatever language you think in. */}
      <div className="card phase-edge phase-1" style={{ marginTop: 14 }}>
        <div className="card-header">
          <Icon name="dream" />
          <h3>
            Your statement
            <HelpTip title="Why this matters more than anything else you type">
              Everything downstream reads this: which companies are looked at, how each
              opportunity is scored against what you actually want, and what the motivation
              letter argues. Write it as prose. There is no length limit and no keyword to
              hit.
            </HelpTip>
          </h3>
          <div className="spacer" />
          <span className="small muted">{words ? `${words} words` : 'nothing written yet'}</span>
          {unsaved && <Badge tone="warn">Unsaved</Badge>}
        </div>

        <textarea
          ref={editorRef}
          className="statement-editor"
          value={text}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={
            'The work itself — what you want to be spending your days on, and what you want to stop doing.\n\n' +
            'The organisation — its size, stage, sector, how decisions get made, who you would be working with.\n\n' +
            'The conditions — where, how often, how much travel, how much autonomy.\n\n' +
            'And what would make you turn a role down however good the rest of it looked.'
          }
        />

        <div className="row row-wrap" style={{ marginTop: 12 }}>
          <button
            className="btn btn-sm"
            disabled={!unsaved || busy === 'save'}
            onClick={saveStatement}
          >
            {busy === 'save' ? (
              <span className="spinner" />
            ) : (
              <>
                <Icon name="check" />
                Save the statement
              </>
            )}
          </button>
          {unsaved && draft !== null && (
            <button className="btn btn-sm btn-ghost" onClick={() => setDraft(null)}>
              <Icon name="x" />
              Discard my changes
            </button>
          )}
          <div className="spacer" />
          <button
            className="btn btn-primary btn-sm"
            disabled={!text.trim() || busy === 'build'}
            onClick={build}
          >
            {busy === 'build' ? (
              <span className="spinner" />
            ) : model ? (
              <>
                <Icon name="refresh" />
                Rebuild the model from this text
              </>
            ) : (
              <>
                <Icon name="sparkle" />
                Build the structured model
              </>
            )}
          </button>
        </div>
      </div>

      {/* FR-128: a model built from text the job seeker has since rewritten is
          worse than no model, because planning and scoring read it silently. */}
      {(stale || unsaved) && model && (
        <div className="alert alert-warn" style={{ marginTop: 14 }}>
          <Icon name="warning" size={16} />
          <div style={{ flex: 1 }}>
            <strong>The model no longer matches your statement.</strong>{' '}
            {unsaved
              ? 'You have unsaved changes above.'
              : `It was built on ${formatDate(model.created_at)} from an earlier version of the text.`}{' '}
            Campaign planning and scoring read the model, not the text, so rebuild it before
            you launch anything.
            <HelpTip term="model_drift" />
            <div style={{ marginTop: 10 }}>
              <button className="btn btn-sm" disabled={busy === 'build'} onClick={build}>
                <Icon name="refresh" />
                Rebuild it now
              </button>
            </div>
          </div>
        </div>
      )}

      {model ? (
        <ModelEditor
          model={model}
          stale={stale}
          busy={busy}
          onPatch={(patch) =>
            run('patch', async () => {
              await api.patch(`/enrichment/dream-job/${model.id}`, patch)
              reload()
            })
          }
          onConfirm={() => setConfirming(true)}
        />
      ) : (
        saved && (
          <div className="phase-1" style={{ marginTop: 14 }}>
            <Empty
              title={
                <>
                  <Icon name="composite" /> No structured model yet
                </>
              }
            >
              Your statement is saved, but nothing has read it yet. Build the model so the
              search knows what to look for and the ranking knows what you actually want.
            </Empty>
          </div>
        )
      )}

      {confirming && (
        <Modal
          title="Confirm this model?"
          onClose={() => setConfirming(false)}
          actions={
            <>
              <button className="btn btn-sm" onClick={() => setConfirming(false)}>
                <Icon name="clock" />
                Not yet
              </button>
              <button
                className="btn btn-sm btn-primary"
                onClick={() => {
                  setConfirming(false)
                  run('confirm', async () => {
                    await api.post(`/enrichment/dream-job/${model.id}/confirm`)
                    reload()
                  })
                }}
              >
                <Icon name="check" />
                Confirm — use it for matching
              </button>
            </>
          }
        >
          <p className="small">
            Scoring and generation prefer a confirmed model, so this is the point at which it
            starts to affect real results.
          </p>
          <p className="small muted">
            Check the deal-breakers in particular: a hard one vetoes an opportunity outright,
            so a wrong entry costs you roles you would have wanted to see.
          </p>
        </Modal>
      )}
    </div>
  )
}
