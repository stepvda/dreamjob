/**
 * Applying for an internship: how long, from when, and whether it is paid.
 *
 * Saved for the job seeker rather than per job, because it describes what they
 * are looking for, not one opening. The generator appends the terms to every
 * application e-mail as one fixed sentence — the preview below is that
 * sentence, word for word — so a model never paraphrases a start date. A
 * package that was generated before a change keeps its text until it is
 * regenerated, which is said here rather than discovered later.
 */

import { useEffect, useState } from 'react'

import Icon from '../../components/Icon'
import { Badge, ErrorBox, Field } from '../../components/ui'

const DURATIONS = [1, 2, 3, 4, 5, 6, 9, 12]

const PAY = [
  { key: 'paid', label: 'Paid' },
  { key: 'unpaid', label: 'Unpaid' },
  { key: 'either', label: 'Either' },
]

const BLANK = { internship: false, duration_months: null, start_month: null, pay: 'either' }

function sameTerms(a, b) {
  return ['internship', 'duration_months', 'start_month', 'pay'].every(
    (key) => (a?.[key] ?? null) === (b?.[key] ?? null),
  )
}

export default function InternshipTerms({ saved, loading, onSave }) {
  const [draft, setDraft] = useState(BLANK)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (saved) setDraft({ ...BLANK, ...saved })
  }, [saved])

  const dirty = saved ? !sameTerms(draft, saved) : false
  const set = (key, value) => setDraft((current) => ({ ...current, [key]: value }))

  async function save() {
    setBusy(true)
    setError(null)
    try {
      await onSave({
        internship: draft.internship,
        duration_months: draft.duration_months,
        start_month: draft.start_month,
        pay: draft.pay,
      })
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card" style={{ marginBottom: 14, padding: '10px 14px' }}>
      <div className="row row-wrap" style={{ gap: 10 }}>
        <label className="row" style={{ gap: 6, cursor: 'pointer', fontWeight: 550 }}>
          <input
            type="checkbox"
            checked={draft.internship}
            disabled={loading || busy}
            onChange={(e) => {
              set('internship', e.target.checked)
              if (e.target.checked) setOpen(true)
            }}
          />
          Applying for an internship
        </label>
        {saved?.internship && !dirty && (
          <Badge tone="ok">
            <Icon name="check" /> In every e-mail generated from now on
          </Badge>
        )}
        {draft.internship && !open && (
          <span className="small muted">{saved?.preview}</span>
        )}
        <div className="spacer" />
        {draft.internship && (
          <button className="btn btn-sm btn-ghost" onClick={() => setOpen(!open)}>
            <Icon name="edit" /> {open ? 'Hide terms' : 'Edit terms'}
          </button>
        )}
        {dirty && (
          <button className="btn btn-sm btn-primary" disabled={busy} onClick={save}>
            {busy ? <span className="spinner" /> : <Icon name="check" />} Save
          </button>
        )}
      </div>

      {draft.internship && open && (
        <div className="grid grid-3" style={{ marginTop: 12 }}>
          <Field label="For how long">
            <select
              value={draft.duration_months ?? ''}
              onChange={(e) =>
                set('duration_months', e.target.value ? Number(e.target.value) : null)
              }
            >
              <option value="">Not stated</option>
              {DURATIONS.map((n) => (
                <option key={n} value={n}>
                  {n} month{n === 1 ? '' : 's'}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Starting" hint="Leave empty to not state a start.">
            <input
              type="month"
              value={draft.start_month ?? ''}
              onChange={(e) => set('start_month', e.target.value || null)}
            />
          </Field>

          <Field label="Pay">
            <div className="row" style={{ gap: 14, minHeight: 32 }}>
              {PAY.map((option) => (
                <label key={option.key} className="row" style={{ gap: 5, cursor: 'pointer' }}>
                  <input
                    type="radio"
                    name="internship-pay"
                    checked={draft.pay === option.key}
                    onChange={() => set('pay', option.key)}
                  />
                  {option.label}
                </label>
              ))}
            </div>
          </Field>
        </div>
      )}

      {open && (dirty || saved?.internship) && (
        <p className="small muted" style={{ margin: draft.internship ? 0 : '8px 0 0' }}>
          {dirty ? 'Save to use these terms. ' : `Added to the e-mail, in its language: “${saved.preview}” `}
          Packages that are already generated keep their text until you regenerate them.
        </p>
      )}

      {error && <ErrorBox error={error} onRetry={() => setError(null)} />}
    </div>
  )
}
