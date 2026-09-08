/**
 * Follow-ups (FR-327).
 *
 * One follow-up per application, never two, and never without the job seeker
 * reading it first (FR-324). The draft is generated on request and shown in
 * full for editing; sending it threads it onto the original message so it
 * arrives in the same conversation rather than as a second cold email.
 *
 * The interval is the one mail setting the API actually accepts a write for -
 * the pace, the cap and the window are deployment configuration - so this is
 * the only editable control on the mail screen.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, Empty, ErrorBox, Loading, Modal, SectionCard } from '../../components/ui'
import { daysUntil, formatWhen } from './vocabulary'

export default function FollowUps({ followUps, settings, onSettingsSaved }) {
  const rows = followUps.data || []

  return (
    <div className="col" style={{ gap: 14 }}>
      <IntervalCard settings={settings} onSaved={onSettingsSaved} />
      <DueList followUps={followUps} rows={rows} />
    </div>
  )
}

/* --- The one writable setting --------------------------------------------- */

function IntervalCard({ settings, onSaved }) {
  const [days, setDays] = useState(String(settings?.follow_up_days ?? 7))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [saved, setSaved] = useState(false)

  const value = Number(days)
  const valid = Number.isFinite(value) && value >= 1 && value <= 90
  const dirty = valid && value !== settings?.follow_up_days

  async function save(e) {
    e.preventDefault()
    setSaving(true)
    setError(null)
    try {
      const res = await api.put('/mail/settings', { follow_up_days: value })
      setDays(String(res.follow_up_days))
      setSaved(true)
      onSaved?.()
    } catch (err) {
      setError(err)
    } finally {
      setSaving(false)
    }
  }

  return (
    <SectionCard icon="clock" title="When a follow-up becomes due" phase="phase-0">
      <form onSubmit={save}>
        <div className="field" style={{ maxWidth: 260 }}>
          <label>
            Days of silence before a follow-up
            <HelpTip term="follow_up" />
          </label>
          <input
            type="number"
            min={1}
            max={90}
            value={days}
            onChange={(e) => {
              setDays(e.target.value)
              setSaved(false)
            }}
          />
          <span className="hint">
            Between 1 and 90. Counted from the moment the application was delivered, and only
            for messages that were neither answered nor bounced.
          </span>
        </div>

        {saved && !dirty && <div className="alert alert-ok"><div>Saved.</div></div>}
        <ErrorBox error={error} />

        <button className="btn btn-sm" disabled={!dirty || saving}>
          {saving ? <span className="spinner" /> : 'Save'}
        </button>
      </form>
    </SectionCard>
  )
}

/* --- What is due ---------------------------------------------------------- */

function DueList({ followUps, rows }) {
  const [draft, setDraft] = useState(null) // { dispatch, subject, body, language }
  const [drafting, setDrafting] = useState(null)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [reminding, setReminding] = useState(false)

  async function makeDraft(d) {
    setDrafting(d.id)
    setError(null)
    try {
      const res = await api.post(`/mail/dispatches/${d.id}/follow-up`)
      setDraft({ dispatch: d, subject: res.subject || '', body: res.body || '', language: res.language })
    } catch (err) {
      setError(err)
    } finally {
      setDrafting(null)
    }
  }

  async function remind() {
    setReminding(true)
    setError(null)
    try {
      const res = await api.post('/mail/follow-ups/remind')
      setNotice(
        res.notifications
          ? `${res.notifications} reminder${res.notifications === 1 ? '' : 's'} raised.`
          : 'Nothing new to remind you about — each application raises one reminder only.',
      )
    } catch (err) {
      setError(err)
    } finally {
      setReminding(false)
    }
  }

  return (
    <SectionCard
      icon="responses"
      title="Follow-ups due"
      phase="phase-0"
      actions={
        <button className="btn btn-sm" onClick={remind} disabled={reminding || rows.length === 0}>
          {reminding ? <span className="spinner" /> : <Icon name="info" />} Raise reminders
        </button>
      }
    >
      <p className="small muted" style={{ marginTop: 0 }}>
        An application appears here once it has gone unanswered for the interval above, and
        leaves as soon as a reply or a bounce is detected. At most one follow-up is ever sent
        for an application.
      </p>

      {notice && <div className="alert alert-info"><div>{notice}</div></div>}
      <ErrorBox error={error} />

      {followUps.loading && <Loading rows={2} />}
      <ErrorBox error={followUps.error} onRetry={followUps.reload} />

      {!followUps.loading && !followUps.error && rows.length === 0 && (
        <Empty title="Nothing is due">
          Applications that were answered, that bounced, or that are still inside the waiting
          interval are not listed here.
        </Empty>
      )}

      {rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Recipient</th>
                <th>Subject</th>
                <th>Sent</th>
                <th>Due</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((d) => {
                const overdue = (daysUntil(d.follow_up_due_at) ?? 0) < 0
                return (
                  <tr key={d.id}>
                    <td className="mono">{d.recipient_email}</td>
                    <td>{d.subject}</td>
                    <td className="nowrap">{formatWhen(d.sent_at)}</td>
                    <td className="nowrap">
                      {formatWhen(d.follow_up_due_at)}{' '}
                      {overdue && <Badge tone="warn">overdue</Badge>}
                    </td>
                    <td className="nowrap">
                      <button
                        className="btn btn-sm"
                        onClick={() => makeDraft(d)}
                        disabled={drafting === d.id}
                      >
                        {drafting === d.id ? <span className="spinner" /> : 'Draft a follow-up'}
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {draft && (
        <DraftModal
          draft={draft}
          onChange={setDraft}
          onClose={() => setDraft(null)}
          onSent={(message) => {
            setDraft(null)
            setNotice(message)
            followUps.reload()
          }}
        />
      )}
    </SectionCard>
  )
}

/* --- Review before it goes (FR-324) --------------------------------------- */

function DraftModal({ draft, onChange, onClose, onSent }) {
  const [sending, setSending] = useState(false)
  const [error, setError] = useState(null)

  async function send() {
    setSending(true)
    setError(null)
    try {
      const res = await api.post(`/mail/dispatches/${draft.dispatch.id}/follow-up/send`, {
        subject: draft.subject,
        body: draft.body,
      })
      onSent(
        res?.status === 'queued'
          ? `Queued: ${res.reason || 'the recipient’s send window is closed'}. It will go out on its own.`
          : `Follow-up sent to ${draft.dispatch.recipient_email}.`,
      )
    } catch (err) {
      setError(err)
      setSending(false)
    }
  }

  return (
    <Modal
      title={`Follow up with ${draft.dispatch.recipient_email}`}
      onClose={onClose}
      wide
      actions={
        <>
          <button className="btn btn-sm" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn btn-sm btn-primary"
            onClick={send}
            disabled={sending || !draft.body.trim() || !draft.subject.trim()}
          >
            {sending ? <span className="spinner" /> : 'Send the follow-up'}
          </button>
        </>
      }
    >
      <p className="small muted" style={{ marginTop: 0 }}>
        Written in {draft.language || 'the language of the application'} and threaded onto the
        original message, so it arrives in the same conversation. The CV is not attached again
        — a second copy in the same thread adds weight for no benefit. Nothing is sent until
        you choose to send it (FR-324).
      </p>

      <div className="field">
        <label>Subject</label>
        <input
          type="text"
          value={draft.subject}
          onChange={(e) => onChange({ ...draft, subject: e.target.value })}
        />
      </div>

      <div className="field">
        <label>Message</label>
        <textarea
          rows={10}
          value={draft.body}
          onChange={(e) => onChange({ ...draft, body: e.target.value })}
        />
        <span className="hint">
          Your signature is appended after this text; do not write a sign-off.
        </span>
      </div>

      <ErrorBox error={error} />
    </Modal>
  )
}
