/**
 * Recording a response by hand (extends FR-326).
 *
 * The picking list is GET /api/learning/responses/awaiting - every sent
 * application with nothing recorded against it - and the form below it is the
 * body of POST /api/learning/responses.
 *
 * A rejection is one chip among seven, styled exactly like the rest. The
 * segment analysis (FR-425) is worthless without rejections, and a control
 * that makes recording one feel like an admission is a control people avoid.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import { ErrorBox, Field, KindBadge, formatDate } from '../../components/ui'
import { CHANNELS, OUTCOMES, SILENCE_DAYS, daysSince, todayISODate } from './vocabulary'

/**
 * The entry form for one application. Every outcome is the same control with
 * the same weight — see the file header.
 */
export function RecordForm({ target, onCancel, onRecorded }) {
  const [channel, setChannel] = useState('email')
  const [outcome, setOutcome] = useState('')
  const [date, setDate] = useState(todayISODate())
  const [text, setText] = useState('')
  const [notes, setNotes] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function submit(e) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const received = new Date(`${date}T12:00:00`)
      const result = await api.post('/learning/responses', {
        dispatch_id: target.dispatch_id || null,
        opportunity_id: target.opportunity_id || null,
        channel,
        stated_outcome: outcome || null,
        raw_text: text,
        notes,
        received_at: Number.isNaN(received.getTime()) ? null : received.toISOString(),
        // FR-422: the text is still read for detail — proposed times, who to
        // contact instead — even though the stated outcome overrides it.
        classify: true,
      })
      onRecorded(result)
    } catch (err) {
      setError(err)
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} style={{ marginTop: 14 }}>
      <div className="row row-wrap" style={{ marginBottom: 12 }}>
        <div>
          <strong>{target.company_name || 'Unknown company'}</strong>
          <span className="muted"> · {target.opportunity_title || target.subject || 'Application'}</span>
        </div>
        {target.opportunity_kind && <KindBadge kind={target.opportunity_kind} />}
        <div className="spacer" />
        <button type="button" className="btn btn-sm btn-ghost" onClick={onCancel}>
          Choose a different application
        </button>
      </div>

      {error && <ErrorBox error={error} />}

      <div className="grid grid-2">
        <Field
          label={
            <>
              How it reached you
              <HelpTip term="response_channel" />
            </>
          }
          hint="The channel is kept with the response; it is not guessed from the text."
        >
          <div className="chips">
            {CHANNELS.map((c) => (
              <span
                key={c.value}
                className={`chip clickable${channel === c.value ? ' on' : ''}`}
                onClick={() => setChannel(c.value)}
              >
                {c.label}
              </span>
            ))}
          </div>
        </Field>

        <Field
          label={
            <>
              The date it arrived
              <HelpTip title="Why the date matters">
                An application counts as silent only after {SILENCE_DAYS} days, and the
                rates are computed per resolved application. Backdating a response to when
                it actually arrived keeps those windows honest.
              </HelpTip>
            </>
          }
        >
          <input
            type="date"
            value={date}
            max={todayISODate()}
            onChange={(e) => setDate(e.target.value)}
          />
        </Field>
      </div>

      <Field
        label={
          <>
            What happened
            <HelpTip term="stated_outcome" />
          </>
        }
        hint="Your reading is what counts and overrides the model's. Leave it unset only if you genuinely cannot tell — the text will then be classified on its own."
      >
        {/* Every outcome is one chip of the same kind. A rejection is not
            styled as bad news: FR-425 needs it exactly as much as the rest. */}
        <div className="chips">
          {OUTCOMES.map((o) => (
            <span
              key={o.value}
              className={`chip clickable${outcome === o.value ? ' on' : ''}`}
              onClick={() => setOutcome(o.value)}
            >
              {o.label}
            </span>
          ))}
        </div>
      </Field>

      <Field
        label={
          <>
            The text, if you have it
            <HelpTip title="What the text is used for">
              It is read for detail — proposed interview times, requested documents, who to
              contact instead — and never treated as an instruction. Paste it as it arrived;
              a summary in your own words works too.
            </HelpTip>
          </>
        }
      >
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Paste the message, or write down what was said on the call."
          rows={6}
        />
      </Field>

      <Field label="Your own note (optional)">
        <input
          type="text"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="Who called, what tone, anything you want to remember."
        />
      </Field>

      <div className="row">
        <button className="btn btn-primary" disabled={busy}>
          {busy ? <span className="spinner" /> : 'Record this response'}
        </button>
        <button type="button" className="btn btn-ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </form>
  )
}

/** The picking list: sent applications with nothing recorded against them. */
export function AwaitingPicker({ rows, onPick }) {
  const [q, setQ] = useState('')
  const needle = q.trim().toLowerCase()
  const matches = needle
    ? rows.filter((r) =>
        [r.company_name, r.opportunity_title, r.recipient_name, r.recipient_email, r.subject]
          .filter(Boolean)
          .some((v) => v.toLowerCase().includes(needle)),
      )
    : rows

  return (
    <>
      <div className="row" style={{ marginBottom: 10 }}>
        <input
          type="text"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Filter by company, role or recipient…"
          style={{ maxWidth: 340 }}
        />
        <span className="small muted">
          {matches.length} of {rows.length} awaiting a response
        </span>
      </div>

      <div className="table-wrap" style={{ maxHeight: 340, overflowY: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th>Company and role</th>
              <th>Sent to</th>
              <th>Went out</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {matches.map((r) => {
              const age = daysSince(r.sent_at)
              return (
                <tr key={r.dispatch_id}>
                  <td>
                    <div className="row row-wrap" style={{ gap: 6 }}>
                      <strong>{r.company_name || 'Unknown company'}</strong>
                      {r.opportunity_kind && <KindBadge kind={r.opportunity_kind} />}
                    </div>
                    <div className="small muted">{r.opportunity_title || r.subject || '–'}</div>
                  </td>
                  <td>
                    <div>{r.recipient_name || r.recipient_email || '–'}</div>
                    {r.recipient_name && r.recipient_email && (
                      <div className="tiny muted">{r.recipient_email}</div>
                    )}
                  </td>
                  <td className="nowrap">
                    <div>{formatDate(r.sent_at)}</div>
                    <div className="tiny muted">
                      {age == null ? '' : age === 0 ? 'today' : `${age} days ago`}
                      {age != null && age >= SILENCE_DAYS ? ' · counts as silence' : ''}
                    </div>
                  </td>
                  <td className="nowrap">
                    <button className="btn btn-sm" onClick={() => onPick(r)}>
                      Record a response
                    </button>
                  </td>
                </tr>
              )
            })}
            {!matches.length && (
              <tr>
                <td colSpan={4} className="muted small">
                  Nothing matches “{q}”.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  )
}
