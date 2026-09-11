/**
 * One application package, reviewed (FR-321..324, FR-329..331, NFR-206).
 *
 * Five tabs, in the order a careful reader would take them: the email that is
 * actually sent, the CV that is actually attached, the two documents that are
 * not, and then the checks that decide whether any of it may go.
 *
 * This file owns the header, the tab bar and every call to the API; the panels
 * are presentation. Two constraints shape it more than anything else:
 *
 * * FR-321 makes the briefing and the motivation document job-seeker material.
 *   The API enforces it; this screen has to *say* it, because a reader who is
 *   not told will assume a generated document is attached.
 * * FR-322 makes a passing consistency check a precondition for dispatch, so
 *   the approve control is disabled with its reason visible rather than
 *   failing on the server after the click.
 *
 * The email draft lives here rather than in the panel so that switching tabs
 * does not discard what the reader was in the middle of writing.
 */

import { useEffect, useState } from 'react'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import { Badge, ErrorBox, Field, KindBadge, Modal, Tabs, formatDate } from '../../components/ui'

import ChecksPanel from './ChecksPanel'
import CvPanel from './CvPanel'
import EmailPanel from './EmailPanel'
import SeekerOnlyPanel from './SeekerOnlyPanel'
import {
  LANGUAGES,
  PACKAGE_STATUS_LABEL,
  PACKAGE_STATUS_TONE,
  canApprove,
  documentName,
  hardBlockers,
  softBlockers,
} from './shared'

export default function PackageDetail({ pkg, templates, photo, onChanged, onApprove }) {
  const [tab, setTab] = useState('email')
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [instructions, setInstructions] = useState('')
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [discarding, setDiscarding] = useState(false)
  const [reason, setReason] = useState('')

  // A save or a regeneration replaces the package; the editor follows it.
  useEffect(() => {
    setSubject(pkg.email_subject || '')
    setBody(pkg.email_body || '')
    setError(null)
  }, [pkg.id, pkg.updated_at, pkg.email_subject, pkg.email_body])

  const report = pkg.consistency_report || {}
  const findingCount = (report.findings?.length || 0) + (report.leaks?.length || 0)
  const editable = pkg.status !== 'sent' && pkg.status !== 'discarded'
  const hard = hardBlockers(pkg)
  const soft = softBlockers(pkg)

  async function run(key, fn) {
    setBusy(key)
    setError(null)
    try {
      await fn()
      onChanged()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(null)
    }
  }

  const save = () =>
    run('save', () =>
      api.patch(`/applications/${pkg.id}`, { email_subject: subject, email_body: body }),
    )

  const recheck = () => run('check', () => api.post(`/applications/${pkg.id}/consistency`))

  /* FR-324: regenerate with an instruction, keeping the same package. */
  const regenerate = (parts, extra) =>
    run('regen', () =>
      api.post(`/applications/${pkg.id}/regenerate`, {
        parts,
        instructions: instructions.trim() || undefined,
        use_llm: true,
        ...extra,
      }),
    )

  const download = (kind, label, extension) =>
    api
      .download(`/applications/${pkg.id}/documents/${kind}`, documentName(pkg, label, extension))
      .catch(setError)

  return (
    <div className="col" style={{ gap: 0 }}>
      <div className="card">
        <div className="card-header">
          <div style={{ minWidth: 0 }}>
            <h3>{pkg.opportunity_title || 'Untitled role'}</h3>
            <div className="small muted">{pkg.company_name || 'Unknown company'}</div>
          </div>
          <div className="spacer" />
          <KindBadge kind={pkg.opportunity_kind} />
          <Badge tone={PACKAGE_STATUS_TONE[pkg.status]}>
            {PACKAGE_STATUS_LABEL[pkg.status] || pkg.status}
          </Badge>
        </div>

        {error && <ErrorBox error={error} />}

        <div className="apl-meta">
          <span className="muted">Recipient</span>
          <span>
            {pkg.contact_email ? (
              <>
                <span>{pkg.contact_name || pkg.contact_email}</span>
                {pkg.contact_role && <span className="small muted">· {pkg.contact_role}</span>}
                <span className="small muted">· {pkg.contact_email}</span>
              </>
            ) : (
              <span className="muted">No contact yet — find one on the Contacts screen</span>
            )}
            {/* NFR-302: an objection is absolute, and belongs beside the name. */}
            {pkg.contact_objected ? <Badge tone="danger">objected to contact</Badge> : null}
          </span>

          <span className="muted">
            Built from
            <HelpTip term="profile_version" />
          </span>
          <span>
            <span>
              profile version {pkg.profile_version_id ? pkg.profile_version_id.slice(0, 8) : '–'}
            </span>
            <span className="small muted">
              · company data as of {formatDate(pkg.company_snapshot_at)} · last changed{' '}
              {formatDate(pkg.updated_at)}
            </span>
          </span>

          <span className="muted">Language</span>
          <span>{LANGUAGES.find((l) => l.value === pkg.language)?.label || pkg.language}</span>
        </div>

        {/* NFR-104: say when the model was not available rather than pretending. */}
        {pkg.generation?.degradation?.length > 0 && (
          <p className="small muted" style={{ margin: '10px 0 0' }}>
            Written without the model: {pkg.generation.degradation.join(' ')}
          </p>
        )}

        <div className="row row-wrap" style={{ marginTop: 14 }}>
          <button
            className="btn btn-primary"
            disabled={!canApprove(pkg)}
            onClick={() => onApprove([pkg.id])}
            title={
              canApprove(pkg)
                ? 'Read what will be sent, then approve'
                : hard.map((b) => b.detail).join(' ') || 'Already decided'
            }
          >
            Approve for dispatch
          </button>
          <button className="btn" disabled={busy === 'check'} onClick={recheck}>
            {busy === 'check' ? <span className="spinner" /> : 'Re-run the checks'}
          </button>
          <div className="spacer" />
          {editable && (
            <button className="btn btn-danger" onClick={() => setDiscarding(true)}>
              Discard
            </button>
          )}
        </div>

        {/* FR-322: the reason approval is unavailable belongs next to the button. */}
        {pkg.status === 'draft' && hard.length > 0 && (
          <p className="small" style={{ margin: '8px 0 0', color: 'var(--danger)' }}>
            Cannot be approved: {hard.map((b) => b.detail).join(' ')}
          </p>
        )}
        {pkg.status === 'draft' && hard.length === 0 && soft.length > 0 && (
          <p className="small" style={{ margin: '8px 0 0', color: 'var(--warn)' }}>
            Approving this one will ask you to record why: {soft.map((b) => b.detail).join(' ')}
          </p>
        )}
      </div>

      <div className="card">
        <Tabs
          active={tab}
          onChange={setTab}
          tabs={[
            { key: 'email', label: 'Email' },
            { key: 'cv', label: 'CV' },
            { key: 'briefing', label: 'Briefing' },
            { key: 'motivation', label: 'Motivation' },
            { key: 'checks', label: 'Checks', count: findingCount || undefined },
          ]}
        />

        {tab === 'email' && (
          <EmailPanel
            pkg={pkg}
            editable={editable}
            busy={busy}
            draft={{ subject, setSubject, body, setBody, instructions, setInstructions }}
            onSave={save}
            onRegenerate={regenerate}
          />
        )}

        {tab === 'cv' && (
          <CvPanel
            pkg={pkg}
            templates={templates}
            photo={photo}
            editable={editable}
            busy={busy}
            onTemplate={(template) =>
              run('tpl', () => api.post(`/applications/${pkg.id}/cv-template`, { template }))
            }
            onRegenerate={regenerate}
            onDownload={download}
          />
        )}

        {tab === 'briefing' && (
          <SeekerOnlyPanel
            packageId={pkg.id}
            kind="briefing"
            title="The briefing"
            purpose="Everything known about the company and the role — the profile, five years of financials, hiring signals, competitors, and the questions worth asking. Written for you to read before an interview (FR-329)."
            result={pkg.generation?.briefing || {}}
            available={pkg.documents?.briefing}
            neverSent={pkg.never_sent}
            onDownload={() => download('briefing', 'briefing', 'pdf')}
            action={
              editable && (
                <button
                  className="btn"
                  disabled={busy === 'brief'}
                  onClick={() =>
                    run('brief', () => api.post(`/applications/${pkg.id}/briefing/refresh`))
                  }
                >
                  {busy === 'brief' ? <span className="spinner" /> : 'Refresh before an interview'}
                </button>
              )
            }
          />
        )}

        {tab === 'motivation' && (
          <SeekerOnlyPanel
            packageId={pkg.id}
            kind="motivation"
            title="The motivation document"
            purpose="Why this role, where you meet its requirements and where you do not, and the talking points that follow. Written for you, so the gaps are stated plainly rather than written around (FR-330)."
            result={pkg.generation?.motivation || {}}
            available={pkg.documents?.motivation}
            neverSent={pkg.never_sent}
            onDownload={() => download('motivation', 'motivation', 'pdf')}
            extra={
              pkg.generation?.motivation?.gaps?.length > 0 && (
                <div className="alert alert-warn">
                  <div>
                    <strong>Requirements you do not meet.</strong> Prepare an answer for each:
                    <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
                      {pkg.generation.motivation.gaps.map((g, i) => (
                        <li key={i}>{g}</li>
                      ))}
                    </ul>
                  </div>
                </div>
              )
            }
            action={
              editable && (
                <button
                  className="btn btn-ghost"
                  disabled={busy === 'regen'}
                  onClick={() => regenerate(['motivation'])}
                >
                  {busy === 'regen' ? <span className="spinner" /> : 'Regenerate'}
                </button>
              )
            }
          />
        )}

        {tab === 'checks' && (
          <ChecksPanel pkg={pkg} busy={busy === 'check'} onRecheck={recheck} />
        )}
      </div>

      {/* Discarding is destructive and outward-facing, so it confirms first. */}
      {discarding && (
        <Modal
          title="Discard this application package?"
          onClose={() => setDiscarding(false)}
          actions={
            <>
              <button className="btn" onClick={() => setDiscarding(false)}>
                Keep it
              </button>
              <button
                className="btn btn-danger"
                disabled={busy === 'discard'}
                onClick={() =>
                  run('discard', async () => {
                    await api.post(`/applications/${pkg.id}/discard`, { reason: reason.trim() })
                    setDiscarding(false)
                    setReason('')
                  })
                }
              >
                Discard
              </button>
            </>
          }
        >
          <p>
            The CV, briefing, motivation document and email for{' '}
            <strong>{pkg.opportunity_title}</strong> at <strong>{pkg.company_name}</strong> stop
            counting as a live application. Nothing is deleted, and you can generate a new package
            for the same opportunity.
          </p>
          <Field label="Why (kept in the audit trail)">
            <input type="text" value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>
        </Modal>
      )}
    </div>
  )
}
