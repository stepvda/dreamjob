/**
 * Search directives (FR-141..149, FR-385).
 *
 * FR-141 is the whole shape of this screen: directives are set with drop-downs,
 * multi-select chips, auto-complete and sliders, in five groups, with exactly
 * one free-text field — "notes to the AI". Everything here posts back as the
 * `DirectiveSetPayload` defined in `dreamjob.pipeline.directives`, so the field
 * names on this page are that model's field names and nothing else.
 *
 * The estimate (FR-147) sits beside the controls rather than behind a button on
 * the campaign screen, because the question it answers — "how much would this
 * collect?" — is only useful while you can still change the answer.
 */

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { HelpTip, ScreenIntro } from '../components/Help'
import Icon from '../components/Icon'
import WorkflowMap, { deriveJourney } from '../components/WorkflowMap'
import { ErrorBox, Loading, useFetch } from '../components/ui'
import { useSession } from '../session'
import { DeleteDialog, DuplicateDialog, VersionsDialog } from './directives/dialogs'
import DiscretionCard from './directives/discretion'
import {
  CompanyTypeCard,
  CompensationCard,
  JobContentCard,
  LocationCard,
  WorkArrangementCard,
} from './directives/groups'
import { blankDraft, fromServer, toPayload } from './directives/model'
import { EstimatePanel, SavedSets } from './directives/sidebar'
import { EditorHeader, StartState } from './directives/shell'

/* --- Page ------------------------------------------------------------------ */

export default function DirectivesPage() {
  const { setSession } = useSession()
  const vocabQ = useFetch(() => api.get('/directives/vocabulary?locale=en'), [])
  const setsQ = useFetch(() => api.get('/directives/'), [])

  const [draft, setDraft] = useState(null)
  const [current, setCurrent] = useState(null)   // the saved set the draft came from
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState(null)
  const [savedNote, setSavedNote] = useState(null)
  const [needsVersion, setNeedsVersion] = useState(false)   // FR-148: PUT refused
  const [proposeError, setProposeError] = useState(null)
  const [proposing, setProposing] = useState(false)
  const [dialog, setDialog] = useState(null)                // {kind, set, name}
  const [versions, setVersions] = useState(null)
  const [journey, setJourney] = useState(null)

  const vocab = vocabQ.data

  /* Where this screen sits in the ten-stage pipeline. Falls back to what the
     page itself knows if the overview endpoint is unavailable. */
  useEffect(() => {
    let live = true
    api
      .get('/overview/journey')
      .then((r) => live && setJourney(r?.journey || null))
      .catch(() => {})
    return () => {
      live = false
    }
  }, [])

  /* --- FR-147 estimate, recomputed as the directives change ---------------- */
  const [estimate, setEstimate] = useState(null)
  const [estBusy, setEstBusy] = useState(false)
  const [estError, setEstError] = useState(null)
  const [estNonce, setEstNonce] = useState(0)

  useEffect(() => {
    if (!draft) return undefined
    let live = true
    const t = setTimeout(async () => {
      setEstBusy(true)
      setEstError(null)
      try {
        const res = await api.post('/directives/estimate', { directives: toPayload(draft) })
        if (live) setEstimate(res)
      } catch (e) {
        if (live) {
          setEstError(e)
          setEstimate(null)
        }
      } finally {
        if (live) setEstBusy(false)
      }
    }, 700)
    return () => {
      live = false
      clearTimeout(t)
    }
  }, [draft, estNonce])

  /* --- Actions ------------------------------------------------------------- */

  function edit(patch) {
    setDraft((d) => ({ ...d, ...patch }))
    setSavedNote(null)
  }

  function editGroup(key, patch) {
    setDraft((d) => ({ ...d, [key]: { ...d[key], ...patch } }))
    setSavedNote(null)
  }

  function load(set) {
    setCurrent(set)
    setDraft(fromServer(set))
    setNeedsVersion(false)
    setSaveError(null)
    setSavedNote(null)
  }

  /** FR-147: pre-fill from the composite profile. Nothing is saved by /propose. */
  async function propose() {
    setProposing(true)
    setProposeError(null)
    try {
      const res = await api.post('/directives/propose', { name: 'Proposed directives', geocode: true })
      setCurrent(null)
      setDraft(fromServer(res.directives))
      setSavedNote(
        res.unresolved?.length
          ? `Pre-filled from your profile. ${res.unresolved.length} settings could not be derived and are still empty.`
          : 'Pre-filled from your profile. Review every group before saving.',
      )
    } catch (e) {
      setProposeError(e)
    } finally {
      setProposing(false)
    }
  }

  async function save({ asVersion = false } = {}) {
    setSaving(true)
    setSaveError(null)
    setNeedsVersion(false)
    try {
      const payload = toPayload(draft)
      let saved
      if (asVersion && current?.id) {
        saved = await api.post(`/directives/${current.id}/versions`, payload)
      } else if (current?.id) {
        saved = await api.put(`/directives/${current.id}`, payload)
      } else {
        saved = await api.post('/directives/', payload)
      }
      setCurrent(saved)
      setDraft(fromServer(saved))
      setSavedNote(`Saved as “${saved.name}” version ${saved.version}.`)
      setsQ.reload()
      refreshSession()
    } catch (e) {
      // FR-148: a set a campaign has used is versioned rather than overwritten.
      if (e.status === 409) setNeedsVersion(true)
      setSaveError(e)
    } finally {
      setSaving(false)
    }
  }

  /** FR-385: the shell marks every screen from `session.discretion_mode`, so a
   *  change here has to reach the session and not only this editor. Re-read
   *  rather than patched: which set is *in force* is the server's answer, not
   *  this screen's. A failure is swallowed - the save itself succeeded, and the
   *  indicator catches up on the next load. */
  async function refreshSession() {
    try {
      setSession(await api.get('/auth/me'))
    } catch {
      /* the indicator is not worth failing a successful save over */
    }
  }

  async function runDialog() {
    const d = dialog
    setDialog(null)
    try {
      if (d.kind === 'duplicate') {
        const copy = await api.post(`/directives/${d.set.id}/duplicate`, { name: d.name })
        setsQ.reload()
        load(copy)
      } else if (d.kind === 'delete') {
        await api.del(`/directives/${d.set.id}`)
        setsQ.reload()
        if (current?.id === d.set.id) {
          setCurrent(null)
          setDraft(null)
        }
      }
    } catch (e) {
      setSaveError(e)
    }
  }

  async function showVersions(set) {
    setVersions({ name: set.name, rows: null, error: null })
    try {
      const rows = await api.get(`/directives/${set.id}/versions`)
      setVersions({ name: set.name, rows, error: null })
    } catch (e) {
      setVersions({ name: set.name, rows: [], error: e })
    }
  }

  /* --- Render -------------------------------------------------------------- */

  const sets = setsQ.data
  const showFirstRun = !draft && !setsQ.loading && !setsQ.error && (sets?.length ?? 0) === 0

  return (
    <>
      <WorkflowMap
        compact
        current="directives"
        journey={journey || deriveJourney({ directives: current })}
      />

      <ScreenIntro pathname="/directives" />

      {vocabQ.loading && <Loading rows={4} />}
      {vocabQ.error && <ErrorBox error={vocabQ.error} onRetry={vocabQ.reload} />}

      {!vocabQ.loading && !vocabQ.error && (
        <div className="dir-layout">
          <div className="col" style={{ gap: 0 }}>
            {proposeError && <ErrorBox error={proposeError} onRetry={propose} />}

            {!draft && (
              <StartState
                isFirstRun={showFirstRun}
                proposing={proposing}
                onPropose={propose}
                onBlank={() => setDraft(blankDraft())}
              />
            )}

            {draft && (
              <>
                <EditorHeader
                  draft={draft}
                  current={current}
                  savedNote={savedNote}
                  saveError={saveError}
                  needsVersion={needsVersion}
                  onRename={(name) => edit({ name })}
                  onSaveVersion={() => save({ asVersion: true })}
                />

                <JobContentCard
                  value={draft.job_content}
                  onChange={(p) => editGroup('job_content', p)}
                  vocab={vocab}
                  locale={vocab?.locale}
                />
                <CompanyTypeCard
                  value={draft.company_type}
                  onChange={(p) => editGroup('company_type', p)}
                  vocab={vocab}
                />
                <LocationCard
                  value={draft.location}
                  onChange={(p) => editGroup('location', p)}
                  vocab={vocab}
                  locale={vocab?.locale}
                />
                <WorkArrangementCard
                  value={draft.work_arrangement}
                  onChange={(p) => editGroup('work_arrangement', p)}
                  vocab={vocab}
                />
                <CompensationCard
                  value={draft.compensation}
                  onChange={(p) => editGroup('compensation', p)}
                  vocab={vocab}
                />

                <DiscretionCard
                  value={draft}
                  onChange={edit}
                  vocab={vocab}
                  savedId={current?.id}
                />

                {/* FR-141: the one and only free-text field on this screen. */}
                <div className="card phase-2 phase-edge">
                  <div className="card-header">
                    <Icon name="sparkle" />
                    <h3>Notes to the AI</h3>
                    <HelpTip title="The only free-text field">
                      Everything else on this screen is structured so that each source can be
                      queried in its own language. This box is read by the model when it scores
                      and writes, not by the source adapters — nuance the controls cannot carry
                      belongs here, hard constraints do not.
                    </HelpTip>
                    <div className="spacer" />
                    <span className="dir-summary">
                      {(draft.notes_to_ai || '').length} / 4000
                    </span>
                  </div>
                  <textarea
                    value={draft.notes_to_ai || ''}
                    maxLength={4000}
                    rows={5}
                    placeholder="For example: I would rather join a team rebuilding something than one maintaining it, and I care more about the people than the sector."
                    onChange={(e) => edit({ notes_to_ai: e.target.value })}
                  />

                  <div className="checkline" style={{ marginTop: 14 }}>
                    <input
                      id="dir-spontaneous-only"
                      type="checkbox"
                      checked={Boolean(draft.spontaneous_only)}
                      onChange={(e) => edit({ spontaneous_only: e.target.checked })}
                    />
                    <label htmlFor="dir-spontaneous-only">
                      <Icon name="speculative" /> Speculative openings only — do not collect
                      advertised vacancies
                    </label>
                    <HelpTip term="speculative_opening" align="right" />
                  </div>
                  <span className="small muted">
                    FR-149. This narrows the sources the estimate counts, so the panel beside this
                    changes as soon as you tick it.
                  </span>
                </div>
              </>
            )}
          </div>

          <aside className="dir-side">
            {draft && (
              <div className="card phase-2 phase-edge">
                <div className="col">
                  <button
                    className="btn btn-primary btn-lg"
                    onClick={() => save()}
                    disabled={saving || !draft.name.trim()}
                  >
                    {saving ? (
                      <span className="spinner" />
                    ) : (
                      <>
                        <Icon name="check" />
                        {current ? 'Save changes' : 'Save directive set'}
                      </>
                    )}
                  </button>
                  {current && (
                    <button className="btn" onClick={() => save({ asVersion: true })} disabled={saving}>
                      <Icon name="copy" /> Save as new version
                    </button>
                  )}
                  <Link className="btn btn-ghost" to="/campaigns">
                    <Icon name="campaign" /> Use these directives in a campaign →
                  </Link>
                </div>
              </div>
            )}

            <EstimatePanel
              estimate={estimate}
              busy={estBusy}
              error={estError}
              onRetry={() => setEstNonce((n) => n + 1)}
            />

            <SavedSets
              sets={sets}
              loading={setsQ.loading}
              error={setsQ.error}
              onRetry={setsQ.reload}
              currentId={current?.id}
              onLoad={load}
              onVersions={showVersions}
              onDuplicate={(s) => setDialog({ kind: 'duplicate', set: s, name: `${s.name} (copy)` })}
              onDelete={(s) => setDialog({ kind: 'delete', set: s })}
            />
          </aside>
        </div>
      )}

      {dialog?.kind === 'duplicate' && (
        <DuplicateDialog
          dialog={dialog}
          onChange={setDialog}
          onConfirm={runDialog}
          onClose={() => setDialog(null)}
        />
      )}

      {dialog?.kind === 'delete' && (
        <DeleteDialog dialog={dialog} onConfirm={runDialog} onClose={() => setDialog(null)} />
      )}

      {versions && (
        <VersionsDialog
          versions={versions}
          onClose={() => setVersions(null)}
          onLoad={(v) => {
            load(v)
            setVersions(null)
          }}
        />
      )}

    </>
  )
}
