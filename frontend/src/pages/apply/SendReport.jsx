/**
 * What happened when Send was pressed.
 *
 * The two send controls are never disabled, which is a deliberate choice: a
 * greyed-out button teaches nothing, while a button that runs the whole path
 * and comes back with "assembled, not sent — here is the recipient, here is
 * the attachment, here is the file on disk" teaches exactly where the pipeline
 * stops and what would still be in the way if it did not.
 *
 * So this panel treats a refusal as a result rather than as a failure. The
 * shape it renders covers all four endings — assembled and withheld, queued
 * until the recipient's window opens, refused outright, and actually sent —
 * because they are the same question answered differently and putting them in
 * four different places would hide which one you got.
 */

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge } from '../../components/ui'

import { errorText } from './shared'

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

export default function SendReport({ result, error, onDismiss }) {
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
              <span className="muted">nothing — the CV file was not found</span>
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
