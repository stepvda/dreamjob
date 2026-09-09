/**
 * The send guard, stated before either send control is pressed (RK-05, FR-325).
 *
 * The product owner asked for the whole pipeline up to a generated email with
 * the CV attached, and explicitly not the send. This banner is where the
 * screen says so — calmly, in advance, and with the one thing that would have
 * to change for it to be otherwise.
 *
 * It reports the server's answer, never its own. `GET /api/apply/send-status`
 * reads the guard out of the mail layer, which is where the guard actually is:
 * the backends themselves refuse to carry a message while the dry run is on,
 * so nothing this screen does — and nothing an HTTP client does behind its
 * back — can put one on the wire. Saying that plainly is the point. A user who
 * only sees a greyed-out button learns that the button is broken; a user who
 * reads this learns where the pipeline stops and why.
 */

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge } from '../../components/ui'

export default function SendGuardBanner({ status, loading, error, onRetry }) {
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
              consistency gate, the guard rails in the recipient’s time zone, and the message
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
