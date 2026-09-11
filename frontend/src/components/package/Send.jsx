/**
 * The send guard and the report of what happened (RK-05, FR-325).
 *
 * The product owner asked for the whole pipeline up to a generated email with
 * the CV attached, and explicitly not the send. These two panels are how the
 * screen says so - calmly, in advance, and with the one thing that would have
 * to change for it to be otherwise - and then reports where each attempt
 * stopped.
 *
 * The guard is read from the server, never decided here: `GET
 * /api/apply/send-status` reads it out of the mail layer, which is where the
 * guard actually is. Both routes render the same banner.
 */

import { createContext, useContext } from 'react'

import { HelpTip } from '../Help'
import Icon from '../Icon'
import { Badge } from '../ui'

import { errorText } from '../packageStatus'

/**
 * FR-325: the "send now anyway" override.
 *
 * `SendReport` is rendered by the shared `PackageDetail` pane, which forwards
 * only the report's own data. The host screen supplies the callback through
 * this context, so the override reaches the report without the pane having to
 * know about it.
 */
export const SendNowContext = createContext(null)

export function SendNowProvider({ onSendNow, children }) {
  return <SendNowContext.Provider value={onSendNow}>{children}</SendNowContext.Provider>
}

export function SendGuardBanner({ status, loading, error, onRetry }) {
  if (loading) {
    return (
      <div className="alert alert-info">
        <div>
          <strong>Checking what would happen if you pressed Send…</strong>
        </div>
      </div>
    )
  }

  if (error || !status) {
    return (
      <div className="alert alert-warn">
        <div style={{ flex: 1 }}>
          <strong>The send guard could not be read.</strong>
          <div style={{ marginTop: 4 }}>
            Until it answers, treat the two send controls as live. {error?.message}
          </div>
          {onRetry && (
            <button className="btn btn-sm" style={{ marginTop: 10 }} onClick={onRetry}>
              Check again
            </button>
          )}
        </div>
      </div>
    )
  }

  const dryRun = status.dry_run
  const setting = status.setting || {}
  const cap = status.daily_cap || {}
  const rate = status.rate || {}
  const win = status.send_window || {}
  const backend = status.mail_backend || {}

  return (
    <div className={`alert ${dryRun ? 'alert-ok' : 'alert-warn'}`} style={{ marginBottom: 14 }}>
      <div style={{ flex: 1 }}>
        <strong>
          <Icon name={dryRun ? 'lock' : 'send'} />{' '}
          {dryRun
            ? 'Nothing will be sent. This machine is in dry run.'
            : 'Sending is armed. A message that clears the guard rails will reach its recipient.'}
        </strong>

        <div style={{ marginTop: 6 }}>
          {dryRun ? (
            <>
              Both send controls below run the whole path for real — the approval rule, the
              consistency gate, the guard rails in the recipient's time zone, and the message
              itself with your tailored CV attached. The message is then written to disk instead
              of being handed to a mailbox. Press them: they will tell you exactly where each
              application stands.
              <HelpTip term="dry_run" />
            </>
          ) : (
            <>
              The dry-run guard is off on the server. Approved packages that pass the guard rails
              will be delivered to real people.
            </>
          )}
        </div>

        {/* What would have to change. Stated even when nothing is blocking, so
            the answer is always the same shape and always findable. */}
        <div style={{ marginTop: 10 }}>
          <span className="small muted">
            {dryRun ? 'For anything to be sent:' : 'What is standing in the way:'}
          </span>
          {status.blocked_because?.length > 0 ? (
            <ul className="small" style={{ margin: '4px 0 0', paddingLeft: 18 }}>
              {status.blocked_because.map((line, i) => (
                <li key={i}>{line}</li>
              ))}
            </ul>
          ) : (
            <div className="small" style={{ marginTop: 4 }}>
              Nothing — sending is armed and a mailbox is connected.
            </div>
          )}
        </div>

        <div className="row row-wrap small muted" style={{ marginTop: 10, gap: 14 }}>
          <span className="mono">
            {setting.name}={String(setting.value)}
          </span>
          <span>
            enforced in {setting.enforced_in || 'the mail layer'}
            <HelpTip term="send_guard" />
          </span>
          {status.guarded_backends?.length > 0 && (
            <span>guarded backends: {status.guarded_backends.join(', ')}</span>
          )}
          <span>
            mailbox: {backend.backend || 'none'}{' '}
            <Badge tone={backend.configured ? 'ok' : undefined}>
              {backend.configured ? 'configured' : 'not configured'}
            </Badge>
          </span>
        </div>

        <div className="row row-wrap small muted" style={{ marginTop: 6, gap: 14 }}>
          <span>
            Daily cap {cap.used_today ?? 0}/{cap.cap ?? '–'} used
            <HelpTip term="daily_cap" />
          </span>
          <span>
            One message every {rate.min_interval_seconds}s
            <HelpTip term="send_pacing" />
          </span>
          <span>
            Window {win.start}–{win.end}, {win.days}, in {win.timezone}
            <HelpTip term="send_window" />
          </span>
        </div>

        {dryRun && status.dry_run_dir && (
          <div className="small muted" style={{ marginTop: 6 }}>
            <Icon name="document" /> Assembled messages are written to{' '}
            <span className="mono">{status.dry_run_dir}</span> as .eml files. Open one in any mail
            client to see exactly what would have gone out.
          </div>
        )}
      </div>
    </div>
  )
}

const OUTCOME = {
  dry_run: {
    tone: 'alert-ok',
    icon: 'lock',
    title: 'Assembled in full. Nothing was sent.',
  },
  queued: {
    tone: 'alert-warn',
    icon: 'clock',
    title: 'Held until the recipient’s send window opens.',
  },
  sent: {
    tone: 'alert-ok',
    icon: 'send',
    title: 'Sent.',
  },
}

/**
 * What happened when Send was pressed.
 *
 * The send controls are never disabled, which is a deliberate choice: a
 * greyed-out button teaches nothing, while a button that runs the whole path
 * and comes back with "assembled, not sent — here is the recipient, here is the
 * attachment, here is the file on disk" teaches exactly where the pipeline
 * stops. A refusal is therefore a result rather than a failure.
 */
export function SendReport({ result, error, onDismiss, onSendNow }) {
  const inheritedSendNow = useContext(SendNowContext)
  const sendNow = onSendNow || inheritedSendNow

  if (!result && !error) return null

  if (error) {
    return (
      <div className="alert alert-danger">
        <div style={{ flex: 1 }}>
          <strong>
            <Icon name="x" /> Refused, and nothing was sent.
          </strong>
          <div style={{ marginTop: 4 }}>{errorText(error)}</div>
          <div className="small muted" style={{ marginTop: 6 }}>
            The path ran as far as this rule and stopped there. Fix what it names and press Send
            again — the check is made on the server every time, so it cannot be skipped.
          </div>
          {onDismiss && (
            <button className="btn btn-sm" style={{ marginTop: 10 }} onClick={onDismiss}>
              Close
            </button>
          )}
        </div>
      </div>
    )
  }

  const shape = OUTCOME[result.status] || {
    tone: 'alert-info',
    icon: 'info',
    title: `The send path finished as “${result.status}”.`,
  }
  const rails = result.guard_rails
  // The window the message was held for, when the server names it. Falls back to
  // the plain phrase so the button reads well either way.
  const sendWindow = result.window || result.send_window || 'send window'

  return (
    <div className={`alert ${shape.tone}`}>
      <div style={{ flex: 1 }}>
        <strong>
          <Icon name={shape.icon} /> {shape.title}
        </strong>

        {result.message && <div style={{ marginTop: 4 }}>{result.message}</div>}

        <div className="apl-meta" style={{ marginTop: 10 }}>
          <span className="muted">Recipient</span>
          <span>
            {result.recipient_name && <strong>{result.recipient_name}</strong>}
            <span className="mono small">{result.recipient || '—'}</span>
          </span>

          {result.subject && (
            <>
              <span className="muted">Subject</span>
              <span>{result.subject}</span>
            </>
          )}

          <span className="muted">Attached</span>
          <span>
            {result.attachments?.length > 0 ? (
              result.attachments.map((name) => (
                <Badge tone="info" key={name}>
                  <Icon name="document" /> {name}
                </Badge>
              ))
            ) : (
              <span className="muted">nothing — this package has no CV attached</span>
            )}
            <span className="tiny muted">
              The briefing and the motivation document are not in this list, and cannot be.
            </span>
          </span>

          {result.mime_path && (
            <>
              <span className="muted">Written to</span>
              <span>
                <span className="mono small">{result.mime_path}</span>
                <span className="tiny muted">
                  {result.mime_bytes
                    ? `${Number(result.mime_bytes).toLocaleString('en-GB')} bytes`
                    : ''}{' '}
                  — open it in any mail client to read exactly what would have gone out.
                </span>
              </span>
            </>
          )}

          {result.sent_at && (
            <>
              <span className="muted">
                Sent at
                <HelpTip term="message_id" />
              </span>
              <span>
                {result.sent_at}
                {result.message_id && <span className="mono tiny">{result.message_id}</span>}
              </span>
            </>
          )}

          {result.scheduled_for && (
            <>
              <span className="muted">
                Queued for
                <HelpTip term="queued_dispatch" />
              </span>
              <span>{result.scheduled_for}</span>
            </>
          )}
        </div>

        {rails && (
          <div className="small" style={{ marginTop: 10 }}>
            <strong>Would it have gone out now?</strong>{' '}
            {rails.allowed ? (
              <Badge tone="ok">yes, every guard rail was clear</Badge>
            ) : (
              <>
                <Badge tone="warn">no — {rails.code.replace(/_/g, ' ')}</Badge>
                <div className="muted" style={{ marginTop: 4 }}>
                  {rails.reason}
                </div>
              </>
            )}
            {rails.recipient_local_time && (
              <div className="tiny muted" style={{ marginTop: 4 }}>
                It is {rails.recipient_local_time} where the recipient is ({rails.timezone}).
              </div>
            )}
          </div>
        )}

        {/* FR-325: the job seeker can step over the send window and deliver now. */}
        {result.status === 'queued' && sendNow && (
          <div style={{ marginTop: 10 }}>
            <button className="btn btn-sm" onClick={sendNow}>
              <Icon name="send" /> Send now anyway (outside the {sendWindow})
            </button>
            <div className="small muted" style={{ marginTop: 6 }}>
              This overrides the recipient’s send window. The override is recorded in the audit
              trail, with the reason the message was held.
            </div>
          </div>
        )}

        {result.notes?.length > 0 && (
          <ul className="small muted" style={{ margin: '8px 0 0', paddingLeft: 18 }}>
            {result.notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        )}

        {onDismiss && (
          <button className="btn btn-sm" style={{ marginTop: 10 }} onClick={onDismiss}>
            Close
          </button>
        )}
      </div>
    </div>
  )
}
