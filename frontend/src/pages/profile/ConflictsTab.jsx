/**
 * Conflicts tab (FR-103).
 *
 * When the LinkedIn export and the CV disagree, the merge does not choose. It
 * records both values and asks. Until every disagreement is settled the
 * composite profile is built on facts the system knows are contested, which is
 * why an unresolved row here is styled as a blocker rather than a to-do.
 */

import { useEffect, useState } from 'react'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, Empty, ErrorBox, Loading, Modal } from '../../components/ui'

const UNRESOLVED = (c) => !c.resolution || c.resolution === 'unresolved'

export default function ConflictsTab({ conflicts, loading, error, reload, hasProfile, onNewVersion }) {
  const [rows, setRows] = useState([])
  const [drafts, setDrafts] = useState({})
  const [saving, setSaving] = useState(null)
  const [failure, setFailure] = useState(null)
  const [confirming, setConfirming] = useState(false)
  const [applied, setApplied] = useState(null)

  useEffect(() => {
    setRows(conflicts || [])
  }, [conflicts])

  async function choose(conflict, resolution, resolvedValue) {
    setFailure(null)
    setSaving(conflict.id)
    try {
      const next = await api.post(`/profile/conflicts/${conflict.id}/resolve`, {
        resolution,
        resolved_value: resolution === 'manual' ? resolvedValue : null,
      })
      setRows((current) => current.map((c) => (c.id === next.id ? next : c)))
    } catch (err) {
      setFailure(err)
    } finally {
      setSaving(null)
    }
  }

  async function apply() {
    setFailure(null)
    setConfirming(false)
    try {
      const version = await api.post('/profile/conflicts/apply')
      setApplied(version.version)
      onNewVersion(version)
      reload()
    } catch (err) {
      setFailure(err)
    }
  }

  const open = rows.filter(UNRESOLVED)
  const settled = rows.filter((c) => !UNRESOLVED(c))

  if (loading) return <Loading rows={4} />
  if (error) return <ErrorBox error={error} onRetry={reload} />

  if (!rows.length) {
    return (
      <Empty
        title={
          <>
            <span className="icon-chip icon-chip-lg phase-chip" style={{ display: 'flex' }}>
              <Icon name={hasProfile ? 'success' : 'document'} />
            </span>
            {hasProfile ? 'Nothing is contested' : 'No documents to compare yet'}
          </>
        }
      >
        {hasProfile
          ? 'Your LinkedIn export and your CV agree on every field the merge could pair up, so there is nothing here for you to decide.'
          : 'Conflicts appear once both a LinkedIn export and a CV have been imported — they are the disagreements between the two.'}
      </Empty>
    )
  }

  return (
    <div className="col" style={{ gap: 14 }}>
      {failure && <ErrorBox error={failure} onRetry={() => setFailure(null)} />}

      {applied != null && (
        <div className="alert alert-ok">
          <Icon name="success" />
          <div>
            <strong>Written into version {applied}.</strong> Your decisions are now part of the
            profile the rest of the system reads.
          </div>
        </div>
      )}

      {/* FR-103: an unresolved conflict is a blocker, not a suggestion. */}
      {open.length > 0 && (
        <div className="alert alert-danger">
          <Icon name="warning" />
          <div>
            <strong>
              {open.length} disagreement{open.length === 1 ? '' : 's'} still to settle.
            </strong>{' '}
            Until you decide, the composite profile
            <HelpTip term="composite_profile" /> carries a value the system knows is contested —
            and a tailored CV can repeat it to an employer.
          </div>
        </div>
      )}

      <div className="row">
        <span className="small muted">
          {settled.length} of {rows.length} settled
          <HelpTip term="merge_conflict" />
        </span>
        <div className="spacer" />
        <button
          className="btn btn-primary btn-sm"
          disabled={!settled.length}
          onClick={() => (open.length ? setConfirming(true) : apply())}
        >
          <Icon name="check" />
          Write decisions into a new version
        </button>
      </div>

      <div>
        {rows.map((c) => (
          <ConflictRow
            key={c.id}
            conflict={c}
            saving={saving === c.id}
            draft={drafts[c.id] ?? c.resolved_value ?? ''}
            onDraft={(v) => setDrafts((d) => ({ ...d, [c.id]: v }))}
            onChoose={choose}
          />
        ))}
      </div>

      {confirming && (
        <Modal
          title="Some conflicts are still open"
          onClose={() => setConfirming(false)}
          actions={
            <>
              <button className="btn" onClick={() => setConfirming(false)}>
                Keep deciding
              </button>
              <button className="btn btn-primary" onClick={apply}>
                <Icon name="check" />
                Apply the {settled.length} settled
              </button>
            </>
          }
        >
          <p>
            {open.length} field{open.length === 1 ? '' : 's'} will keep the merged value the
            system guessed, and stay flagged as contested. You can come back and settle them at
            any time — applying again writes another version.
          </p>
        </Modal>
      )}
    </div>
  )
}

function ConflictRow({ conflict, saving, draft, onDraft, onChoose }) {
  const unresolved = UNRESOLVED(conflict)

  return (
    <div className={`conflict ${unresolved ? 'unresolved' : 'settled'}`}>
      <div className="row row-wrap">
        <span className="field-path">{conflict.field_path}</span>
        <div className="spacer" />
        {saving && <span className="spinner" />}
        {unresolved ? (
          <Badge tone="danger">
            <Icon name="warning" />
            needs a decision
          </Badge>
        ) : (
          <Badge tone="ok">
            <Icon name="check" />
            {conflict.resolution}
          </Badge>
        )}
      </div>

      <div className="conflict-values">
        <label className={`conflict-value${conflict.resolution === 'linkedin' ? ' chosen' : ''}`}>
          <input
            type="radio"
            name={`conflict-${conflict.id}`}
            checked={conflict.resolution === 'linkedin'}
            onChange={() => onChoose(conflict, 'linkedin')}
          />
          <span>
            <span className="src">LinkedIn export</span>
            <span className="val">{conflict.value_linkedin ?? <em className="muted">absent</em>}</span>
          </span>
        </label>

        <label className={`conflict-value${conflict.resolution === 'cv' ? ' chosen' : ''}`}>
          <input
            type="radio"
            name={`conflict-${conflict.id}`}
            checked={conflict.resolution === 'cv'}
            onChange={() => onChoose(conflict, 'cv')}
          />
          <span>
            <span className="src">CV</span>
            <span className="val">{conflict.value_cv ?? <em className="muted">absent</em>}</span>
          </span>
        </label>
      </div>

      <div className="row row-wrap" style={{ gap: 8 }}>
        <label className="checkline" style={{ flexShrink: 0 }}>
          <input
            type="radio"
            name={`conflict-${conflict.id}`}
            checked={conflict.resolution === 'manual'}
            onChange={() => draft.trim() && onChoose(conflict, 'manual', draft.trim())}
          />
          Neither — use
        </label>
        <input
          type="text"
          style={{ flex: 1, minWidth: 180 }}
          value={draft}
          placeholder="the value you want instead"
          onChange={(e) => onDraft(e.target.value)}
        />
        <button
          className="btn btn-sm"
          disabled={!draft.trim() || saving}
          onClick={() => onChoose(conflict, 'manual', draft.trim())}
        >
          <Icon name="check" />
          Use this
        </button>
      </div>

      {conflict.resolution === 'manual' && conflict.resolved_value && (
        <p className="small muted" style={{ margin: '6px 0 0' }}>
          Stored as “{conflict.resolved_value}”.
        </p>
      )}
    </div>
  )
}
