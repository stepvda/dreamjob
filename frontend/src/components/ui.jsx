/**
 * Shared building blocks.
 *
 * These exist so the sixteen screens agree on how a score, a provenance
 * reference, a long-running job or a speculative opening looks - the visual
 * consistency that FR-222 asks for on company profiles and that the ranked
 * list needs to stay readable.
 */

import { Fragment, useEffect, useState } from 'react'

import Icon from './Icon'

/* --- Data loading --------------------------------------------------------- */

/**
 * Fetch on mount with the three states every screen needs. `deps` re-runs it.
 */
export function useFetch(fn, deps = []) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    let live = true
    setLoading(true)
    setError(null)
    Promise.resolve(fn())
      .then((d) => live && setData(d))
      .catch((e) => live && setError(e))
      .finally(() => live && setLoading(false))
    return () => {
      live = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce])

  return { data, error, loading, reload: () => setNonce((n) => n + 1), setData }
}

export function Loading({ rows = 3 }) {
  return (
    <div className="col" style={{ gap: 8 }}>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton" style={{ width: `${100 - i * 12}%` }} />
      ))}
    </div>
  )
}

export function ErrorBox({ error, onRetry }) {
  if (!error) return null
  return (
    <div className="alert alert-danger">
      <div>
        <strong>Something went wrong.</strong> {error.message}
        {onRetry && (
          <div style={{ marginTop: 8 }}>
            <button className="btn btn-sm" onClick={onRetry}>
              Try again
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

export function Empty({ title, children, action }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  )
}

/* --- Scores and meters ---------------------------------------------------- */

/** A 0-100 score bar. Used for the overall score and every sub-score (FR-282). */
export function Meter({ value, max = 100, tone, width }) {
  const pct = value == null ? 0 : Math.max(0, Math.min(100, (value / max) * 100))
  const auto = pct >= 70 ? 'ok' : pct >= 40 ? '' : 'warn'
  return (
    <div className="meter" style={width ? { width } : undefined}>
      <div className="meter-track">
        <div className={`meter-fill ${tone ?? auto}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="meter-value">{value == null ? '–' : Math.round(value)}</span>
    </div>
  )
}

/** The sub-score breakdown that makes a ranking explainable (FR-282). */
export function SubScores({ scores }) {
  const entries = Object.entries(scores || {}).filter(([, v]) => v != null)
  if (!entries.length) return null
  return (
    <div className="subscores">
      {entries.map(([k, v]) => (
        <Fragment key={k}>
          <span className="label">{LABELS[k] || k}</span>
          <div className="meter-track">
            <div
              className={`meter-fill ${v >= 70 ? 'ok' : v >= 40 ? '' : 'warn'}`}
              style={{ width: `${Math.max(0, Math.min(100, v))}%` }}
            />
          </div>
          <span className="meter-value">{Math.round(v)}</span>
        </Fragment>
      ))}
    </div>
  )
}

const LABELS = {
  score_profile_fit: 'Profile fit',
  score_dream_fit: 'Dream-job fit',
  score_directive_fit: 'Directive fit',
  score_company: 'Company',
  score_compensation: 'Compensation',
  score_plausibility: 'Plausibility',
  score_reachability: 'Reachability',
}

/* --- Labels --------------------------------------------------------------- */

export function Badge({ tone, children }) {
  return <span className={`badge${tone ? ` badge-${tone}` : ''}`}>{children}</span>
}

/**
 * FR-263: a speculative opening must be distinguishable from a real vacancy in
 * every view. This is the single component that renders that distinction.
 */
export function KindBadge({ kind }) {
  return kind === 'speculative' ? (
    <Badge tone="speculative">Speculative opening</Badge>
  ) : (
    <Badge tone="info">Advertised vacancy</Badge>
  )
}

/** NFR-402: every extracted field carries confidence and provenance. */
export function Provenance({ source, confidence }) {
  if (!source) return null
  const label = typeof source === 'string' ? source : source.url || source.source_type
  return (
    <span
      className="provenance"
      title={`Source: ${label}${confidence != null ? ` · confidence ${Math.round(confidence * 100)}%` : ''}`}
    >
      {shorten(label)}
      {confidence != null && confidence < 0.5 ? ' ·  low confidence' : ''}
    </span>
  )
}

function shorten(s) {
  if (!s) return ''
  try {
    const u = new URL(s)
    return u.hostname.replace(/^www\./, '')
  } catch {
    return s.length > 34 ? `${s.slice(0, 32)}…` : s
  }
}

export function ValidationBadge({ result }) {
  const tone =
    { valid: 'ok', risky: 'warn', invalid: 'danger', unknown: undefined }[result] ?? undefined
  return <Badge tone={tone}>{result || 'unchecked'}</Badge>
}

/* --- Long-running work (NFR-502) ------------------------------------------ */

/**
 * Every long-running operation shows progress, a time estimate and a cancel
 * control (NFR-502).
 */
export function JobProgress({ job, onPause, onResume, onCancel }) {
  if (!job) return null
  const running = job.status === 'running'
  const terminal = ['done', 'failed', 'cancelled'].includes(job.status)
  // For a finished job the bar reflects what actually happened, not the
  // original plan. The planned total is often a generous ceiling (e.g. "12
  // candidate URLs") and the worker only reaches a subset that were fetchable,
  // so showing done/plan would look stuck at e.g. 4/12 long after completion.
  const pct = terminal
    ? job.status === 'done'
      ? 100
      : job.progress_total
        ? Math.round((job.progress_done / job.progress_total) * 100)
        : null
    : job.progress_total
      ? Math.round((job.progress_done / job.progress_total) * 100)
      : null

  return (
    <div className="card">
      <div className="row" style={{ marginBottom: 8 }}>
        <strong>{job.kind}</strong>
        {job.adapter_key && <span className="badge">{job.adapter_key}</span>}
        <Badge tone={STATUS_TONE[job.status]}>{job.status}</Badge>
        <div className="spacer" />
        {running && onPause && (
          <button className="btn btn-sm" onClick={onPause}>
            Pause
          </button>
        )}
        {job.status === 'paused' && onResume && (
          <button className="btn btn-sm" onClick={onResume}>
            Resume
          </button>
        )}
        {(running || job.status === 'paused') && onCancel && (
          <button className="btn btn-sm btn-danger" onClick={onCancel}>
            Cancel
          </button>
        )}
      </div>
      <div className="progress-track">
        <div
          className={`progress-fill${pct == null && running ? ' indeterminate' : ''}`}
          style={pct == null ? undefined : { width: `${pct}%` }}
        />
      </div>
      <div className="row small muted" style={{ marginTop: 6 }}>
        <span>
          {terminal
            ? job.status === 'done'
              ? 'Complete'
              : `${job.progress_done ?? 0} of ${job.progress_total || '?'} attempted`
            : `${job.progress_done ?? 0}${job.progress_total ? ` / ${job.progress_total}` : ''}`}
        </span>
        {job.estimated_seconds != null && <span>· {formatDuration(job.estimated_seconds)} estimated</span>}
        {job.error_count > 0 && <span className="badge badge-warn">{job.error_count} errors</span>}
        <div className="spacer" />
        {job.last_error && <span title={job.last_error}>{shorten(job.last_error)}</span>}
      </div>
    </div>
  )
}

const STATUS_TONE = {
  running: 'accent',
  done: 'ok',
  failed: 'danger',
  cancelled: undefined,
  paused: 'warn',
  pending: undefined,
}

/* --- Formatting ----------------------------------------------------------- */

export function formatDuration(seconds) {
  if (seconds == null) return '–'
  if (seconds < 60) return `${Math.round(seconds)}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`
  const h = Math.floor(seconds / 3600)
  const m = Math.round((seconds % 3600) / 60)
  return m ? `${h}h ${m}m` : `${h}h`
}

export function formatMoney(value, currency = 'EUR') {
  if (value == null) return '–'
  return new Intl.NumberFormat('en-BE', {
    style: 'currency',
    currency,
    maximumFractionDigits: 0,
  }).format(value)
}

export function formatDate(iso) {
  if (!iso) return '–'
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })
}

export function formatPercent(value, digits = 1) {
  if (value == null) return '–'
  return `${(value * 100).toFixed(digits)}%`
}

/* --- Modal ---------------------------------------------------------------- */

export function Modal({ title, children, onClose, actions, wide }) {
  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose?.()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        style={wide ? { maxWidth: 1000 } : undefined}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <h3>{title}</h3>
          <div className="spacer" />
          <button className="btn btn-sm btn-ghost" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {actions && <div className="modal-foot">{actions}</div>}
      </div>
    </div>
  )
}

/* --- Structured inputs (FR-141) ------------------------------------------- */

export function Field({ label, hint, children }) {
  return (
    <div className="field">
      {label && <label>{label}</label>}
      {children}
      {hint && <span className="hint">{hint}</span>}
    </div>
  )
}

/** Multi-select as chips. FR-141 wants structured controls, not free text. */
export function ChipSelect({ options, value = [], onChange, allowCustom }) {
  const [draft, setDraft] = useState('')
  const selected = new Set(value)

  function toggle(v) {
    onChange(selected.has(v) ? value.filter((x) => x !== v) : [...value, v])
  }

  return (
    <div className="col" style={{ gap: 6 }}>
      <div className="chips">
        {options.map((o) => {
          const v = typeof o === 'string' ? o : o.value
          const label = typeof o === 'string' ? o : o.label
          return (
            <span
              key={v}
              className={`chip clickable${selected.has(v) ? ' on' : ''}`}
              onClick={() => toggle(v)}
            >
              {label}
            </span>
          )
        })}
        {value
          .filter((v) => !options.some((o) => (typeof o === 'string' ? o : o.value) === v))
          .map((v) => (
            <span key={v} className="chip on">
              {v}
              <button onClick={() => toggle(v)}>×</button>
            </span>
          ))}
      </div>
      {allowCustom && (
        <div className="row">
          <input
            type="text"
            value={draft}
            placeholder="Add…"
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && draft.trim()) {
                e.preventDefault()
                if (!selected.has(draft.trim())) onChange([...value, draft.trim()])
                setDraft('')
              }
            }}
          />
        </div>
      )}
    </div>
  )
}

export function Tabs({ tabs, active, onChange }) {
  return (
    <div className="tabs">
      {tabs.map((t) => (
        <button
          key={t.key}
          className={`tab${active === t.key ? ' active' : ''}`}
          onClick={() => onChange(t.key)}
        >
          {t.label}
          {t.count != null && <span className="badge" style={{ marginLeft: 6 }}>{t.count}</span>}
        </button>
      ))}
    </div>
  )
}

/* --- Spectrum helpers ------------------------------------------------------
 *
 * The five journey phases each own a hue (styles/theme.css). These wrappers
 * are how a screen opts into its phase colour without hard-coding one, so a
 * card, its icon and its meter all agree.
 */


export const PHASE_CLASS = {
  overview: 'phase-0',
  profile: 'phase-1',
  composite: 'phase-1',
  'dream-job': 'phase-1',
  directives: 'phase-2',
  campaigns: 'phase-2',
  browser: 'phase-2',
  opportunities: 'phase-3',
  companies: 'phase-3',
  intelligence: 'phase-3',
  contacts: 'phase-4',
  applications: 'phase-4',
  networking: 'phase-4',
  pipeline: 'phase-5',
  responses: 'phase-5',
  insights: 'phase-5',
  monitoring: 'phase-0',
  mail: 'phase-0',
  admin: 'phase-0',
}

/** The phase class for a route, e.g. phaseOf('/opportunities') -> 'phase-3'. */
export function phaseOf(pathname) {
  return PHASE_CLASS[(pathname || '').split('/')[1]] || 'phase-0'
}

/** A headline number. Links somewhere when `to` is given. */
export function Stat({ icon, value, label, phase, to, onClick }) {
  const body = (
    <>
      <span className="icon-chip">
        <Icon name={icon} />
      </span>
      <span>
        <span className="stat-value">{value ?? '–'}</span>
        <span className="stat-label">{label}</span>
      </span>
    </>
  )
  const cls = `stat ${phase || ''}`
  if (to) {
    return (
      <a className={cls} href={to} onClick={onClick}>
        {body}
      </a>
    )
  }
  return (
    <div className={cls} onClick={onClick}>
      {body}
    </div>
  )
}

/** A card headed by an icon in its phase colour. */
export function SectionCard({ icon, title, phase, actions, children, edge = true }) {
  return (
    <div className={`card ${edge ? 'phase-edge' : ''} ${phase || ''}`}>
      {(icon || title) && (
        <div className="card-header">
          {icon && <Icon name={icon} />}
          <h3>{title}</h3>
          <div className="spacer" />
          {actions}
        </div>
      )}
      {children}
    </div>
  )
}
