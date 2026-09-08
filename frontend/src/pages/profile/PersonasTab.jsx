/**
 * Personas tab (FR-442).
 *
 * One career often supports more than one honest story. A persona is a slant
 * on the same profile — an emphasis, its own dream-job statement and its own
 * directive defaults — so a search run "as an architect" and a search run "as
 * a product leader" do not have to be two accounts.
 *
 * Exactly one persona is the default; the backend clears the others when one
 * is promoted, so the control here is a promotion rather than a toggle.
 */

import { useState } from 'react'

import { HelpTip } from '../../components/Help'
import { api } from '../../api/client'
import {
  Badge,
  Empty,
  ErrorBox,
  Field,
  Loading,
  Modal,
  formatDate,
  useFetch,
} from '../../components/ui'

const BLANK = { name: '', emphasis: '', dream_job_statement: '' }

export default function PersonasTab() {
  const personas = useFetch(() => api.get('/profile/personas'), [])
  const [draft, setDraft] = useState(BLANK)
  const [adding, setAdding] = useState(false)
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState(null)
  const [deleting, setDeleting] = useState(null)

  async function create() {
    setFailure(null)
    setBusy(true)
    try {
      await api.post('/profile/personas', {
        name: draft.name.trim(),
        emphasis: draft.emphasis.trim() || null,
        dream_job_statement: draft.dream_job_statement.trim() || null,
        directive_defaults: {},
        is_default: false,
      })
      setDraft(BLANK)
      setAdding(false)
      personas.reload()
    } catch (err) {
      setFailure(err)
    } finally {
      setBusy(false)
    }
  }

  async function makeDefault(persona) {
    setFailure(null)
    try {
      await api.post(`/profile/personas/${persona.id}/default`)
      personas.reload()
    } catch (err) {
      setFailure(err)
    }
  }

  async function remove(persona) {
    setDeleting(null)
    try {
      await api.del(`/profile/personas/${persona.id}`)
      personas.reload()
    } catch (err) {
      setFailure(err)
    }
  }

  const rows = personas.data || []

  return (
    <div className="col" style={{ gap: 14 }}>
      {failure && <ErrorBox error={failure} onRetry={() => setFailure(null)} />}

      <div className="row">
        <span className="small muted">
          One career, more than one honest story
          <HelpTip term="persona" />
        </span>
        <div className="spacer" />
        <button className="btn btn-primary btn-sm" onClick={() => setAdding(true)}>
          New persona
        </button>
      </div>

      {personas.loading && <Loading rows={3} />}
      {personas.error && <ErrorBox error={personas.error} onRetry={personas.reload} />}

      {!personas.loading && !personas.error && rows.length === 0 && (
        <Empty
          title="No personas yet"
          action={
            <button className="btn btn-primary" onClick={() => setAdding(true)}>
              Create one
            </button>
          }
        >
          You do not need a persona to use the system — without one your profile is used as it
          stands. Create one when you want to run a search under a different emphasis without
          rewriting your profile.
        </Empty>
      )}

      {rows.length > 0 && (
        <div className="grid grid-2">
          {rows.map((p) => (
            <div className="card" key={p.id}>
              <div className="card-header">
                <h3>{p.name}</h3>
                {p.is_default ? <Badge tone="ok">default</Badge> : null}
                <div className="spacer" />
                {!p.is_default && (
                  <button className="btn btn-sm" onClick={() => makeDefault(p)}>
                    Make default
                  </button>
                )}
                <button className="btn btn-sm btn-danger" onClick={() => setDeleting(p)}>
                  Delete
                </button>
              </div>

              {p.emphasis ? (
                <p className="small">{p.emphasis}</p>
              ) : (
                <p className="small muted">No emphasis written.</p>
              )}

              {p.dream_job_statement && (
                <div className="small" style={{ marginBottom: 8 }}>
                  <strong>Dream job for this persona</strong>
                  <p style={{ margin: '4px 0 0' }}>{p.dream_job_statement}</p>
                </div>
              )}

              <div className="small muted">created {formatDate(p.created_at)}</div>
            </div>
          ))}
        </div>
      )}

      {adding && (
        <Modal
          title="New persona"
          onClose={() => setAdding(false)}
          actions={
            <>
              <button className="btn" onClick={() => setAdding(false)}>
                Cancel
              </button>
              <button
                className="btn btn-primary"
                disabled={!draft.name.trim() || busy}
                onClick={create}
              >
                {busy ? 'Creating…' : 'Create'}
              </button>
            </>
          }
        >
          <Field label="Name" hint="How you will recognise it in a campaign — “Platform architect”.">
            <input
              type="text"
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              autoFocus
            />
          </Field>

          <Field
            label="Emphasis"
            hint="Which part of your experience this persona leads with, and what it plays down."
          >
            <textarea
              rows={3}
              value={draft.emphasis}
              onChange={(e) => setDraft({ ...draft, emphasis: e.target.value })}
            />
          </Field>

          <Field
            label="Dream-job statement for this persona"
            hint="Optional. Leave it empty to reuse the statement on the Dream job screen."
          >
            <textarea
              rows={4}
              value={draft.dream_job_statement}
              onChange={(e) => setDraft({ ...draft, dream_job_statement: e.target.value })}
            />
          </Field>

          {/* FR-442: the first persona created becomes the default automatically. */}
          <p className="small muted">
            {rows.length === 0
              ? 'This will become your default persona, because it is the first one.'
              : 'Your current default stays the default until you promote this one.'}
          </p>
        </Modal>
      )}

      {deleting && (
        <Modal
          title={`Delete “${deleting.name}”?`}
          onClose={() => setDeleting(null)}
          actions={
            <>
              <button className="btn" onClick={() => setDeleting(null)}>
                Cancel
              </button>
              <button className="btn btn-danger" onClick={() => remove(deleting)}>
                Delete
              </button>
            </>
          }
        >
          <p>
            Profile versions saved under this persona keep their history. If this was the
            default, another persona is promoted in its place.
          </p>
        </Modal>
      )}
    </div>
  )
}
