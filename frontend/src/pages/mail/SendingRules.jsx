/**
 * The sending guard rails (FR-325, RK-05).
 *
 * Three rails bound every dispatch: a minimum gap between messages, a daily
 * ceiling, and a window of hours. They are enforced in the dispatcher, so the
 * numbers here are a report of what is in force rather than a form that
 * decides it - a client cannot talk its way past a window by calling the API
 * directly, and it cannot raise a cap from this screen either.
 *
 * The window is the one people misread, and misreading it is expensive: it is
 * applied in the *recipient's* time zone, worked out from the company's
 * country and locations, not in yours.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { Caution, HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, ErrorBox, Modal, SectionCard, formatDuration } from '../../components/ui'

const BACKEND_NAME = { gmail_oauth: 'Gmail (OAuth)', resend: 'Resend (stepvda.com relay)' }

export default function SendingRules({ settings, queued, onProcessed }) {
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [result, setResult] = useState(null)

  async function processQueue() {
    setBusy(true)
    setError(null)
    try {
      // Outward-facing: this puts real mail on the wire, so it is confirmed
      // first (house rule) even though the rails are re-checked per message.
      const res = await api.post('/mail/queue/process', undefined)
      setResult(res)
      setConfirm(false)
      onProcessed?.()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  const s = settings || {}
  const held = result?.results?.filter((r) => r.status === 'held').length ?? 0
  const sent = result?.results?.filter((r) => r.status === 'sent').length ?? 0

  return (
    <div className="col" style={{ gap: 14 }}>
      {/* RK-05: pacing and caps are what keep this mail out of a spam folder. */}
      <Caution title="These limits protect your sender reputation">
        A new sending address has no reputation to spend. Pacing, a daily ceiling and office
        hours are what make a run of applications look like a person writing letters rather
        than a bulk campaign — and raising them sharply is the fastest way to have everything
        you send classified as spam, including the applications that already went out. If you
        need more volume, raise the cap by a few messages a day and watch the bounce rate,
        never by an order of magnitude at once.
      </Caution>

      <SectionCard icon="clock" title="Guard rails in force" phase="phase-0">
        <dl className="mail-kv">
          <dt>
            Pace between sends
            <HelpTip term="send_pacing" />
          </dt>
          <dd>
            <strong>{formatDuration(s.send_min_interval_seconds)}</strong>{' '}
            <span className="small muted">
              minimum gap; a message that arrives sooner waits rather than being dropped.
            </span>
          </dd>

          <dt>
            Daily cap
            <HelpTip term="daily_cap" />
          </dt>
          <dd>
            <strong>{s.send_daily_cap ?? '–'}</strong>{' '}
            <span className="small muted">applications a day, counted per calendar day in UTC.</span>
          </dd>

          <dt>
            Send window
            <HelpTip term="send_window" />
          </dt>
          <dd>
            <strong>
              {s.send_window_start ?? '–'} – {s.send_window_end ?? '–'}
            </strong>{' '}
            {s.window_timezone === 'recipient' && (
              <Badge tone="info">in the recipient’s time zone</Badge>
            )}
            <div className="small muted" style={{ marginTop: 4 }}>
              The zone is derived from the company’s country and locations, so a Belgian
              application and a Californian one leave at different moments of your day. Outside
              the window a send is queued, not refused, and goes out when the window opens.
            </div>
          </dd>

          <dt>Default backend</dt>
          <dd>
            {BACKEND_NAME[s.default_backend] || s.default_backend || '–'}{' '}
            <span className="small muted">
              A connected personal mailbox always outranks the relay, whatever this says.
            </span>
          </dd>
        </dl>

        <p className="small muted" style={{ marginTop: 14, marginBottom: 0 }}>
          These three come from the server’s configuration rather than from this screen. That is
          deliberate: changing them takes an edit to <span className="mono">.env</span> and a
          restart, which is a slower path than a slider and a worse day to regret.
        </p>
      </SectionCard>

      <SectionCard
        icon="send"
        title="Queued sends"
        phase="phase-0"
        actions={
          queued > 0 ? <Badge tone="info">{queued} waiting</Badge> : <Badge>none waiting</Badge>
        }
      >
        <p className="small muted" style={{ marginTop: 0 }}>
          A message held for the pace, the cap or a closed window sits in the queue with the
          moment it may go. Processing the queue re-checks every rail per message — the cap and
          the pacing both move as the batch drains — so what is still blocked stays blocked.
        </p>

        {result && (
          <div className="alert alert-ok">
            <div>
              Processed {result.processed ?? 0}: {sent} sent, {held} still held.
              {held > 0 && ' The held ones name their reason in the send log.'}
            </div>
          </div>
        )}
        <ErrorBox error={error} />

        <button className="btn btn-sm" onClick={() => setConfirm(true)} disabled={!queued}>
          <Icon name="send" /> Send the queue now
        </button>
      </SectionCard>

      {confirm && (
        <Modal
          title="Send the queued applications?"
          onClose={() => setConfirm(false)}
          actions={
            <>
              <button className="btn btn-sm" onClick={() => setConfirm(false)}>
                Cancel
              </button>
              <button className="btn btn-sm btn-primary" onClick={processQueue} disabled={busy}>
                {busy ? <span className="spinner" /> : 'Send them'}
              </button>
            </>
          }
        >
          <p>
            Up to {queued} message{queued === 1 ? '' : 's'} will be delivered to real people from
            your own address. Each one is re-checked against the pace, the daily cap, the
            recipient’s send window and the objection list first; anything that fails a check
            stays in the queue.
          </p>
          <p className="small muted">
            This is not a way around the window — it only sends what is already allowed to go.
          </p>
        </Modal>
      )}
    </div>
  )
}
