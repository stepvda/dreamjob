/**
 * Reading this installation's own logs (NFR-701, FR-361).
 *
 * The backend writes six files and answers `GET /api/logs/summary` and
 * `GET /api/logs/tail` over them. Without this tab those endpoints exist and
 * nobody reads them: observability that can only be reached with curl is
 * observability an operator does not have.
 *
 * The two halves answer different questions and are therefore separate:
 *
 *   the summary  — "is anything wrong?"  Counts by level over a window, so the
 *                  screen can say "12 warnings in the last hour" rather than
 *                  making somebody read a file to find out.
 *   the tail     — "what exactly happened?"  The last N lines of one named
 *                  file, coloured by level, filterable by level and by text.
 *
 * The level of a line is parsed here rather than requested from the server,
 * because the endpoint hands back the file's own lines and the file is the
 * record. Both of the formatter's shapes are understood — the aligned columns
 * of `HumanFormatter` and the one-object-per-line of `JsonFormatter` — and a
 * traceback's indented continuation lines inherit the level of the event they
 * belong to, so an exception stays one coloured block instead of turning grey
 * halfway down.
 *
 * Nothing here polls. A log view that refreshes itself is a log view that
 * scrolls away from whatever the operator was reading; the refresh is a
 * button.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  SectionCard,
  Stat,
  useFetch,
} from '../../components/ui'
import { num } from './format'

/** The files the backend will serve, in the order an operator reads them. */
const FILES = [
  { key: 'app', label: 'Application', hint: 'Everything that is not one of the channels below.' },
  { key: 'requests', label: 'Requests', hint: 'One line per HTTP request: method, path, status, duration.' },
  { key: 'database', label: 'Database', hint: 'Statements the database layer thought worth reporting, slow ones included.' },
  { key: 'errors', label: 'Errors', hint: 'Every warning and worse, copied out of the file it came from.' },
  { key: 'frontend', label: 'Browser', hint: 'What the SPA shipped back from real browsers (NFR-701).' },
  { key: 'audit', label: 'Audit', hint: 'The audit trail mirrored to a file (NFR-702).' },
]

const WINDOWS = [
  { minutes: 15, label: 'last 15 minutes' },
  { minutes: 60, label: 'last hour' },
  { minutes: 60 * 24, label: 'last 24 hours' },
  { minutes: 60 * 24 * 7, label: 'last 7 days' },
]

const LINE_COUNTS = [100, 200, 500, 1000, 2000]

/** Levels loudest first — the order the summary reads in. */
const LEVELS = ['CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG']

const LEVEL_TONE = {
  CRITICAL: 'danger',
  ERROR: 'danger',
  WARNING: 'warn',
  INFO: 'info',
  DEBUG: undefined,
}

/** Plurals that read as English: "Criticals" does not. */
const LEVEL_LABEL = {
  CRITICAL: 'Critical',
  ERROR: 'Errors',
  WARNING: 'Warnings',
  INFO: 'Info',
  DEBUG: 'Debug',
}

/** Singular and plural, for the sentence at the top of the summary. */
const LEVEL_NOUN = {
  CRITICAL: ['critical event', 'critical events'],
  ERROR: ['error', 'errors'],
  WARNING: ['warning', 'warnings'],
}

const LEVEL_ICON = {
  CRITICAL: 'error',
  ERROR: 'error',
  WARNING: 'warning',
  INFO: 'info',
  DEBUG: 'filter',
}

/** The formatter abbreviates so every level is five characters wide. */
const LEVEL_ALIAS = { WARN: 'WARNING', CRIT: 'CRITICAL' }

const HUMAN_LINE =
  /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) {2}(\S+) +(\S+) +(\S+) {2}([\s\S]*)$/

export default function LogsTab() {
  const [file, setFile] = useState('app')
  const [minutes, setMinutes] = useState(60)
  const [count, setCount] = useState(200)
  const [level, setLevel] = useState('')
  // The text filter is applied on submit rather than on every keystroke: each
  // change is a request that reads the end of a file on the server.
  const [draft, setDraft] = useState('')
  const [contains, setContains] = useState('')

  const summary = useFetch(() => api.get(`/logs/summary?minutes=${minutes}`), [minutes])
  const tail = useFetch(() => {
    const q = new URLSearchParams({ name: file, lines: String(count) })
    if (level) q.set('level', level)
    if (contains) q.set('contains', contains)
    return api.get(`/logs/tail?${q.toString()}`)
  }, [file, count, level, contains])

  return (
    <div className="stack">
      <LevelSummary
        summary={summary}
        minutes={minutes}
        onWindow={setMinutes}
        onOpenFile={(name) => {
          setFile(name)
          setLevel('')
        }}
      />

      <SectionCard
        icon="document"
        title={
          <>
            The end of one file
            <HelpTip term="log_tail" align="right" />
          </>
        }
        phase="phase-0"
        actions={
          <button className="btn btn-sm" onClick={tail.reload} disabled={tail.loading}>
            <Icon name="refresh" /> Refresh
          </button>
        }
      >
        <div className="chips" style={{ marginBottom: 10 }}>
          {FILES.map((f) => (
            <button
              key={f.key}
              type="button"
              title={f.hint}
              className={`chip clickable${file === f.key ? ' on' : ''}`}
              onClick={() => setFile(f.key)}
            >
              {f.label}
            </button>
          ))}
        </div>

        <form
          className="row row-wrap"
          style={{ marginBottom: 12 }}
          onSubmit={(e) => {
            e.preventDefault()
            setContains(draft.trim())
          }}
        >
          <select
            value={level}
            onChange={(e) => setLevel(e.target.value)}
            aria-label="Lowest level to show"
            style={{ maxWidth: 200 }}
          >
            <option value="">Every level</option>
            {LEVELS.map((l) => (
              <option key={l} value={l}>
                {titleCase(l)} and worse
              </option>
            ))}
          </select>
          <select
            value={count}
            onChange={(e) => setCount(Number(e.target.value))}
            aria-label="How many lines"
            style={{ maxWidth: 160 }}
          >
            {LINE_COUNTS.map((n) => (
              <option key={n} value={n}>
                Last {num(n)} lines
              </option>
            ))}
          </select>
          <input
            type="text"
            value={draft}
            placeholder="Contains…"
            aria-label="Keep only lines containing this text"
            onChange={(e) => setDraft(e.target.value)}
            style={{ maxWidth: 260 }}
          />
          <button className="btn btn-sm" type="submit">
            Filter
          </button>
          {contains && (
            <button
              className="btn btn-sm btn-ghost"
              type="button"
              onClick={() => {
                setDraft('')
                setContains('')
              }}
            >
              Clear
            </button>
          )}
        </form>

        <p className="small muted" style={{ marginTop: 0 }}>
          Each line is written as timestamp, level, the part of the application that wrote
          it
          <HelpTip term="log_channel" />, the correlation id of the request that caused it
          <HelpTip term="correlation_id" />, and the event itself.
        </p>

        <TailView tail={tail} file={file} level={level} contains={contains} />
      </SectionCard>
    </div>
  )
}

/* --- "12 warnings in the last hour" (NFR-701) ------------------------------ */

function LevelSummary({ summary, minutes, onWindow, onOpenFile }) {
  const window = WINDOWS.find((w) => w.minutes === minutes) || WINDOWS[1]

  return (
    <SectionCard
      icon="chart"
      title={
        <>
          What the logs have been saying
          <HelpTip term="log_level" />
        </>
      }
      phase="phase-0"
      actions={
        <select
          value={minutes}
          onChange={(e) => onWindow(Number(e.target.value))}
          aria-label="Window to summarise"
          style={{ maxWidth: 190 }}
        >
          {WINDOWS.map((w) => (
            <option key={w.minutes} value={w.minutes}>
              {titleCase(w.label)}
            </option>
          ))}
        </select>
      }
    >
      {summary.loading ? (
        <Loading rows={3} />
      ) : summary.error ? (
        <ErrorBox error={summary.error} onRetry={summary.reload} />
      ) : (
        <>
          <p className="small muted" style={{ marginTop: 0 }}>
            {headline(summary.data.totals, window.label)} Counted from{' '}
            {summary.data.since.replace('T', ' ')}. The errors file is left out of the
            totals — every line in it is already counted in the file it was copied from.
          </p>

          <div className="grid grid-4" style={{ marginBottom: 14 }}>
            {LEVELS.map((l) => (
              <Stat
                key={l}
                icon={LEVEL_ICON[l]}
                label={LEVEL_LABEL[l]}
                value={num(summary.data.totals[l] ?? 0)}
                phase="phase-0"
              />
            ))}
          </div>

          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>File</th>
                  {LEVELS.map((l) => (
                    <th key={l} style={{ textAlign: 'right' }}>
                      {titleCase(l)}
                    </th>
                  ))}
                  <th style={{ textAlign: 'right' }}>Size</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {FILES.map((f) => {
                  const counts = summary.data.files[f.key] || {}
                  const scanned =
                    (summary.data.scanned_files || []).find((s) => s.name === f.key) || {}
                  return (
                    <tr key={f.key}>
                      <td>
                        <div style={{ fontWeight: 600 }}>{f.label}</div>
                        <div className="mono tiny muted">{f.key}.log</div>
                      </td>
                      {LEVELS.map((l) => (
                        <td key={l} style={{ textAlign: 'right' }} className="small">
                          {counts[l] ? (
                            <Badge tone={LEVEL_TONE[l]}>{num(counts[l])}</Badge>
                          ) : (
                            '–'
                          )}
                        </td>
                      ))}
                      <td style={{ textAlign: 'right' }} className="small nowrap">
                        {scanned.exists ? bytes(scanned.size_bytes) : 'not written yet'}
                        {scanned.clipped && (
                          <div className="tiny muted" title="The scan stopped before the whole window">
                            partial scan
                          </div>
                        )}
                      </td>
                      <td style={{ textAlign: 'right' }}>
                        <button className="btn btn-sm" onClick={() => onOpenFile(f.key)}>
                          Open
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          {(summary.data.recent || []).length > 0 && (
            <>
              <h4 style={{ marginBottom: 6 }}>The most recent problems</h4>
              <LogLines lines={summary.data.recent} />
            </>
          )}
        </>
      )}
    </SectionCard>
  )
}

/* --- The tail itself ------------------------------------------------------- */

function TailView({ tail, file, level, contains }) {
  if (tail.loading) return <Loading rows={6} />
  if (tail.error) return <ErrorBox error={tail.error} onRetry={tail.reload} />

  const data = tail.data
  const label = FILES.find((f) => f.key === file)?.label || file

  if (!data.lines.length) {
    return (
      <Empty title={`Nothing in ${label.toLowerCase()} matches`}>
        {level || contains
          ? 'The file has lines in it, but none of them pass this filter. Widen the level or clear the text.'
          : 'The file is empty, or has not been written to yet. Nothing has gone wrong — there is simply nothing to read.'}
      </Empty>
    )
  }

  return (
    <>
      <div className="row row-wrap small muted" style={{ marginBottom: 8 }}>
        <span>
          {num(data.returned)} line{data.returned === 1 ? '' : 's'} from {data.name}.log
        </span>
        <span>{bytes(data.size_bytes)} on disk</span>
        {data.truncated && <Badge tone="info">older lines not read</Badge>}
        <div className="spacer" />
        <span>Oldest first, newest at the bottom.</span>
      </div>
      <LogLines lines={data.lines} />
    </>
  )
}

/**
 * The lines, one per row, coloured by their own level.
 *
 * The columns are kept as the file wrote them rather than re-laid-out into a
 * table: an operator who greps this file at three in the morning should be
 * looking at the same shape they saw here.
 */
function LogLines({ lines }) {
  let carried = ''
  const parsed = lines.map((raw) => {
    const line = parse(raw)
    if (line.level) carried = line.level
    else line.level = carried
    return line
  })

  return (
    <div className="log-view">
      {parsed.map((line, i) => (
        <div key={i} className={`log-line log-${(line.level || 'plain').toLowerCase()}`}>
          {line.ts && <span className="log-ts">{line.ts}</span>}
          {line.badge && <span className="log-level">{line.badge}</span>}
          {line.channel && <span className="log-channel">{line.channel}</span>}
          {line.cid && <span className="log-cid">{line.cid}</span>}
          <span className="log-msg">{line.message}</span>
        </div>
      ))}
    </div>
  )
}

/**
 * One line, in whichever of the two formats the installation is configured for
 * (`DREAMJOB_LOG_FORMAT`). Anything unrecognised — a traceback's continuation,
 * a line from a library that wrote straight to the file — is shown verbatim
 * rather than dropped, because a log view that hides what it cannot parse is
 * worse than no log view at all.
 */
function parse(raw) {
  if (raw.startsWith('{')) {
    try {
      const payload = JSON.parse(raw)
      const level = LEVEL_ALIAS[payload.level] || payload.level || ''
      return {
        ts: (payload.ts || '').replace('T', ' '),
        level,
        badge: level ? level.slice(0, 5) : '',
        channel: payload.channel || '',
        cid: payload.cid || '',
        message: [payload.msg, payload.exception].filter(Boolean).join('\n'),
      }
    } catch {
      return { ts: '', level: '', badge: '', channel: '', cid: '', message: raw }
    }
  }

  const match = HUMAN_LINE.exec(raw)
  if (!match) return { ts: '', level: '', badge: '', channel: '', cid: '', message: raw }
  const [, ts, badge, channel, cid, message] = match
  return {
    ts,
    level: LEVEL_ALIAS[badge] || badge,
    badge,
    channel,
    cid: cid.startsWith('-') ? '' : cid,
    message,
  }
}

/* --- Small helpers --------------------------------------------------------- */

/**
 * The sentence NFR-701 is really asking for. Silence is stated as silence:
 * "no warnings and no errors" is a finding, not an empty result.
 */
function headline(totals, windowLabel) {
  const parts = []
  for (const level of ['CRITICAL', 'ERROR', 'WARNING']) {
    const n = totals[level] || 0
    if (n) parts.push(`${num(n)} ${LEVEL_NOUN[level][n === 1 ? 0 : 1]}`)
  }
  if (!parts.length) return `No warnings and no errors in the ${windowLabel}.`
  return `${sentence(parts)} in the ${windowLabel}.`
}

function sentence(parts) {
  if (parts.length === 1) return capitalise(parts[0])
  return capitalise(`${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`)
}

function capitalise(text) {
  return text.charAt(0).toUpperCase() + text.slice(1)
}

function titleCase(text) {
  return text.charAt(0).toUpperCase() + text.slice(1).toLowerCase()
}

function bytes(value) {
  if (value == null) return '–'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} kB`
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}
