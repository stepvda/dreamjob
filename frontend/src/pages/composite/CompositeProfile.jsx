/**
 * The composite profile itself, statement by statement (FR-121, FR-125).
 *
 * FR-125 is the whole reason this renders as a list of lines rather than as
 * prose: every statement must name the source that supports it, and a
 * statement the backend could not trace is flagged rather than quietly
 * presented as fact. `composite.statements` is the backend's own flattening of
 * the stored blocks - {id, block, text, source} - and `unsupported_statements`
 * is the list of ids it could not vouch for.
 */

import { useMemo, useState } from 'react'

import { FirstRun, HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, Provenance, formatDate } from '../../components/ui'

/** composite_profile columns, in the order the profile reads best. */
const BLOCKS = [
  { key: 'narrative', label: 'Narrative', kind: 'text', icon: 'document' },
  { key: 'seniority', label: 'Seniority', kind: 'object', icon: 'chart' },
  { key: 'career_trajectory', label: 'Career trajectory', kind: 'list', icon: 'trendUp' },
  { key: 'core_competencies', label: 'Core competencies', kind: 'list', icon: 'target' },
  { key: 'adjacent_competencies', label: 'Adjacent competencies', kind: 'list', icon: 'plus' },
  { key: 'domains', label: 'Domains', kind: 'list', icon: 'browser' },
  { key: 'achievements', label: 'Achievements', kind: 'list', icon: 'dream' },
  { key: 'public_footprint', label: 'Public footprint', kind: 'list', icon: 'link' },
  { key: 'inferred_preferences', label: 'Inferred preferences', kind: 'list', icon: 'sparkle' },
  { key: 'constraints', label: 'Constraints', kind: 'list', icon: 'lock' },
]

/** pipeline/composite.py SOURCE_TYPES, in the wording a person would use. */
const SOURCE_LABEL = {
  user_input: 'You said so',
  linkedin_export: 'LinkedIn export',
  cv: 'Your CV',
  profile: 'Your profile',
  web: 'Web page',
  unsupported: 'No verified source',
}

export default function CompositeProfile({ composite, findings, busy, onBuild, onPatch }) {
  const [editing, setEditing] = useState(null)

  // A web citation is only as strong as the finding it points at, so the
  // identity score of that finding is the confidence shown on the marker.
  const scoreByUrl = useMemo(
    () => Object.fromEntries(findings.map((f) => [f.url, f.identity_score])),
    [findings],
  )

  if (!composite) {
    return (
      <FirstRun
        pathname="/composite"
        action={
          <button className="btn btn-primary" disabled={busy === 'build'} onClick={onBuild}>
            {busy === 'build' ? (
              <span className="spinner" />
            ) : (
              <>
                <Icon name="sparkle" /> Build the composite profile
              </>
            )}
          </button>
        }
      />
    )
  }

  const rows = composite.statements || []
  const unsupported = new Set(composite.unsupported_statements || [])
  // NFR-104: when the synthesis could not run the backend assembles the
  // composite from the profile alone. It reads like a thin synthesis, so
  // without this the job seeker is shown a degraded profile as if it were the
  // real one — and was charged for a call that produced nothing.
  const generation = composite.generation || {}
  const degraded =
    generation.mode && generation.mode !== 'llm'
      ? generation.reason || 'the synthesis did not return a usable answer'
      : null
  const byBlock = {}
  for (const row of rows) (byBlock[row.block] ||= []).push(row)

  return (
    <>
      <div className="card phase-edge phase-1">
        <div className="card-header">
          <Icon name="composite" />
          <h3>
            Version {composite.version}
            <HelpTip term="composite_profile" />
          </h3>
          <div className="spacer" />
          {composite.edited_by_user ? <Badge tone="accent">Edited by you</Badge> : null}
          {unsupported.size > 0 && (
            <span className="row" style={{ gap: 0 }}>
              <Badge tone="warn">
                <Icon name="warning" />
                {unsupported.size} statements not traced
              </Badge>
              <HelpTip term="unsupported_statement" align="right" />
            </span>
          )}
          <span className="small muted">{formatDate(composite.created_at)}</span>
          <button className="btn btn-sm" disabled={busy === 'build'} onClick={onBuild}>
            <Icon name="refresh" /> Re-synthesise
          </button>
        </div>
        {degraded && (
          <div className="alert alert-warn" style={{ marginTop: 10 }}>
            <Icon name="warning" />
            <span>
              This version was assembled straight from your profile, without the synthesis:{' '}
              {degraded}. Nothing below is invented, but it is thinner than a synthesised
              profile — re-synthesise to try again.
            </span>
          </div>
        )}
        <p className="small muted" style={{ margin: 0 }}>
          Every line below carries the source that supports it
          <HelpTip term="provenance" />. Editing a line makes you its source — your
          wording wins over the synthesis and survives the next rebuild only if you
          re-apply it.
        </p>
      </div>

      {BLOCKS.map((block) => {
        const items = byBlock[block.key] || []
        const open = editing === block.key
        return (
          <div className="card phase-1" key={block.key}>
            <div className="card-header">
              <Icon name={block.icon} />
              <h3>{block.label}</h3>
              <div className="spacer" />
              <span className="small muted">{items.length || '–'}</span>
              <button
                className="btn btn-sm btn-ghost"
                onClick={() => setEditing(open ? null : block.key)}
              >
                {open ? (
                  <>
                    <Icon name="x" /> Cancel
                  </>
                ) : (
                  <>
                    <Icon name="edit" /> Edit
                  </>
                )}
              </button>
            </div>

            {open ? (
              <BlockEditor
                block={block}
                composite={composite}
                busy={busy === 'patch'}
                onCancel={() => setEditing(null)}
                onSave={(patch) => {
                  setEditing(null)
                  onPatch(patch)
                }}
              />
            ) : items.length === 0 ? (
              <p className="small muted" style={{ margin: 0 }}>
                Nothing here yet. Re-synthesise once your profile has more in it, or add a
                line by hand.
              </p>
            ) : (
              items.map((row) => (
                <div
                  className={`statement-line${unsupported.has(row.id) ? ' confidence-low' : ''}`}
                  key={row.id}
                >
                  <div className="statement-text">{row.text}</div>
                  <div className="statement-meta">
                    {unsupported.has(row.id) && <Badge tone="warn">Needs confirming</Badge>}
                    <StatementSource source={row.source} scoreByUrl={scoreByUrl} />
                  </div>
                </div>
              ))
            )}
          </div>
        )
      })}
    </>
  )
}

/** FR-125: the source marker for one statement. */
function StatementSource({ source, scoreByUrl }) {
  const type = source?.source_type || 'unsupported'
  // Confidence is only shown where the backend computed one — the identity
  // score of the finding a web citation points at. A document the job seeker
  // uploaded carries no score, and inventing one would misrepresent it.
  if (type === 'web' && source.source_ref) {
    return <Provenance source={source.source_ref} confidence={scoreByUrl[source.source_ref]} />
  }
  const label = SOURCE_LABEL[type] || type
  return (
    <Provenance
      source={source?.source_ref ? `${label} · ${source.source_ref}` : label}
      confidence={type === 'unsupported' ? 0 : undefined}
    />
  )
}

/** In-place editing of one block (FR-125). */
function BlockEditor({ block, composite, busy, onCancel, onSave }) {
  const [text, setText] = useState(() => {
    if (block.kind === 'text') return composite.narrative || ''
    if (block.kind === 'object') return composite.seniority?.text || composite.seniority?.level || ''
    return ''
  })
  const [items, setItems] = useState(() =>
    block.kind === 'list' ? (composite[block.key] || []).map((i) => ({ ...i })) : [],
  )

  function save() {
    if (block.kind === 'text') return onSave({ narrative: text })
    if (block.kind === 'object')
      return onSave({
        seniority: { ...(composite.seniority || {}), id: 'seniority:1', text, level: text },
      })
    onSave({ [block.key]: items.filter((i) => (i.text || '').trim()) })
  }

  return (
    <div className="col">
      {block.kind === 'list' ? (
        <>
          {items.map((item, i) => (
            <div className="block-item" key={item.id || `new:${i}`}>
              <textarea
                value={item.text || ''}
                rows={2}
                onChange={(e) =>
                  setItems(items.map((x, j) => (j === i ? { ...x, text: e.target.value } : x)))
                }
              />
              <button
                className="btn btn-sm btn-ghost"
                onClick={() => setItems(items.filter((_, j) => j !== i))}
                title="Remove this statement"
              >
                <Icon name="x" />
              </button>
            </div>
          ))}
          <div>
            <button
              className="btn btn-sm"
              onClick={() => setItems([...items, { id: `${block.key}:new-${items.length + 1}`, text: '' }])}
            >
              <Icon name="plus" /> Add a statement
            </button>
          </div>
        </>
      ) : (
        <textarea
          value={text}
          rows={block.kind === 'text' ? 7 : 2}
          onChange={(e) => setText(e.target.value)}
        />
      )}

      <div className="row">
        <button className="btn btn-primary btn-sm" disabled={busy} onClick={save}>
          <Icon name="check" /> Save
        </button>
        <button className="btn btn-sm btn-ghost" onClick={onCancel}>
          <Icon name="x" /> Cancel
        </button>
        <span className="small muted">
          Anything you add here is attributed to you, not to a web page.
        </span>
      </div>
    </div>
  )
}

