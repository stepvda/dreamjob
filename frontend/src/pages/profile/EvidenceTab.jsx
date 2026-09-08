/**
 * Evidence tab (FR-441).
 *
 * A claim with a link behind it reads differently from a claim without one.
 * Evidence items are attached to normalised skills so a tailored CV can cite
 * the repository, paper or talk that backs the skill it is leading with,
 * rather than asserting it.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import {
  Badge,
  ChipSelect,
  Empty,
  ErrorBox,
  Field,
  Loading,
  Modal,
  Provenance,
  formatDate,
  useFetch,
} from '../../components/ui'

/** Mirrors repo.EVIDENCE_KINDS in backend/dreamjob/db/repositories/profiles.py. */
const KINDS = [
  { value: 'repository', label: 'Repository' },
  { value: 'publication', label: 'Publication' },
  { value: 'talk', label: 'Talk' },
  { value: 'case_study', label: 'Case study' },
  { value: 'article', label: 'Article' },
  { value: 'reference', label: 'Reference' },
  { value: 'certificate', label: 'Certificate' },
]

const BLANK = { kind: 'repository', title: '', url: '', description: '', linked_skills: [] }

export default function EvidenceTab({ profile }) {
  const items = useFetch(() => api.get('/profile/evidence'), [])
  const skills = useFetch(() => api.get('/profile/skills'), [profile?.id])

  const [draft, setDraft] = useState(BLANK)
  const [adding, setAdding] = useState(false)
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState(null)
  const [deleting, setDeleting] = useState(null)

  const skillOptions = (skills.data || [])
    .map((s) => s.normalised_label)
    .filter(Boolean)
    .filter((v, i, a) => a.indexOf(v) === i)

  async function create() {
    setFailure(null)
    setBusy(true)
    try {
      await api.post('/profile/evidence', {
        kind: draft.kind,
        title: draft.title.trim(),
        url: draft.url.trim() || null,
        description: draft.description.trim() || null,
        linked_skills: draft.linked_skills,
      })
      setDraft(BLANK)
      setAdding(false)
      items.reload()
    } catch (err) {
      setFailure(err)
    } finally {
      setBusy(false)
    }
  }

  async function attach(item, file) {
    if (!file) return
    setFailure(null)
    const body = new FormData()
    body.append('file', file)
    try {
      await api.upload(`/profile/evidence/${item.id}/file`, body)
      items.reload()
    } catch (err) {
      setFailure(err)
    }
  }

  async function remove(item) {
    setDeleting(null)
    try {
      await api.del(`/profile/evidence/${item.id}`)
      items.reload()
    } catch (err) {
      setFailure(err)
    }
  }

  return (
    <div className="col" style={{ gap: 14 }}>
      {failure && <ErrorBox error={failure} onRetry={() => setFailure(null)} />}

      <div className="row">
        <span className="small muted">
          Proof you can point at
          <HelpTip term="evidence_item" />
        </span>
        <div className="spacer" />
        <button className="btn btn-primary btn-sm" onClick={() => setAdding(true)}>
          Add evidence
        </button>
      </div>

      {items.loading && <Loading rows={3} />}
      {items.error && <ErrorBox error={items.error} onRetry={items.reload} />}

      {!items.loading && !items.error && (items.data || []).length === 0 && (
        <Empty
          title="No evidence yet"
          action={
            <button className="btn btn-primary" onClick={() => setAdding(true)}>
              Add your first item
            </button>
          }
        >
          A repository, a paper, a conference talk, a certificate. Each one is linked to a skill
          and is cited with its link when a tailored CV leans on that skill.
        </Empty>
      )}

      {(items.data || []).length > 0 && (
        <div className="grid grid-2">
          {items.data.map((item) => (
            <div className="card" key={item.id}>
              <div className="card-header">
                <Badge tone="info">{KINDS.find((k) => k.value === item.kind)?.label || item.kind}</Badge>
                <strong>{item.title}</strong>
                <div className="spacer" />
                <Badge tone={item.verification_status === 'verified' ? 'ok' : undefined}>
                  {item.verification_status || 'unverified'}
                </Badge>
              </div>

              {item.url && (
                <div className="small" style={{ marginBottom: 6 }}>
                  <a href={item.url} target="_blank" rel="noreferrer noopener">
                    {item.url}
                  </a>{' '}
                  <Provenance source={item.url} />
                </div>
              )}

              {item.description && <p className="small">{item.description}</p>}

              {(item.linked_skills || []).length > 0 && (
                <div className="chips" style={{ marginBottom: 8 }}>
                  {item.linked_skills.map((s) => (
                    <span className="chip on" key={s}>
                      {s}
                    </span>
                  ))}
                </div>
              )}

              <div className="row row-wrap small muted">
                <span>added {formatDate(item.created_at)}</span>
                {item.file_path && <Badge tone="ok">file attached</Badge>}
                <div className="spacer" />
                <label className="btn btn-sm" style={{ cursor: 'pointer' }}>
                  Attach file
                  <input
                    type="file"
                    hidden
                    onChange={(e) => {
                      attach(item, e.target.files?.[0])
                      e.target.value = ''
                    }}
                  />
                </label>
                <button className="btn btn-sm btn-danger" onClick={() => setDeleting(item)}>
                  Delete
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {adding && (
        <Modal
          title="Add evidence"
          onClose={() => setAdding(false)}
          actions={
            <>
              <button className="btn" onClick={() => setAdding(false)}>
                Cancel
              </button>
              <button
                className="btn btn-primary"
                disabled={!draft.title.trim() || busy}
                onClick={create}
              >
                {busy ? 'Saving…' : 'Add'}
              </button>
            </>
          }
        >
          <Field label="Kind">
            <select value={draft.kind} onChange={(e) => setDraft({ ...draft, kind: e.target.value })}>
              {KINDS.map((k) => (
                <option key={k.value} value={k.value}>
                  {k.label}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Title">
            <input
              type="text"
              value={draft.title}
              onChange={(e) => setDraft({ ...draft, title: e.target.value })}
              autoFocus
            />
          </Field>

          <Field label="Link" hint="A public URL a reader can open. Optional, but it is what makes this evidence.">
            <input
              type="url"
              value={draft.url}
              onChange={(e) => setDraft({ ...draft, url: e.target.value })}
              placeholder="https://"
            />
          </Field>

          <Field label="What it shows">
            <textarea
              rows={3}
              value={draft.description}
              onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            />
          </Field>

          <Field
            label="Skills it backs"
            hint="Pick from your normalised skills, or type one that is not there yet."
          >
            <ChipSelect
              options={skillOptions}
              value={draft.linked_skills}
              onChange={(v) => setDraft({ ...draft, linked_skills: v })}
              allowCustom
            />
          </Field>
        </Modal>
      )}

      {deleting && (
        <Modal
          title={`Delete “${deleting.title}”?`}
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
            The item and any attached file are removed. Documents already generated keep the
            citation they were written with.
          </p>
        </Modal>
      )}
    </div>
  )
}
