/**
 * FR-324: bulk approval, and the summary that is not optional.
 *
 * Approving twelve applications at once is the single most expensive gesture
 * in the product, so it cannot be made from a list of ticked boxes. The modal
 * fetches `POST /applications/approval-summary` and shows what that call
 * returns - recipient, company, role, kind, subject and attachments, one row
 * per application - and the summary written into the audit trail is drafted
 * from those same rows, so the sentence recorded and the table read are the
 * same statement.
 *
 * Applications the API has already refused are shown here rather than hidden,
 * and are left out of the approval, so the table really is what will be sent.
 */

import { useEffect, useMemo, useState } from 'react'

import { api } from '../../api/client'
import { Caution, HelpTip } from '../../components/Help'
import { Badge, ErrorBox, KindBadge, Loading, Modal } from '../../components/ui'

import { ConsistencyBadge, LeakBadge } from './shared'

export default function BulkApprovalModal({ packageIds, onClose, onApproved }) {
  const [summary, setSummary] = useState(null)
  const [error, setError] = useState(null)
  const [statement, setStatement] = useState('')
  const [overrideReason, setOverrideReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)

  useEffect(() => {
    let live = true
    setError(null)
    api
      .post('/applications/approval-summary', { package_ids: packageIds })
      .then((d) => live && setSummary(d))
      .catch((e) => live && setError(e))
    return () => {
      live = false
    }
  }, [packageIds])

  const rows = summary?.packages || []
  const sendable = useMemo(
    () => rows.filter((r) => !(r.blockers || []).some((b) => !b.overridable)),
    [rows],
  )
  const held = rows.filter((r) => !sendable.includes(r))
  const needsOverride = sendable.some((r) => (r.blockers || []).length > 0)

  // The recorded sentence starts as a description of the table above it.
  useEffect(() => {
    if (!summary || statement) return
    const recipients = [...new Set(sendable.map((r) => r.contact_email).filter(Boolean))]
    setStatement(
      `Approving ${sendable.length} application${sendable.length === 1 ? '' : 's'} for dispatch to ` +
        `${recipients.length} recipient${recipients.length === 1 ? '' : 's'}: ` +
        sendable
          .map(
            (r) =>
              `${r.contact_email || 'no recipient yet'} at ${r.company || 'unknown company'} ` +
              `(${r.opportunity || 'role unknown'}, ${
                r.kind === 'speculative' ? 'speculative opening' : 'advertised vacancy'
              })`,
          )
          .join('; ') +
        '.',
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [summary])

  async function approve() {
    setBusy(true)
    setError(null)
    try {
      const res = await api.post('/applications/approve', {
        package_ids: sendable.map((r) => r.package_id),
        summary: statement.trim(),
        override_reason: overrideReason.trim() || undefined,
      })
      setResult(res)
      onApproved(res)
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  const blocked = sendable.length === 0 || !statement.trim() || (needsOverride && !overrideReason.trim())

  return (
    <Modal
      title={
        result
          ? 'Approval recorded'
          : packageIds.length === 1
            ? 'Approve this application'
            : `Approve ${packageIds.length} applications`
      }
      onClose={onClose}
      wide
      actions={
        result ? (
          <button className="btn btn-primary" onClick={onClose}>
            Close
          </button>
        ) : (
          <>
            <button className="btn" onClick={onClose} disabled={busy}>
              Cancel
            </button>
            <button className="btn btn-primary" onClick={approve} disabled={busy || blocked}>
              {busy ? <span className="spinner" /> : `Approve these ${sendable.length}`}
            </button>
          </>
        )
      }
    >
      {error && <ErrorBox error={error} />}
      {!summary && !error && <Loading rows={4} />}

      {result && (
        <div className="col" style={{ gap: 10 }}>
          <div className="alert alert-ok">
            <div>
              <strong>{result.approved?.length || 0} approved for dispatch.</strong> Nothing has
              left your mailbox yet — approved applications are sent from the Mail screen.
            </div>
          </div>
          {result.refused?.length > 0 && (
            <div className="alert alert-warn">
              <div>
                <strong>{result.refused.length} were refused.</strong>
                <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
                  {result.refused.map((r) => (
                    <li key={r.package_id}>
                      {r.company} — {r.opportunity}:{' '}
                      {(r.blockers || []).map((b) => b.detail).join(' ')}
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}
        </div>
      )}

      {summary && !result && (
        <div className="col" style={{ gap: 14 }}>
          {/* FR-324: the summary of what goes to whom is the point of this modal. */}
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Recipient</th>
                  <th>Company</th>
                  <th>Role</th>
                  <th>Kind</th>
                  <th>Subject</th>
                  <th>Attached</th>
                  <th>Checks</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const isHeld = held.includes(r)
                  return (
                    <tr key={r.package_id} style={isHeld ? { opacity: 0.55 } : undefined}>
                      <td>
                        <div>{r.contact_email || <span className="muted">No recipient</span>}</div>
                        {r.contact_name && <div className="small muted">{r.contact_name}</div>}
                      </td>
                      <td>{r.company || '–'}</td>
                      <td>{r.opportunity || '–'}</td>
                      <td>
                        <KindBadge kind={r.kind} />
                      </td>
                      <td className="small">{r.subject || <span className="muted">–</span>}</td>
                      <td className="small">
                        {(r.attachments || []).join(', ') || (
                          <span className="muted">nothing</span>
                        )}
                      </td>
                      <td>
                        <div className="col" style={{ gap: 4 }}>
                          <ConsistencyBadge status={r.consistency_status} />
                          <LeakBadge status={r.leak_scan_status} />
                          {isHeld && <Badge tone="danger">held back</Badge>}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          {/* FR-321: naming what is not attached is as important as naming what is. */}
          <p className="small muted" style={{ margin: 0 }}>
            Each message carries the tailored CV and nothing else. The briefing and the motivation
            document are yours and are never attached.
            <HelpTip term="seeker_only_document" />
          </p>

          {held.length > 0 && (
            <Caution title={`${held.length} will not be approved`}>
              {held.map((r) => (
                <div key={r.package_id}>
                  <strong>{r.company}</strong> — {r.opportunity}:{' '}
                  {(r.blockers || [])
                    .filter((b) => !b.overridable)
                    .map((b) => b.detail)
                    .join(' ')}
                </div>
              ))}
              They stay in the list as drafts. Fix them and approve them separately.
            </Caution>
          )}

          <div className="field">
            <label>
              What you are approving
              <HelpTip term="bulk_approval_summary" />
            </label>
            <textarea
              value={statement}
              onChange={(e) => setStatement(e.target.value)}
              rows={5}
              placeholder="Describe what is about to be sent, and to whom."
            />
            <span className="hint">
              Required. This sentence is written to the audit trail with your name and the time
              (NFR-702), so make it say what you actually decided.
            </span>
          </div>

          {needsOverride && (
            <div className="field">
              <label>
                Why you are overriding a failed check
                <HelpTip term="approval_override" />
              </label>
              <textarea
                value={overrideReason}
                onChange={(e) => setOverrideReason(e.target.value)}
                rows={3}
                placeholder="The claim the checker could not trace is defensible because…"
              />
              <span className="hint">
                Required: at least one of these applications has a consistency finding. FR-322 lets
                you proceed, on the record, not silently.
              </span>
            </div>
          )}

          <p className="small muted" style={{ margin: 0 }}>
            Approving authorises dispatch. The messages themselves go out from your own mailbox on
            the Mail screen, subject to the daily sending caps.
          </p>
        </div>
      )}
    </Modal>
  )
}
