/**
 * Continuous data collection (FR-161..166, FR-185, FR-301..303, FR-281).
 *
 * One switch that keeps the corpus growing on its own. The tab is a window on
 * a state machine that lives in the database: the scheduler runs one phase per
 * interval, the phases rotate Discover → Contacts → Enrich → Score and the
 * cycle never stops until the switch does. Everything here therefore has to
 * answer three questions without a page reload: is it on, what is it doing
 * next, and what did it just do.
 *
 * The status and the four corpus counters are polled together every five
 * seconds (`?fresh=1` bypasses the backend's eight-second cache), paused while
 * the tab is hidden, so an administrator watching a run sees the numbers move.
 * A manual run returns its own report; it does not move the cycle on.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import {
  Badge,
  ErrorBox,
  Field,
  Loading,
  SectionCard,
  Stat,
  formatDuration,
  useFetch,
} from '../../components/ui'
import { num, stamp } from './format'
import './continuous.css'

const POLL_MS = 5000

const PHASES = ['discover', 'contacts', 'enrich', 'score']

const PHASE_LABELS = {
  discover: 'Discover',
  contacts: 'Contacts',
  enrich: 'Enrich',
  score: 'Score',
}

const PHASE_BLURBS = {
  discover:
    'Starts the next ready profile through autopilot: plan, collect, rank — the contract the rest of the cycle feeds.',
  contacts:
    'Fills in e-mail addresses for stored contacts and rotates in a discovery pass so newly added companies get somebody to write to.',
  enrich:
    'Builds company profiles, their financials and the shared knowledge base for companies that have none yet.',
  score:
    'Re-ranks the cursor seeker’s campaign. Deterministic: no model is called and no tokens are spent.',
}

const MIN_INTERVAL = 300
const MAX_INTERVAL = 86400

/** Parse the interval field. Returns `{value}` or `{error}` — one or the other. */
function parseInterval(raw) {
  if (raw === '' || raw == null) return { error: 'Enter an interval in seconds.' }
  const value = Number(raw)
  if (!Number.isInteger(value)) return { error: 'Whole seconds only — 900, not “15 minutes”.' }
  if (value < MIN_INTERVAL) return { error: `At least ${MIN_INTERVAL} seconds (5 minutes).` }
  if (value > MAX_INTERVAL) return { error: `At most ${MAX_INTERVAL} seconds (24 hours).` }
  return { value }
}

/** “3 minutes ago” / “in 12 minutes”, enough precision for a five-second poll. */
function relativeTime(iso) {
  if (!iso) return null
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return iso
  const delta = Math.round((then - Date.now()) / 1000)
  const abs = Math.abs(delta)
  const say = (n, unit) => `${n} ${unit}${n === 1 ? '' : 's'}`
  let text
  if (abs < 5) return 'just now'
  else if (abs < 60) text = say(abs, 'second')
  else if (abs < 3600) text = say(Math.round(abs / 60), 'minute')
  else if (abs < 86400) text = say(Math.round(abs / 3600), 'hour')
  else text = say(Math.round(abs / 86400), 'day')
  return delta < 0 ? `${text} ago` : `in ${text}`
}

/**
 * Flatten a phase report into label/value rows. Reports mix scalars with one
 * level of nested dicts (`profiles`, `financials`), so nested keys carry their
 * parent as a dotted prefix rather than opening another panel.
 */
function flattenReport(value, prefix = '', out = []) {
  if (value == null || typeof value !== 'object' || Array.isArray(value)) {
    out.push([prefix || 'value', value])
    return out
  }
  const entries = Object.entries(value)
  if (!entries.length) {
    out.push([prefix || 'report', '–'])
    return out
  }
  for (const [key, entry] of entries) {
    const label = prefix ? `${prefix}.${key}` : key
    if (entry && typeof entry === 'object' && !Array.isArray(entry)) {
      flattenReport(entry, label, out)
    } else {
      out.push([label, entry])
    }
  }
  return out
}

function reportText(value) {
  if (value == null || value === '') return '–'
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (Array.isArray(value)) return value.map(reportText).join(', ')
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function ReportEntries({ report }) {
  const rows = flattenReport(report)
  if (!rows.length) return null
  return (
    <div className="cont-report">
      <div className="cont-report-scroll">
        {rows.map(([key, value]) => (
          <div className="cont-report-row" key={key}>
            <span className="cont-report-key">{key.replace(/_/g, ' ')}</span>
            <span className="cont-report-value">{reportText(value)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

/** The early-exit shapes the engine returns: nothing ran, and why. */
function OutcomeBanner({ report }) {
  if (!report) return null
  if (report.error) {
    return (
      <div className="alert alert-danger" style={{ marginTop: 10 }}>
        <div>
          <strong>Phase failed.</strong> {report.error}
          <div className="small" style={{ marginTop: 4 }}>
            The cycle recorded it and moved on; the next tick retries from the next phase.
          </div>
        </div>
      </div>
    )
  }
  if (report.deferred) {
    return (
      <div className="alert alert-warn" style={{ marginTop: 10 }}>
        <div>
          <strong>Deferred — {report.deferred}.</strong> A collection or contacts job owns the
          shared pool right now; the next tick tries again.
        </div>
      </div>
    )
  }
  if (report.enabled === false) {
    return (
      <div className="alert alert-info" style={{ marginTop: 10 }}>
        <div>The cycle is switched off, so nothing ran.</div>
      </div>
    )
  }
  if (report.skipped) {
    return (
      <div className="alert alert-info" style={{ marginTop: 10 }}>
        <div>
          <strong>Skipped: {report.skipped}.</strong>
          {report.reasons?.length ? ' The reasons are in the counters below.' : ''}
        </div>
      </div>
    )
  }
  return null
}

function PhaseSteps({ current, last }) {
  return (
    <ol className="cont-steps">
      {PHASES.map((phase, index) => {
        const done = phase === last && phase !== current
        const classes = ['cont-step']
        if (phase === current) classes.push('current')
        else if (done) classes.push('done')
        return (
          <li key={phase} className={classes.join(' ')} title={PHASE_BLURBS[phase]}>
            <span className="cont-step-dot">{done ? '✓' : index + 1}</span>
            {PHASE_LABELS[phase]}
          </li>
        )
      })}
    </ol>
  )
}

export default function ContinuousTab() {
  const [status, setStatus] = useState(null)
  const [counters, setCounters] = useState(null)
  const [loading, setLoading] = useState(true)
  const [pollError, setPollError] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [report, setReport] = useState(null)
  const [scheduled, setScheduled] = useState(null)
  const [phase, setPhase] = useState('')
  const [draftInterval, setDraftInterval] = useState('')
  const [syncedFrom, setSyncedFrom] = useState(null)

  // `alive` stops a slow response writing into an unmounted tab; `mutating`
  // keeps a poll from overwriting the state a PUT/POST is about to return.
  const alive = useRef(true)
  const mutating = useRef(false)

  const refresh = useCallback(async (initial = false) => {
    if (initial) setLoading(true)
    try {
      const [state, counts] = await Promise.all([
        api.get('/admin/continuous'),
        api.get('/overview/counters?fresh=1'),
      ])
      if (!alive.current) return
      setStatus(state)
      setCounters(counts)
      setPollError(null)
    } catch (err) {
      if (alive.current) setPollError(err)
    } finally {
      if (initial && alive.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    alive.current = true
    refresh(true)
    const id = setInterval(() => {
      if (document.hidden || mutating.current) return
      refresh()
    }, POLL_MS)
    // Coming back to a visible tab should not wait out the current interval.
    const onVisible = () => {
      if (!document.hidden && !mutating.current) refresh()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      alive.current = false
      clearInterval(id)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [refresh])

  // The form is seeded from the server value and re-seeded only when the
  // server value changes, so a poll never overwrites what is being typed.
  if (status && status.interval_seconds !== syncedFrom) {
    setSyncedFrom(status.interval_seconds)
    setDraftInterval(String(status.interval_seconds))
  }

  const cursor = status?.seeker_cursor || null
  const cursorUser = useFetch(
    () => (cursor ? api.get(`/admin/users/${encodeURIComponent(cursor)}`) : Promise.resolve(null)),
    [cursor],
  )

  if (loading && !status) return <Loading rows={5} />
  if (pollError && !status) return <ErrorBox error={pollError} onRetry={() => refresh(true)} />

  const enabled = Boolean(status?.enabled)
  const interval = parseInterval(draftInterval)
  const savedInterval = status?.interval_seconds
  const intervalChanged = !interval.error && interval.value !== savedInterval
  const intervalLabel = interval.error ? null : formatDuration(interval.value)
  const lastReport = status?.last_report || null
  const scheduler = status?.scheduler || null
  const cursorEmail = cursorUser.data?.email || null
  const cursorName = cursorUser.data?.display_name || null

  async function apply(nextEnabled, nextInterval) {
    if (busy) return
    setBusy(true)
    setActionError(null)
    mutating.current = true
    try {
      const payload = { enabled: nextEnabled }
      if (nextInterval != null) payload.interval_seconds = nextInterval
      const state = await api.put('/admin/continuous', payload)
      if (alive.current) setStatus(state)
    } catch (err) {
      if (alive.current) setActionError(err)
    } finally {
      mutating.current = false
      if (alive.current) setBusy(false)
      if (alive.current) refresh()
    }
  }

  function toggle() {
    // A malformed interval would be refused by the API; keep it out of the
    // switch's PUT and say so in the field instead.
    if (interval.error) return
    apply(!enabled, interval.value)
  }

  async function runNow() {
    if (busy) return
    setBusy(true)
    setActionError(null)
    setReport(null)
    setScheduled(null)
    // Deliberately not `mutating`: a phase can take minutes and the poll
    // should keep the counters moving while it does. Only the PUT races the
    // status endpoint, so only the PUT pauses the poll.
    try {
      const body = { force: true }
      if (phase) body.phase = phase
      const result = await api.post('/admin/continuous/run', body)
      if (alive.current) {
        // The API schedules the phase and answers 202 immediately; the outcome
        // lands in the polled status. (Older builds returned the report.)
        if (result?.scheduled) setScheduled(result)
        else setReport(result)
      }
    } catch (err) {
      if (alive.current) setActionError(err)
    } finally {
      if (alive.current) setBusy(false)
      if (alive.current) refresh()
    }
  }

  return (
    <div className="stack">
      {pollError && <ErrorBox error={pollError} onRetry={() => refresh()} />}
      {actionError && <ErrorBox error={actionError} />}

      <SectionCard
        icon="refresh"
        title="Continuous data collection"
        phase="phase-0"
        actions={<Badge tone={enabled ? 'ok' : undefined}>{enabled ? 'On' : 'Off'}</Badge>}
      >
        <div className="row row-wrap" style={{ gap: 14, alignItems: 'center' }}>
          <label className="cont-switch">
            <input
              type="checkbox"
              checked={enabled}
              disabled={busy || Boolean(interval.error)}
              onChange={toggle}
              aria-label={
                enabled ? 'Switch continuous collection off' : 'Switch continuous collection on'
              }
            />
            <span className="cont-switch-track">
              <span className="cont-switch-thumb" />
            </span>
            <span className="cont-switch-text">{enabled ? 'Running' : 'Stopped'}</span>
          </label>
          <span className="small muted">
            {enabled
              ? `One phase every ${formatDuration(savedInterval)} — it keeps running until this switch is turned off.`
              : 'Switch on to expand the corpus in the background, one phase at a time.'}
          </span>
        </div>

        <div className="alert alert-warn" style={{ marginTop: 12 }}>
          <div>
            <strong>{enabled ? 'Continuous collection is on.' : 'Before you switch this on.'}</strong>{' '}
            The cycle never stops on its own: it walks Discover → Contacts → Enrich → Score and
            repeats every {formatDuration(savedInterval)} until the switch is turned off. Each
            phase can start jobs on the shared job pool and make real network requests (job
            boards, company sites, filings, mail-domain lookups) at the politeness caps set on
            the Sources tab, and enrichment can spend model tokens. Turning it off stops new
            phases; a job already started finishes on its own.
          </div>
        </div>

        <Field
          label={
            <span className="row" style={{ gap: 6, alignItems: 'center' }}>
              Interval between phases
              <HelpTip title="What the interval does" align="left">
                How long the engine waits after one phase before running the next. The
                scheduler ticks every five minutes and the engine refuses to run early, so
                very short intervals are still paced by the tick. Minimum 5 minutes, maximum
                24 hours.
              </HelpTip>
            </span>
          }
          hint={
            intervalLabel
              ? `Roughly every ${intervalLabel}. Saved with the switch; use Save interval to change it while running.`
              : `Whole seconds between ${MIN_INTERVAL} (5 minutes) and ${MAX_INTERVAL} (24 hours).`
          }
        >
          <div className="row row-wrap" style={{ gap: 8, alignItems: 'center' }}>
            <input
              className="cont-interval-input"
              type="number"
              min={MIN_INTERVAL}
              max={MAX_INTERVAL}
              step={60}
              value={draftInterval}
              disabled={busy}
              onChange={(e) => setDraftInterval(e.target.value)}
              aria-invalid={interval.error ? 'true' : undefined}
            />
            <span className="small muted">{intervalLabel ? `= ${intervalLabel}` : 'invalid'}</span>
            {intervalChanged && (
              <button
                className="btn btn-sm"
                disabled={busy}
                onClick={() => apply(enabled, interval.value)}
              >
                Save interval
              </button>
            )}
          </div>
          {interval.error && <span className="cont-invalid">{interval.error}</span>}
        </Field>
      </SectionCard>

      <SectionCard
        icon="pipeline"
        title="Cycle status"
        phase="phase-0"
        actions={
          <span className="small muted">
            Next phase: {PHASE_LABELS[status?.phase] || status?.phase || '–'}
          </span>
        }
      >
        <PhaseSteps current={status?.phase} last={lastReport?.phase} />

        <div className="row row-wrap" style={{ gap: 16 }}>
          <span className="small">
            <span className="muted">Seeker cursor: </span>
            <strong>{cursorEmail || cursor || '–'}</strong>
            {cursorName && <span className="muted"> · {cursorName}</span>}
          </span>
          <span className="small" title={stamp(status?.last_tick)}>
            <span className="muted">Last tick: </span>
            <strong>{relativeTime(status?.last_tick) || 'never'}</strong>
          </span>
          <span className="small" title={stamp(status?.next_due)}>
            <span className="muted">Next due: </span>
            <strong>{relativeTime(status?.next_due) || '–'}</strong>
          </span>
        </div>

        <p className="small muted" style={{ margin: '8px 0 0' }}>
          {scheduler?.task ? (
            <>
              Scheduler task <span className="mono">{scheduler.task}</span> ticks every{' '}
              {formatDuration(scheduler.interval_seconds)}, is{' '}
              {scheduler.enabled ? 'enabled' : 'disabled'}, and last ran{' '}
              {relativeTime(scheduler.last_run) || 'never'}.
            </>
          ) : (
            'The scheduler task is not loaded in this process, so phases start on the scheduler’s next tick.'
          )}
        </p>

        {lastReport ? (
          <>
            <div className="row row-wrap" style={{ marginTop: 14, gap: 8, alignItems: 'center' }}>
              <strong className="small">Last phase report</strong>
              {lastReport.phase && <Badge tone="accent">{lastReport.phase}</Badge>}
              {lastReport.at && (
                <span className="tiny muted" title={stamp(lastReport.at)}>
                  {relativeTime(lastReport.at)}
                </span>
              )}
            </div>
            <OutcomeBanner report={lastReport} />
            <ReportEntries report={lastReport} />
          </>
        ) : (
          <p className="small muted" style={{ marginTop: 14, marginBottom: 0 }}>
            No phase has run yet. The first one starts on the next scheduler tick after the
            switch is on, or run one below.
          </p>
        )}
      </SectionCard>

      <SectionCard
        icon="companies"
        title="Corpus now"
        phase="phase-0"
        actions={
          <span className="small muted" title={stamp(counters?.at)}>
            as of {relativeTime(counters?.at) || 'just now'} · refreshed with the poll, no cache
          </span>
        }
      >
        <div className="grid grid-4">
          <Stat icon="companies" label="Companies" value={num(counters?.companies)} phase="phase-0" />
          <Stat icon="vacancy" label="Jobs" value={num(counters?.jobs)} phase="phase-0" />
          <Stat icon="contacts" label="Contacts" value={num(counters?.contacts)} phase="phase-0" />
          <Stat
            icon="opportunities"
            label="Opportunities"
            value={num(counters?.opportunities)}
            phase="phase-0"
          />
        </div>
      </SectionCard>

      <SectionCard icon="play" title="Run once" phase="phase-0">
        <p className="small muted" style={{ marginTop: 0 }}>
          Runs immediately and returns its report, whether or not the cycle is on and whether or
          not the phase is due. It does not move the cycle: the next scheduled tick still runs its
          own phase.
        </p>
        <div className="row row-wrap" style={{ gap: 8, alignItems: 'center' }}>
          <label className="small muted" htmlFor="cont-phase">
            Phase
          </label>
          <select
            id="cont-phase"
            value={phase}
            disabled={busy}
            onChange={(e) => setPhase(e.target.value)}
          >
            <option value="">
              Next in the cycle ({PHASE_LABELS[status?.phase] || status?.phase || '–'})
            </option>
            {PHASES.map((p) => (
              <option key={p} value={p}>
                {PHASE_LABELS[p]}
              </option>
            ))}
          </select>
          <button className="btn btn-primary" disabled={busy} onClick={runNow}>
            {busy ? 'Working…' : 'Run the next phase now'}
          </button>
        </div>
        <p className="small muted" style={{ marginBottom: 0 }}>
          It uses the shared job pool: a heavy collection or contacts job defers it rather than
          letting two runs compete.
        </p>

        {scheduled && (
          <p className="small" style={{ marginBottom: 0 }}>
            {(PHASE_LABELS[scheduled.phase] || scheduled.phase || 'The phase')} started in the
            background — watch the phase, last report and totals above update as it runs.
          </p>
        )}

        {report && (
          <div className="cont-run-result">
            <div className="row row-wrap" style={{ gap: 8, alignItems: 'center' }}>
              <strong className="small">Result</strong>
              {report.phase && <Badge tone="accent">{report.phase}</Badge>}
              {report.at && (
                <span className="tiny muted" title={stamp(report.at)}>
                  {stamp(report.at)}
                </span>
              )}
            </div>
            <OutcomeBanner report={report} />
            <ReportEntries report={report} />
          </div>
        )}
      </SectionCard>
    </div>
  )
}
