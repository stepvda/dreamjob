/**
 * The dispatch log (FR-326).
 *
 * FR-326 names exactly what has to be recorded for every message: recipient,
 * time, attachments, message id, delivery status, and what came back - bounces
 * and replies. All of it is here, and the message id is shown rather than
 * hidden because it is the thing that ties a reply in a mailbox back to the
 * application that provoked it.
 *
 * Replies and bounces are only ever found in a mailbox Dream Job can read, so
 * "check for replies" is offered only when a Gmail mailbox is connected;
 * anything sent through Resend reports by webhook and is answered in the job
 * seeker's own inbox, out of sight.
 */

import { useMemo, useState } from 'react'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, Empty, ErrorBox, Loading, Modal, SectionCard, Tabs } from '../../components/ui'
import { DELIVERY_TONE, deliveryLabel, formatWhen } from './vocabulary'

const STATUS_ORDER = ['queued', 'sent', 'delivered', 'bounced', 'failed']

export default function DispatchLog({ dispatches, replies, counts, canPoll }) {
  const [tab, setTab] = useState('all')
  const [open, setOpen] = useState(null)
  const [polling, setPolling] = useState(false)
  const [pollError, setPollError] = useState(null)
  const [pollResult, setPollResult] = useState(null)

  const rows = dispatches.data || []

  const tabs = useMemo(() => {
    const present = STATUS_ORDER.filter((s) => (counts?.[s] ?? 0) > 0)
    return [
      { key: 'all', label: 'All', count: rows.length },
      ...present.map((s) => ({ key: s, label: deliveryLabel(s), count: counts[s] })),
    ]
  }, [counts, rows.length])

  const shown = tab === 'all' ? rows : rows.filter((r) => r.delivery_status === tab)

  async function poll() {
    setPolling(true)
    setPollError(null)
    try {
      const res = await api.post('/mail/poll', undefined)
      setPollResult(res?.accounts || [])
      dispatches.reload()
      replies.reload()
    } catch (err) {
      setPollError(err)
    } finally {
      setPolling(false)
    }
  }

  return (
    <div className="col" style={{ gap: 14 }}>
      <SectionCard
        icon="send"
        title="Everything sent"
        phase="phase-0"
        actions={
          canPoll && (
            <button className="btn btn-sm" onClick={poll} disabled={polling}>
              {polling ? <span className="spinner" /> : <Icon name="refresh" />} Check for replies
              and bounces
            </button>
          )
        }
      >
        <p className="small muted" style={{ marginTop: 0 }}>
          One row per message, kept whatever happened to it. The message id is the RFC 5322
          header the composer generated before the send, which is what lets a reply be matched
          back to the application even when the provider timed out.
        </p>

        {pollResult && (
          <div className="alert alert-ok">
            <div>
              {pollResult.length === 0
                ? 'No mailbox is connected to poll.'
                : pollResult.map((a) => (
                    <div key={a.account_id}>
                      {a.error ? (
                        <>Polling failed: {a.error}</>
                      ) : a.skipped ? (
                        <>{a.skipped}</>
                      ) : (
                        <>
                          Read {a.polled ?? 0} message{a.polled === 1 ? '' : 's'} — {a.reply ?? 0}{' '}
                          replies, {a.bounced ?? 0} bounces, {a.auto_reply ?? 0} auto-replies,{' '}
                          {a.objection ?? 0} objections.
                        </>
                      )}
                    </div>
                  ))}
            </div>
          </div>
        )}
        <ErrorBox error={pollError} />

        {tabs.length > 1 && <Tabs tabs={tabs} active={tab} onChange={setTab} />}

        {dispatches.loading && <Loading rows={4} />}
        <ErrorBox error={dispatches.error} onRetry={dispatches.reload} />

        {!dispatches.loading && !dispatches.error && rows.length === 0 && (
          <Empty title="Nothing has been sent yet">
            Applications are dispatched from the Applications screen once you have approved
            them. Every one of them will appear here, with what became of it.
          </Empty>
        )}

        {!dispatches.loading && !dispatches.error && rows.length > 0 && shown.length === 0 && (
          <Empty title={`No message is ${deliveryLabel(tab).toLowerCase()}`}>
            Choose “All” to see the rest of the log.
          </Empty>
        )}

        {shown.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Recipient</th>
                  <th>Sent</th>
                  <th>Status</th>
                  <th>Attachments</th>
                  <th>
                    Message id
                    <HelpTip term="message_id" />
                  </th>
                  <th>Came back</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((d) => (
                  <tr key={d.id} onClick={() => setOpen(d)} style={{ cursor: 'pointer' }}>
                    <td>
                      <div>{d.recipient_name || d.recipient_email}</div>
                      <div className="tiny muted mono">{d.recipient_email}</div>
                      {d.kind === 'follow_up' && (
                        <Badge tone="info">follow-up</Badge>
                      )}
                    </td>
                    <td className="nowrap">
                      {formatWhen(d.sent_at || d.scheduled_for || d.created_at)}
                      {!d.sent_at && d.scheduled_for && (
                        <div className="tiny muted">scheduled</div>
                      )}
                    </td>
                    <td className="nowrap">
                      <Badge tone={DELIVERY_TONE[d.delivery_status]}>
                        {deliveryLabel(d.delivery_status)}
                      </Badge>
                    </td>
                    <td>
                      {(d.attachments || []).length === 0 ? (
                        <span className="muted">none</span>
                      ) : (
                        <span title={(d.attachments || []).join('\n')}>
                          {(d.attachments || []).length} file
                          {(d.attachments || []).length === 1 ? '' : 's'}
                        </span>
                      )}
                    </td>
                    <td className="mono tiny" style={{ maxWidth: 220, overflowWrap: 'anywhere' }}>
                      {d.message_id || '–'}
                    </td>
                    <td className="nowrap">
                      {d.reply_detected_at && <Badge tone="ok">reply</Badge>}{' '}
                      {d.bounce_detected_at && <Badge tone="danger">bounce</Badge>}{' '}
                      {!d.reply_detected_at && !d.bounce_detected_at && (
                        <span className="muted small">silence</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </SectionCard>

      <RepliesCard replies={replies} />

      {open && <DispatchDetail dispatch={open} onClose={() => setOpen(null)} />}
    </div>
  )
}

/* --- What came back ------------------------------------------------------- */

function RepliesCard({ replies }) {
  const rows = replies.data || []
  return (
    <SectionCard icon="responses" title="Replies detected" phase="phase-0">
      <p className="small muted" style={{ marginTop: 0 }}>
        Answers found in a connected mailbox, classified on arrival. Anything that reached you
        another way — a call, a LinkedIn message, an ATS portal, or any reply to mail sent
        through Resend — is recorded on the Responses screen instead.
      </p>

      {replies.loading && <Loading rows={2} />}
      <ErrorBox error={replies.error} onRetry={replies.reload} />

      {!replies.loading && !replies.error && rows.length === 0 && (
        <Empty title="No reply has been detected">
          This stays empty until a Gmail mailbox is connected and something has been answered.
          It is not a count of your responses — only of the ones the system found by itself.
        </Empty>
      )}

      {rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>From</th>
                <th>Subject</th>
                <th>Received</th>
                <th>
                  Read as
                  <HelpTip term="reply_classification" />
                </th>
                <th>Handled</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td className="mono">{r.from_address || '–'}</td>
                  <td>{r.subject || <span className="muted">no subject</span>}</td>
                  <td className="nowrap">{formatWhen(r.received_at || r.created_at)}</td>
                  <td className="nowrap">
                    <Badge tone={r.classification === 'bounce' ? 'danger' : 'info'}>
                      {r.classification || 'unclassified'}
                    </Badge>
                    {r.classification_confidence != null && (
                      <span className="tiny muted"> {Math.round(r.classification_confidence * 100)}%</span>
                    )}
                  </td>
                  <td>{r.handled ? <Badge tone="ok">yes</Badge> : <Badge>no</Badge>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </SectionCard>
  )
}

/* --- One message ---------------------------------------------------------- */

function DispatchDetail({ dispatch: d, onClose }) {
  return (
    <Modal title={d.subject || 'Dispatch'} onClose={onClose} wide>
      <dl className="mail-kv">
        <dt>Recipient</dt>
        <dd className="mono">
          {d.recipient_name ? `${d.recipient_name} <${d.recipient_email}>` : d.recipient_email}
        </dd>

        <dt>Status</dt>
        <dd>
          <Badge tone={DELIVERY_TONE[d.delivery_status]}>{deliveryLabel(d.delivery_status)}</Badge>{' '}
          {d.attempts > 1 && <span className="small muted">{d.attempts} attempts</span>}
        </dd>

        <dt>Sent</dt>
        <dd>{formatWhen(d.sent_at)}</dd>

        {d.scheduled_for && (
          <>
            <dt>
              Held until
              <HelpTip term="queued_dispatch" />
            </dt>
            <dd>
              {formatWhen(d.scheduled_for)}{' '}
              {d.recipient_timezone && (
                <span className="small muted">({d.recipient_timezone})</span>
              )}
            </dd>
          </>
        )}

        <dt>Backend</dt>
        <dd>{d.backend}</dd>

        <dt>Message id</dt>
        <dd className="mono tiny">{d.message_id || '–'}</dd>

        <dt>Attachments</dt>
        <dd>
          {(d.attachments || []).length === 0 ? (
            <span className="muted">none</span>
          ) : (
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {(d.attachments || []).map((a) => (
                <li key={a} className="mono tiny">
                  {a}
                </li>
              ))}
            </ul>
          )}
        </dd>

        <dt>Reply detected</dt>
        <dd>{d.reply_detected_at ? formatWhen(d.reply_detected_at) : <span className="muted">none</span>}</dd>

        <dt>
          Bounce detected
          <HelpTip term="bounce" />
        </dt>
        <dd>
          {d.bounce_detected_at ? (
            <Badge tone="danger">{formatWhen(d.bounce_detected_at)}</Badge>
          ) : (
            <span className="muted">none</span>
          )}
        </dd>

        {d.follow_up_due_at && (
          <>
            <dt>
              Follow-up
              <HelpTip term="follow_up" />
            </dt>
            <dd>
              {d.follow_up_sent_at
                ? `sent ${formatWhen(d.follow_up_sent_at)}`
                : `due ${formatWhen(d.follow_up_due_at)}`}
            </dd>
          </>
        )}

        {d.last_error && (
          <>
            <dt>Last error</dt>
            <dd className="small">{d.last_error}</dd>
          </>
        )}
      </dl>

      {d.delivery_detail && (
        <div style={{ marginTop: 14 }}>
          <h4>What the provider said</h4>
          <pre className="mono tiny" style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
            {JSON.stringify(d.delivery_detail, null, 2)}
          </pre>
        </div>
      )}
    </Modal>
  )
}
