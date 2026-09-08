/**
 * Sections tab (FR-104, FR-105).
 *
 * Structured editing of the FR-102 section schema — the same shape the
 * LinkedIn export defines, so what you type here and what the parser extracted
 * are indistinguishable downstream. Descriptions are textareas because that is
 * where the substance of a role lives and where a generated CV draws from.
 *
 * A save appends a version (FR-105); nothing here overwrites history.
 */

import { useEffect, useMemo, useState } from 'react'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, ChipSelect, Empty, ErrorBox, Field } from '../../components/ui'

/**
 * The editable sections, in the order a reader expects them. `fields` mirror
 * the keys the parsers produce (backend/dreamjob/pipeline/linkedin_pdf.py), so
 * an edit round-trips instead of quietly dropping a column.
 */
const SECTIONS = [
  {
    key: 'experience',
    label: 'Experience',
    icon: 'vacancy',
    heading: (e) => e.title || e.company || 'New role',
    blank: {
      company: '', title: '', start: '', end: '', current: false,
      location: '', description: '',
    },
    fields: [
      { key: 'company', label: 'Employer' },
      { key: 'title', label: 'Role title' },
      { key: 'location', label: 'Location' },
      { key: 'start', label: 'From', hint: 'YYYY-MM' },
      { key: 'end', label: 'To', hint: 'YYYY-MM, or leave empty if current' },
      { key: 'current', label: 'Current role', type: 'checkbox' },
      { key: 'description', label: 'What you did', type: 'textarea' },
    ],
  },
  {
    key: 'education',
    label: 'Education',
    icon: 'companies',
    heading: (e) => e.school || 'New entry',
    blank: {
      school: '', degree: '', field: '', location: '',
      start_year: '', end_year: '', description: '',
    },
    fields: [
      { key: 'school', label: 'Institution' },
      { key: 'degree', label: 'Degree' },
      { key: 'field', label: 'Field of study' },
      { key: 'location', label: 'Location' },
      { key: 'start_year', label: 'From (year)', type: 'number' },
      { key: 'end_year', label: 'To (year)', type: 'number' },
      { key: 'description', label: 'Notes', type: 'textarea' },
    ],
  },
  {
    key: 'certifications',
    label: 'Certifications',
    icon: 'success',
    heading: (e) => e.title || 'New certification',
    blank: { title: '', detail: '', start: '', end: '' },
    fields: [
      { key: 'title', label: 'Title' },
      { key: 'start', label: 'Obtained' },
      { key: 'end', label: 'Expires' },
      { key: 'detail', label: 'Issuer and detail', type: 'textarea' },
    ],
  },
  {
    key: 'publications',
    label: 'Publications',
    icon: 'document',
    heading: (e) => e.title || 'New publication',
    blank: { title: '', detail: '', start: '', end: '' },
    fields: [
      { key: 'title', label: 'Title' },
      { key: 'start', label: 'Published' },
      { key: 'detail', label: 'Venue, co-authors, abstract', type: 'textarea' },
    ],
  },
  {
    key: 'projects',
    label: 'Projects',
    icon: 'target',
    heading: (e) => e.title || 'New project',
    blank: { title: '', detail: '', start: '', end: '' },
    fields: [
      { key: 'title', label: 'Title' },
      { key: 'start', label: 'From' },
      { key: 'end', label: 'To' },
      { key: 'detail', label: 'What it was and what you contributed', type: 'textarea' },
    ],
  },
  {
    key: 'languages',
    label: 'Languages',
    icon: 'browser',
    heading: (e) => e.language || 'New language',
    blank: { language: '', proficiency: '' },
    fields: [
      { key: 'language', label: 'Language' },
      { key: 'proficiency', label: 'Proficiency', hint: 'e.g. Native, Professional working' },
    ],
  },
]

const CONTACT_FIELDS = [
  { key: 'name', label: 'Name' },
  { key: 'headline', label: 'Headline' },
  { key: 'location', label: 'Location' },
  { key: 'email', label: 'Email' },
  { key: 'phone', label: 'Phone' },
  { key: 'linkedin_url', label: 'LinkedIn URL' },
]

export default function SectionsTab({ profile, onNewVersion }) {
  const [draft, setDraft] = useState(profile?.sections || null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [savedAs, setSavedAs] = useState(null)

  useEffect(() => {
    setDraft(profile?.sections || null)
    setSavedAs(null)
  }, [profile?.id])

  const lowConfidence = useMemo(
    () => new Set((profile?.low_confidence_fields || []).map((f) => f.field_path)),
    [profile],
  )

  const dirty = useMemo(
    () => JSON.stringify(draft) !== JSON.stringify(profile?.sections),
    [draft, profile],
  )

  if (!profile || !draft) {
    return (
      <Empty
        title={
          <>
            <span className="icon-chip icon-chip-lg phase-chip" style={{ display: 'flex' }}>
              <Icon name="edit" />
            </span>
            Nothing to edit yet
          </>
        }
      >
        Import a LinkedIn export or a CV on the Documents tab, or start from a blank profile by
        saving one from there. The section list comes from the LinkedIn export schema (FR-102).
      </Empty>
    )
  }

  function setSection(key, value) {
    setDraft((d) => ({ ...d, [key]: value }))
  }

  function setEntry(key, index, field, value) {
    setDraft((d) => ({
      ...d,
      [key]: (d[key] || []).map((e, i) => (i === index ? { ...e, [field]: value } : e)),
    }))
  }

  async function save() {
    setError(null)
    setSaving(true)
    try {
      // FR-105: a save appends a version rather than replacing the current one.
      const next = await api.put('/profile/', { sections: draft })
      setSavedAs(next.version)
      onNewVersion(next)
    } catch (err) {
      setError(err)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="col" style={{ gap: 14 }}>
      {error && <ErrorBox error={error} onRetry={() => setError(null)} />}

      <div className="row row-wrap">
        <span className="small muted">
          Editing version {profile.version}.
          <HelpTip
            title="What a save does"
          >
            Saving writes a new version rather than changing this one (FR-105), re-derives your
            normalised skill list from what you wrote, and carries any unsettled conflicts across.
          </HelpTip>
        </span>
        {dirty && (
          <Badge tone="warn">
            <Icon name="edit" />
            unsaved changes
          </Badge>
        )}
        {savedAs != null && !dirty && (
          <Badge tone="ok">
            <Icon name="check" />
            saved as version {savedAs}
          </Badge>
        )}
        <div className="spacer" />
        <button className="btn btn-primary btn-sm" disabled={!dirty || saving} onClick={save}>
          <Icon name="check" />
          {saving ? 'Saving…' : 'Save as a new version'}
        </button>
      </div>

      <div className="card phase-edge phase-1">
        <div className="card-header">
          <Icon name="contacts" />
          <h3>Contact</h3>
          <HelpTip term="do_not_disclose" />
        </div>
        <div className="entry-grid">
          {CONTACT_FIELDS.map((f) => (
            <Field key={f.key} label={f.label}>
              <input
                type="text"
                value={draft.contact?.[f.key] ?? ''}
                onChange={(e) =>
                  setDraft((d) => ({ ...d, contact: { ...(d.contact || {}), [f.key]: e.target.value } }))
                }
              />
            </Field>
          ))}
        </div>
      </div>

      <div className="card phase-edge phase-1">
        <div className="card-header">
          <Icon name="document" />
          <h3>Summary</h3>
          {lowConfidence.has('summary') && <Badge tone="warn">read with low confidence</Badge>}
          <HelpTip term="provenance" />
        </div>
        <textarea
          rows={7}
          value={draft.summary ?? ''}
          placeholder="The paragraph a reader sees first. Written in your own voice — generated documents draw on it but never invent beyond it."
          onChange={(e) => setSection('summary', e.target.value)}
        />
      </div>

      <div className="card phase-edge phase-1">
        <div className="card-header">
          <Icon name="sparkle" />
          <h3>Top skills</h3>
          <HelpTip
            title="Top skills vs the Skills tab"
            align="right"
          >
            These are the free-text labels as they appear on your profile. The Skills tab holds
            the normalised version with proficiency and recency, which is what matching uses.
          </HelpTip>
        </div>
        <ChipSelect
          options={[]}
          value={(draft.top_skills || []).filter((s) => typeof s === 'string')}
          onChange={(v) => setSection('top_skills', v)}
          allowCustom
        />
      </div>

      {SECTIONS.map((section) => (
        <SectionCard
          key={section.key}
          section={section}
          entries={Array.isArray(draft[section.key]) ? draft[section.key] : []}
          lowConfidence={lowConfidence}
          onChange={(entries) => setSection(section.key, entries)}
          onField={(index, field, value) => setEntry(section.key, index, field, value)}
        />
      ))}
    </div>
  )
}

function SectionCard({ section, entries, lowConfidence, onChange, onField }) {
  return (
    <div className="card phase-edge phase-1">
      <div className="card-header">
        <Icon name={section.icon} />
        <h3>{section.label}</h3>
        <span className="badge">{entries.length}</span>
        <div className="spacer" />
        <button
          className="btn btn-sm"
          onClick={() => onChange([...entries, { ...section.blank }])}
        >
          <Icon name="plus" />
          Add
        </button>
      </div>

      {entries.length === 0 ? (
        <p className="small muted" style={{ margin: 0 }}>
          Nothing here yet.
        </p>
      ) : (
        <div>
          {entries.map((entry, index) => {
            const uncertain = [...lowConfidence].some((p) =>
              p.startsWith(`${section.key}.${index}.`),
            )
            return (
              <div className="entry-card" key={index}>
                <div className="entry-head">
                  <strong>{section.heading(entry)}</strong>
                  {/* NFR-402: a field the parser was unsure of is flagged, not
                      quietly presented as fact. */}
                  {uncertain && (
                    <Badge tone="warn">
                      <Icon name="warning" />
                      check this — read with low confidence
                    </Badge>
                  )}
                  {Array.isArray(entry.sources) && entry.sources.length > 0 && (
                    <span className="tiny muted">
                      from {entry.sources.map((s) => (s === 'linkedin_pdf' ? 'LinkedIn' : 'CV')).join(' + ')}
                    </span>
                  )}
                  <div className="spacer" />
                  <button
                    className="btn btn-sm btn-danger"
                    onClick={() => onChange(entries.filter((_, i) => i !== index))}
                  >
                    <Icon name="trash" />
                    Remove
                  </button>
                </div>

                <div className="entry-grid">
                  {section.fields
                    .filter((f) => f.type !== 'textarea')
                    .map((f) => (
                      <Field key={f.key} label={f.label} hint={f.hint}>
                        {f.type === 'checkbox' ? (
                          <label className="checkline">
                            <input
                              type="checkbox"
                              checked={Boolean(entry[f.key])}
                              onChange={(e) => onField(index, f.key, e.target.checked)}
                            />
                            Yes
                          </label>
                        ) : (
                          <input
                            type={f.type === 'number' ? 'number' : 'text'}
                            value={entry[f.key] ?? ''}
                            onChange={(e) =>
                              onField(
                                index,
                                f.key,
                                f.type === 'number'
                                  ? e.target.value === ''
                                    ? null
                                    : Number(e.target.value)
                                  : e.target.value,
                              )
                            }
                          />
                        )}
                      </Field>
                    ))}
                </div>

                {section.fields
                  .filter((f) => f.type === 'textarea')
                  .map((f) => (
                    <Field key={f.key} label={f.label}>
                      <textarea
                        rows={4}
                        value={entry[f.key] ?? ''}
                        onChange={(e) => onField(index, f.key, e.target.value)}
                      />
                    </Field>
                  ))}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
