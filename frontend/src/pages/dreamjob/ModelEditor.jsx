/**
 * The structured dream-job model (FR-128).
 *
 * These shapes are a contract, not a convenience: campaign planning (FR-162),
 * company discovery (FR-224), speculative openings (FR-262) and scoring
 * (FR-281, FR-383) all read the stored row directly. Every field rendered and
 * every field written back here mirrors `pipeline/dreamjob_model.py`, which
 * re-validates the shapes on save - a renamed key silently loses the block.
 *
 * Editing is in place and per block, because the job seeker corrects one
 * misread line at a time; the alternative, re-running the extraction, throws
 * away the corrections they already made.
 */

import { useState } from 'react'

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, Provenance, formatDate } from '../../components/ui'

const IMPORTANCE = ['must', 'strong', 'nice']
const SOURCES = ['stated', 'inferred']
const POLARITIES = ['seek', 'avoid']
const ATTRIBUTES = [
  'size', 'stage', 'ownership', 'sector', 'geography',
  'work_arrangement', 'mission', 'maturity', 'team_structure',
]

const MODEL_BLOCKS = [
  {
    key: 'target_roles',
    label: 'Target roles',
    icon: 'target',
    caption: 'What the search will look for by name, strongest first.',
    blank: { title: '', seniority: null, priority: 3, rationale: '', source: 'stated', quote: null },
    primary: 'title',
    fields: [
      { name: 'title', label: 'Title', type: 'text', grow: true },
      { name: 'seniority', label: 'Seniority', type: 'text' },
      { name: 'priority', label: 'Priority 1–5', type: 'number', min: 1, max: 5 },
      { name: 'rationale', label: 'Why', type: 'text', grow: true },
      { name: 'source', label: 'Source', type: 'select', options: SOURCES },
    ],
  },
  {
    key: 'role_families',
    label: 'Role families',
    icon: 'networking',
    term: 'role_family',
    caption: 'Broader groupings, so a good role with an unfamiliar title is not missed.',
    blank: { family: '', example_titles: [], confidence: 0.5 },
    primary: 'family',
    fields: [
      { name: 'family', label: 'Family', type: 'text', grow: true },
      { name: 'example_titles', label: 'Example titles', type: 'list', grow: true },
      { name: 'confidence', label: 'Confidence', type: 'number', min: 0, max: 1, step: 0.05 },
    ],
  },
  {
    key: 'responsibilities',
    label: 'Responsibilities',
    icon: 'overview',
    caption: 'The work itself. "Must" items are treated as requirements when scoring.',
    blank: { activity: '', importance: 'strong', source: 'stated', quote: null },
    primary: 'activity',
    fields: [
      { name: 'activity', label: 'Activity', type: 'text', grow: true },
      { name: 'importance', label: 'Importance', type: 'select', options: IMPORTANCE },
      { name: 'source', label: 'Source', type: 'select', options: SOURCES },
    ],
  },
  {
    key: 'company_characteristics',
    label: 'Company characteristics',
    icon: 'companies',
    caption: 'Size, stage, ownership, sector, geography, arrangement, mission.',
    blank: { attribute: 'mission', value: '', importance: 'strong', source: 'stated', quote: null },
    primary: 'value',
    fields: [
      { name: 'attribute', label: 'Attribute', type: 'select', options: ATTRIBUTES },
      { name: 'value', label: 'Value', type: 'text', grow: true },
      { name: 'importance', label: 'Importance', type: 'select', options: IMPORTANCE },
      { name: 'source', label: 'Source', type: 'select', options: SOURCES },
    ],
  },
  {
    key: 'culture_values',
    label: 'Culture and values',
    icon: 'contacts',
    caption: 'What you are drawn to, and what you would rather walk away from.',
    blank: { cue: '', polarity: 'seek', why: '', quote: null },
    primary: 'cue',
    fields: [
      { name: 'cue', label: 'Cue', type: 'text', grow: true },
      { name: 'polarity', label: 'Seek or avoid', type: 'select', options: POLARITIES },
      { name: 'why', label: 'Why', type: 'text', grow: true },
    ],
  },
  {
    key: 'deal_breakers',
    label: 'Deal-breakers',
    icon: 'warning',
    term: 'deal_breaker',
    caption: 'A hard deal-breaker vetoes an opportunity outright, however good it looks.',
    blank: { constraint: '', hard: true, detectable_from: [], quote: null },
    primary: 'constraint',
    fields: [
      { name: 'constraint', label: 'Constraint', type: 'text', grow: true },
      { name: 'hard', label: 'Hard veto', type: 'checkbox' },
      { name: 'detectable_from', label: 'Detectable from', type: 'list', grow: true },
    ],
  },
  {
    key: 'implicit_preferences',
    label: 'Implicit preferences',
    icon: 'eye',
    term: 'implicit_preference',
    caption: 'Read between the lines of your statement, so you can disown them.',
    blank: { preference: '', basis: '', confidence: 0.5 },
    primary: 'preference',
    fields: [
      { name: 'preference', label: 'Preference', type: 'text', grow: true },
      { name: 'basis', label: 'Basis', type: 'text', grow: true },
      { name: 'confidence', label: 'Confidence', type: 'number', min: 0, max: 1, step: 0.05 },
    ],
  },
]

const IMPORTANCE_TONE = { must: 'accent', strong: 'info', nice: undefined }

export default function ModelEditor({ model, stale, busy, onPatch, onConfirm }) {
  const [editing, setEditing] = useState(null)

  return (
    <>
      <div className="card phase-edge phase-1" style={{ marginTop: 14 }}>
        <div className="card-header">
          <Icon name="composite" />
          <h3>
            Structured model, version {model.version}
            <HelpTip term="dream_job_fit" />
          </h3>
          <div className="spacer" />
          {model.confirmed_by_user ? (
            <Badge tone="ok">
              <Icon name="check" />
              Confirmed by you
            </Badge>
          ) : (
            <Badge tone="warn">
              <Icon name="warning" />
              Not confirmed
            </Badge>
          )}
          <span className="small muted">{formatDate(model.created_at)}</span>
          {!model.confirmed_by_user && (
            <button className="btn btn-sm btn-primary" disabled={busy === 'confirm'} onClick={onConfirm}>
              <Icon name="check" />
              Confirm this model
            </button>
          )}
        </div>
        <p className="small muted" style={{ margin: 0 }}>
          This is what the search and the ranking act on
          <HelpTip term="directive" />. Correct anything that misreads you — the extraction
          is a first pass over your prose, not a verdict on it.
          {stale && ' It was built from an older version of your statement.'}
        </p>
      </div>

      <div className="grid grid-2 phase-1" style={{ marginTop: 14 }}>
        {MODEL_BLOCKS.map((block) => (
          <ModelBlock
            key={block.key}
            block={block}
            items={model[block.key] || []}
            editing={editing === block.key}
            busy={busy === 'patch'}
            onEdit={() => setEditing(block.key)}
            onCancel={() => setEditing(null)}
            onSave={(items) => {
              setEditing(null)
              onPatch({ [block.key]: items })
            }}
          />
        ))}
      </div>
    </>
  )
}

function ModelBlock({ block, items, editing, busy, onEdit, onCancel, onSave }) {
  const [draft, setDraft] = useState(items)

  function begin() {
    setDraft(items.map((i) => ({ ...i })))
    onEdit()
  }

  const set = (i, name, value) =>
    setDraft(draft.map((row, j) => (j === i ? { ...row, [name]: value } : row)))

  return (
    <div className="card phase-edge">
      <div className="card-header">
        <Icon name={block.icon} />
        <h3>
          {block.label}
          {block.term && <HelpTip term={block.term} />}
        </h3>
        <div className="spacer" />
        <span className="badge phase-chip">{items.length}</span>
        <button className="btn btn-sm btn-ghost" onClick={editing ? onCancel : begin}>
          {editing ? (
            <>
              <Icon name="x" />
              Cancel
            </>
          ) : (
            <>
              <Icon name="edit" />
              Edit
            </>
          )}
        </button>
      </div>

      <p className="small muted" style={{ marginTop: 0 }}>{block.caption}</p>

      {editing ? (
        <div className="col">
          {draft.map((row, i) => (
            <div className="block-item" key={i}>
              <div className="block-fields">
                {block.fields.map((f) => (
                  <FieldInput key={f.name} field={f} value={row[f.name]} onChange={(v) => set(i, f.name, v)} />
                ))}
              </div>
              <button
                className="btn btn-sm btn-ghost"
                title="Remove"
                onClick={() => setDraft(draft.filter((_, j) => j !== i))}
              >
                <Icon name="trash" />
              </button>
            </div>
          ))}
          <div className="row">
            <button className="btn btn-sm" onClick={() => setDraft([...draft, { ...block.blank }])}>
              <Icon name="plus" />
              Add
            </button>
            <div className="spacer" />
            <button
              className="btn btn-sm btn-primary"
              disabled={busy}
              onClick={() => onSave(draft.filter((r) => String(r[block.primary] || '').trim()))}
            >
              <Icon name="check" />
              Save
            </button>
          </div>
        </div>
      ) : items.length === 0 ? (
        <p className="small muted" style={{ margin: 0 }}>
          <Icon name="info" /> Nothing extracted. Either your statement did not say, or it
          said it in a way the extraction missed — add it by hand.
        </p>
      ) : (
        items.map((item, i) => <ModelRow key={i} block={block} item={item} />)
      )}
    </div>
  )
}

function ModelRow({ block, item }) {
  const lists = [
    ...(item.example_titles || []),
    ...(item.detectable_from || []),
  ]
  return (
    <div className="statement-line">
      <div>
        <div className="statement-text">
          {item[block.primary]}
          {item.attribute && <span className="muted"> · {item.attribute.replace(/_/g, ' ')}</span>}
          {item.seniority && <span className="muted"> · {item.seniority}</span>}
        </div>
        {(item.rationale || item.why || item.basis) && (
          <div className="small muted">{item.rationale || item.why || item.basis}</div>
        )}
        {item.quote && <div className="tiny muted">“{item.quote}”</div>}
        {lists.length > 0 && (
          <div className="chips" style={{ marginTop: 4 }}>
            {lists.map((v, i) => (
              <span className="chip" key={i}>{v}</span>
            ))}
          </div>
        )}
      </div>
      <div className="statement-meta">
        {item.importance && (
          <Badge tone={IMPORTANCE_TONE[item.importance]}>{item.importance}</Badge>
        )}
        {item.polarity && (
          <Badge tone={item.polarity === 'avoid' ? 'danger' : 'ok'}>{item.polarity}</Badge>
        )}
        {'hard' in item && (
          <Badge tone={item.hard ? 'danger' : undefined}>
            {item.hard ? 'hard veto' : 'soft'}
          </Badge>
        )}
        {item.priority != null && <Badge>priority {item.priority}</Badge>}
        {item.source && <Badge tone={item.source === 'stated' ? 'info' : undefined}>{item.source}</Badge>}
        {item.confidence != null && (
          // NFR-402: an inferred preference is shown with what it was inferred
          // from and how sure the extraction was, never as a bare assertion.
          <Provenance
            source={item.basis || 'inferred from your statement'}
            confidence={item.confidence}
          />
        )}
      </div>
    </div>
  )
}

function FieldInput({ field, value, onChange }) {
  const style = field.grow ? { flex: '1 1 180px', minWidth: 140 } : { width: 120 }

  if (field.type === 'select') {
    return (
      <select style={style} value={value ?? ''} onChange={(e) => onChange(e.target.value)} title={field.label}>
        {field.options.map((o) => (
          <option key={o} value={o}>{o.replace(/_/g, ' ')}</option>
        ))}
      </select>
    )
  }
  if (field.type === 'checkbox') {
    return (
      <label className="checkline" style={{ width: 120 }}>
        <input type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} />
        {field.label}
      </label>
    )
  }
  if (field.type === 'number') {
    return (
      <input
        type="number"
        style={style}
        value={value ?? ''}
        min={field.min}
        max={field.max}
        step={field.step ?? 1}
        title={field.label}
        onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))}
      />
    )
  }
  if (field.type === 'list') {
    return (
      <input
        type="text"
        style={style}
        value={(value || []).join(', ')}
        placeholder={field.label}
        title={`${field.label} — comma separated`}
        onChange={(e) => onChange(e.target.value.split(',').map((s) => s.trim()).filter(Boolean))}
      />
    )
  }
  return (
    <input
      type="text"
      style={style}
      value={value ?? ''}
      placeholder={field.label}
      title={field.label}
      onChange={(e) => onChange(e.target.value)}
    />
  )
}
