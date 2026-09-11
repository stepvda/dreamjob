/**
 * Structured controls for the directive editor (FR-141).
 *
 * FR-141 is explicit that directives are set with drop-downs, multi-select
 * chips, auto-complete and sliders rather than typed prose, because every
 * source adapter has to translate them into its own query language (FR-162).
 * These are the primitives that make that possible; the only free-text field
 * in the whole editor lives on the page itself ("notes to the AI").
 *
 * The numeric bounds are never hard-coded here: they come from
 * `vocabulary().ranges`, which the backend publishes precisely so the editor
 * cannot offer a value the Pydantic model would reject.
 */

import { useEffect, useRef, useState } from 'react'

import { api } from '../../api/client'
import Icon from '../../components/Icon'
import { Field } from '../../components/ui'

/* --- Collapsible directive group ------------------------------------------ */

/**
 * One of the five directive groups, as a collapsible card. Collapsed by
 * default except for the group the user is most likely to start with — the
 * whole form open at once is what makes structured editors feel unusable.
 */
export function Group({ icon, title, tip, summary, badge, defaultOpen = false, children }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="card phase-2 phase-edge">
      <div className="card-header">
        <button
          type="button"
          className="dir-group-head"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          <span className="dir-caret" aria-hidden>
            {open ? '▾' : '▸'}
          </span>
          {icon && <Icon name={icon} />}
          <h3>{title}</h3>
        </button>
        {tip}
        {badge}
        <div className="spacer" />
        <span className="dir-summary">{summary}</span>
      </div>
      {open && children}
    </div>
  )
}

/* --- Progressive disclosure: advanced controls ---------------------------- */

/**
 * A one-click disclosure that folds each group's rarely-used controls behind an
 * "Advanced settings" toggle. It is collapsed by default so the essentials of a
 * group are what you meet first, but nothing is removed: opening it restores
 * every control exactly as it was (FR-141).
 */
export function Advanced({ label = 'Advanced settings', children }) {
  const [open, setOpen] = useState(false)
  return (
    <div style={{ marginTop: 4, borderTop: '1px solid var(--line)', paddingTop: 12 }}>
      <button
        type="button"
        className="btn btn-sm btn-ghost"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="dir-caret" aria-hidden>
          {open ? '\u25be' : '\u25b8'}
        </span>
        {open ? `Hide ${label}` : `Show ${label}`}
      </button>
      {open && <div style={{ marginTop: 10 }}>{children}</div>}
    </div>
  )
}

/* --- Drop-down ------------------------------------------------------------ */

/** A single-choice control over a vocabulary group (FR-141, NFR-501 labels). */
export function Select({ label, tip, hint, value, onChange, options, placeholder = 'Any' }) {
  return (
    <Field
      label={
        <>
          {label}
          {tip}
        </>
      }
      hint={hint}
    >
      <select value={value ?? ''} onChange={(e) => onChange(e.target.value || null)}>
        <option value="">{placeholder}</option>
        {(options || []).map((o) => (
          <option key={o.key} value={o.key}>
            {o.label}
          </option>
        ))}
      </select>
    </Field>
  )
}

/* --- Slider --------------------------------------------------------------- */

/**
 * A slider bounded by the range the backend published for this field.
 * `nullable` fields carry an explicit "apply this limit" switch, because
 * "no limit" and "a limit of zero" are different directives.
 */
export function Slider({
  label,
  tip,
  hint,
  value,
  onChange,
  range,
  format = (v) => String(v),
  nullable = false,
}) {
  const r = range || { min: 0, max: 100, step: 1, default: 0 }
  const active = !nullable || value != null
  const shown = value == null ? r.default : value

  return (
    <Field
      label={
        <>
          {label}
          {tip}
        </>
      }
      hint={hint}
    >
      {nullable && (
        <label className="checkline">
          <input
            type="checkbox"
            checked={value != null}
            onChange={(e) => onChange(e.target.checked ? r.default : null)}
          />
          <span className="small">Apply this limit</span>
        </label>
      )}
      <div className="dir-slider-row">
        <input
          type="range"
          min={r.min}
          max={r.max}
          step={r.step}
          value={shown}
          disabled={!active}
          onChange={(e) => onChange(Number(e.target.value))}
        />
        <span className="dir-slider-val">{active ? format(shown) : 'No limit'}</span>
      </div>
    </Field>
  )
}

/** A bounded number box, for ranges too wide to slide through usefully. */
export function NumberBox({ label, tip, hint, value, onChange, range, unit }) {
  const r = range || {}
  return (
    <Field
      label={
        <>
          {label}
          {tip}
        </>
      }
      hint={hint}
    >
      <div className="row">
        <input
          type="number"
          min={r.min}
          max={r.max}
          step={r.step ?? 1}
          value={value ?? ''}
          placeholder="No limit"
          onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))}
        />
        {unit && <span className="small muted nowrap">{unit}</span>}
      </div>
    </Field>
  )
}

/* --- Auto-complete -------------------------------------------------------- */

/** Shared dismiss-on-outside-click behaviour for the two auto-completes. */
function useDismiss(onDismiss) {
  const ref = useRef(null)
  useEffect(() => {
    const onDown = (e) => {
      if (ref.current && !ref.current.contains(e.target)) onDismiss()
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [onDismiss])
  return ref
}

/**
 * FR-142: job-title auto-complete from the bundled catalogue. An empty query
 * is legal and returns the catalogue in file order, which is what makes the
 * list useful the moment the field is focused.
 */
export function TitleAutocomplete({ family, locale = 'en', onPick, disabledTitles = [] }) {
  const [q, setQ] = useState('')
  const [rows, setRows] = useState([])
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const ref = useDismiss(() => setOpen(false))
  const taken = new Set(disabledTitles)

  useEffect(() => {
    if (!open) return undefined
    let live = true
    const t = setTimeout(async () => {
      setBusy(true)
      try {
        const params = new URLSearchParams({ q, locale, limit: '10' })
        if (family) params.set('family', family)
        const res = await api.get(`/directives/titles?${params}`)
        if (live) setRows(Array.isArray(res) ? res : [])
      } catch {
        if (live) setRows([])
      } finally {
        if (live) setBusy(false)
      }
    }, 200)
    return () => {
      live = false
      clearTimeout(t)
    }
  }, [q, family, locale, open])

  return (
    <div className="dir-ac" ref={ref}>
      <input
        type="text"
        value={q}
        placeholder="Start typing a job title…"
        maxLength={100}
        onFocus={() => setOpen(true)}
        onChange={(e) => {
          setQ(e.target.value)
          setOpen(true)
        }}
        onKeyDown={(e) => {
          // Enter on a free-typed title still adds it: the catalogue is a help,
          // not a gate (FR-142).
          if (e.key === 'Enter' && q.trim()) {
            e.preventDefault()
            onPick({ canonical: q.trim(), synonyms: [], family: null, seniority: null })
            setQ('')
          }
        }}
      />
      {open && (
        <div className="dir-ac-list" role="listbox">
          {busy && <div className="dir-ac-note small muted">Searching the catalogue…</div>}
          {!busy && rows.length === 0 && (
            <div className="dir-ac-note small muted">
              No catalogue match. Press Enter to use “{q.trim() || '…'}” as typed.
            </div>
          )}
          {rows.map((r) => (
            <button
              type="button"
              key={r.canonical}
              className="dir-ac-item"
              disabled={taken.has(r.canonical)}
              onClick={() => {
                onPick(r)
                setQ('')
              }}
            >
              <span className="dir-ac-main">{r.label || r.canonical}</span>
              <span className="dir-ac-meta small muted">
                {[r.family, r.seniority].filter(Boolean).join(' · ')}
                {r.synonyms?.length ? ` · ${r.synonyms.length} synonyms` : ''}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * FR-144: place auto-complete backed by the geocoder. The endpoint returns an
 * empty list rather than an error when the geocoder is unreachable, so the
 * "use the label as typed" escape is a first-class path, not an error state —
 * an ungeocoded area simply carries no radius filter.
 */
export function PlaceAutocomplete({ countries, locale = 'en', onPick, placeholder }) {
  const [q, setQ] = useState('')
  const [rows, setRows] = useState([])
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)
  const ref = useDismiss(() => setOpen(false))

  useEffect(() => {
    const text = q.trim()
    if (text.length < 2) {
      setRows([])
      return undefined
    }
    let live = true
    const t = setTimeout(async () => {
      setBusy(true)
      setFailed(false)
      try {
        const params = new URLSearchParams({ q: text, locale, limit: '5' })
        if (countries?.length) params.set('countries', countries.join(','))
        const res = await api.get(`/directives/locations/search?${params}`)
        if (live) setRows(Array.isArray(res) ? res : [])
      } catch {
        if (live) {
          setRows([])
          setFailed(true)
        }
      } finally {
        if (live) setBusy(false)
      }
    }, 350)
    return () => {
      live = false
      clearTimeout(t)
    }
  }, [q, countries, locale])

  function addTyped() {
    const text = q.trim()
    if (!text) return
    onPick({ label: text })
    setQ('')
    setOpen(false)
  }

  return (
    <div className="dir-ac" ref={ref}>
      <div className="row">
        <input
          type="text"
          value={q}
          placeholder={placeholder || 'Town, city or region…'}
          onFocus={() => setOpen(true)}
          onChange={(e) => {
            setQ(e.target.value)
            setOpen(true)
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              addTyped()
            }
          }}
        />
        <button type="button" className="btn btn-sm" onClick={addTyped} disabled={!q.trim()}>
          <Icon name="plus" /> Add
        </button>
      </div>
      {open && q.trim().length >= 2 && (
        <div className="dir-ac-list" role="listbox">
          {busy && <div className="dir-ac-note small muted">Looking the place up…</div>}
          {!busy && rows.length === 0 && (
            <div className="dir-ac-note small muted">
              {failed
                ? 'The geocoder could not be reached. “Add” keeps your label; the area will have no radius filter until it resolves.'
                : 'No match. “Add” keeps your label as typed.'}
            </div>
          )}
          {rows.map((r) => (
            <button
              type="button"
              key={`${r.osm_id || r.display_name}-${r.latitude}`}
              className="dir-ac-item"
              onClick={() => {
                onPick({
                  label: r.display_name,
                  latitude: r.latitude,
                  longitude: r.longitude,
                  country_code: r.country_code,
                  place_type: r.place_type,
                  geocoded_at: r.geocoded_at,
                })
                setQ('')
                setOpen(false)
              }}
            >
              <span className="dir-ac-main">{r.display_name}</span>
              <span className="dir-ac-meta small muted">
                {[r.place_type, r.country_code?.toUpperCase()].filter(Boolean).join(' · ')}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/* --- Named company lists -------------------------------------------------- */

/**
 * A list of `CompanyReference` values — name plus whatever else the user
 * happens to know. Used for the include/exclude lists (FR-143) and, with a
 * reason drop-down, for the discretion exclusions (FR-385).
 */
export function CompanyList({ value = [], onChange, reasons, placeholder = 'Company name' }) {
  const [name, setName] = useState('')
  const [domain, setDomain] = useState('')

  function add() {
    const trimmed = name.trim()
    if (!trimmed) return
    if (value.some((c) => c.name.toLowerCase() === trimmed.toLowerCase())) return
    const entry = { name: trimmed, domain: domain.trim() || null }
    if (reasons) {
      entry.reason = 'flagged_sensitive'
      entry.match_group_entities = true
    }
    onChange([...value, entry])
    setName('')
    setDomain('')
  }

  return (
    <div className="col" style={{ gap: 8 }}>
      {value.length > 0 && (
        <div className="col" style={{ gap: 6 }}>
          {value.map((c, i) => (
            <div className="dir-listrow" key={`${c.name}-${i}`}>
              <div className="col" style={{ gap: 2, minWidth: 0 }}>
                <strong className="small">{c.name}</strong>
                {c.domain && <span className="tiny muted">{c.domain}</span>}
              </div>
              <div className="spacer" />
              {reasons && (
                <>
                  <label className="checkline tiny" title="Also exclude group companies">
                    <input
                      type="checkbox"
                      checked={c.match_group_entities !== false}
                      disabled={c.reason === 'current_employer'}
                      onChange={(e) =>
                        onChange(
                          value.map((x, j) =>
                            j === i ? { ...x, match_group_entities: e.target.checked } : x,
                          ),
                        )
                      }
                    />
                    Group
                  </label>
                  <select
                    className="dir-inline-select"
                    value={c.reason || 'flagged_sensitive'}
                    disabled={c.reason === 'current_employer'}
                    onChange={(e) =>
                      onChange(value.map((x, j) => (j === i ? { ...x, reason: e.target.value } : x)))
                    }
                  >
                    {reasons.map((o) => (
                      <option key={o.key} value={o.key}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </>
              )}
              <button
                type="button"
                className="btn btn-sm btn-ghost"
                aria-label={`Remove ${c.name}`}
                disabled={c.reason === 'current_employer'}
                onClick={() => onChange(value.filter((_, j) => j !== i))}
              >
                <Icon name="x" />
              </button>
            </div>
          ))}
        </div>
      )}
      <div className="row">
        <input
          type="text"
          value={name}
          placeholder={placeholder}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), add())}
        />
        <input
          type="text"
          value={domain}
          placeholder="domain (optional)"
          style={{ maxWidth: 180 }}
          onChange={(e) => setDomain(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), add())}
        />
        <button type="button" className="btn btn-sm" onClick={add} disabled={!name.trim()}>
          <Icon name="plus" /> Add
        </button>
      </div>
    </div>
  )
}
