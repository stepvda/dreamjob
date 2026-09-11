/**
 * "Send all with attachment", behind the summary FR-324 asks for.
 *
 * The requirement is that a bulk action shows what will be sent and to whom
 * before it asks for a decision, so the table below is the whole point of the
 * modal rather than decoration on it: one row per message, named recipient,
 * named subject, named attachment. Twenty rows of that is a thing a person can
 * actually check. A count and a Confirm button is not.
 *
 * Rows that were chosen but would not go are listed separately rather than
 * silently dropped, because "why did only eleven of my fourteen go out?" is
 * the question that follows otherwise.
 *
 * The confirm button is labelled with what will actually happen, which depends
 * on the server's dry-run guard and not on anything this screen decides. While
 * the guard is on it says so, and pressing it assembles every message and
 * sends none.
 */

import Icon from '../../components/Icon'
import { Badge, Modal } from '../../components/ui'

import { errorText, exclusionReason } from './shared'

export default function SendAllModal({
  rows,
  excluded,
  guard,
  busy,
  result,
  error,
  onClose,
  onConfirm,
}) {
  // "Dry run" must be stated, not assumed: when the guard request failed or had
  // not loaded, `guard?.dry_run !== false` read as dry-run and the modal said
  // "send nothing" while the confirm still dispatched for real.  Without the
  // guard the safe default is to say what it would do and refuse to start.
  const guardLoaded = Boolean(guard)
  const dryRun = guard?.dry_run === true
  const cap = guard?.daily_cap || {}
  const overCap = cap.remaining != null && rows.length > cap.remaining

  return (
    <Modal
      title={result ? 'What happened' : 'Exactly what would go out, and to whom'}
      onClose={onClose}
      wide
      actions={
        result ? (
          <button className="btn btn-primary" onClick={onClose}>
            Close
          </button>
        ) : (
          <>
            <button className="btn btn-ghost" onClick={onClose}>
              Cancel
            </button>
            <button
              className={dryRun ? 'btn btn-primary' : 'btn btn-danger'}
              disabled={rows.length === 0 || busy || !guardLoaded}
              onClick={onConfirm}
            >
              {busy ? (
                <span className="spinner" />
              ) : dryRun ? (
                `Assemble all ${rows.length} — send nothing`
              ) : (
                `Send ${rows.length} email${rows.length === 1 ? '' : 's'} with the CV attached`
              )}
            </button>
          </>
        )
      }
    >
      {!result && (
        <>
          <div className={`alert ${dryRun ? 'alert-ok' : 'alert-danger'}`}>
            <div>
              <strong>
                <Icon name={dryRun ? 'lock' : 'send'} />{' '}
                {dryRun
                  ? 'Nothing below will be sent.'
                  : 'These messages will be delivered to real people.'}
              </strong>
              <div style={{ marginTop: 4 }}>
                {dryRun
                  ? 'Each message will be built in full — recipient, subject, body, your tailored CV attached — and written to disk instead of being handed to a mailbox. You will get a line per message saying what happened to it.'
                  : 'Each one is a cold approach to somebody who did not ask to hear from you. Read the table before you confirm.'}
              </div>
            </div>
          </div>

          {overCap && (
            <div className="alert alert-warn" style={{ marginTop: 10 }}>
              <div>
                <strong>More than today’s cap.</strong> {cap.remaining} of {cap.cap} messages are
                left for today; the rest will be held for tomorrow rather than dropped.
              </div>
            </div>
          )}

          <p className="small muted" style={{ margin: '12px 0 6px' }}>
            {rows.length} message{rows.length === 1 ? '' : 's'} · one attachment each · the
            briefing and the motivation document are not among them.
          </p>

          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Company</th>
                  <th>Role</th>
                  <th>Goes to</th>
                  <th>Subject</th>
                  <th>Attachment</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.package_id}>
                    <td>{row.company_name || '—'}</td>
                    <td>{row.title || '—'}</td>
                    <td>
                      <div>{row.contact_name || '—'}</div>
                      <div className="mono tiny muted">{row.contact_email}</div>
                    </td>
                    <td className="small">{row.email_subject || '—'}</td>
                    <td>
                      <Badge tone="info">
                        <Icon name="document" /> tailored CV (PDF)
                      </Badge>
                    </td>
                  </tr>
                ))}
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={5} className="muted small">
                      Nothing chosen is ready to send.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {excluded.length > 0 && (
            <>
              <h4 style={{ marginTop: 16 }}>
                Chosen, but not included ({excluded.length})
              </h4>
              <ul className="small">
                {excluded.map((row) => (
                  <li key={row.opportunity_id}>
                    <strong>{row.company_name}</strong> — {row.title}: {exclusionReason(row)}
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}

      {error && (
        <div className="alert alert-danger">
          <div>
            <strong>The batch did not start.</strong> {errorText(error)}
          </div>
        </div>
      )}

      {result && <BatchResult result={result} />}
    </Modal>
  )
}

function BatchResult({ result }) {
  return (
    <div className="col" style={{ gap: 12 }}>
      <div className={`alert ${result.sent ? 'alert-warn' : 'alert-ok'}`}>
        <div>
          <strong>
            <Icon name={result.dry_run ? 'lock' : 'send'} /> {result.message}
          </strong>
          <div className="row row-wrap small muted" style={{ marginTop: 6, gap: 14 }}>
            <span>{result.requested} requested</span>
            <span>{result.prepared} assembled</span>
            <span>{result.sent} sent</span>
            <span>{result.refused} refused</span>
          </div>
        </div>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Outcome</th>
              <th>Goes to</th>
              <th>Subject</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {(result.results || []).map((row, i) => (
              <tr key={row.package_id || i}>
                <td>
                  <Badge tone={row.status === 'refused' ? 'danger' : row.status === 'sent' ? 'warn' : 'ok'}>
                    {row.status === 'dry_run' ? 'assembled, not sent' : row.status}
                  </Badge>
                </td>
                <td className="mono tiny">{row.recipient || '—'}</td>
                <td className="small">{row.subject || '—'}</td>
                <td className="small muted">
                  {row.reason || row.mime_path || row.message || ''}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
