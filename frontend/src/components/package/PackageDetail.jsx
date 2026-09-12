/**
 * One application package, reviewed and dispatched (FR-321..324, FR-329..331,
 * NFR-206).
 *
 * This is the single detail pane both routes render. It is driven by the flat
 * package preview the API returns and by callbacks the host screen supplies,
 * so the Applications screen and the Apply Browser show the same five tabs,
 * the same approval gate and the same send controls while each route keeps its
 * own list, its own endpoints and its own data fetching.
 *
 * Five tabs, in the order a careful reader would take them: the email that is
 * actually sent, the CV that is actually attached, the two documents that are
 * not, and then the checks that decide whether any of it may go.
 *
 * Two constraints shape it more than anything else:
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

import { HelpTip } from '../Help'
import Icon from '../Icon'
import { Badge, ErrorBox, Field, KindBadge, Loading, Modal, Tabs, formatDate } from '../ui'

import { ChecksPanel, CvPanel, EmailPanel, SeekerOnlyPanel } from './Panels'
import { SendReport } from './Send'
import {
  LANGUAGES,
  PACKAGE_STATUS_LABEL,
  PACKAGE_STATUS_TONE,
  canApprove,
  hardBlockers,
  softBlockers,
} from '../packageStatus'

export default function PackageDetail({
  pkg,
  row,
  loading,
  error,
  templates,
  photo,
  busy,
  advisories,
  sendResult,
  sendError,
  onDismissSend,
  onGenerate,
  onSave,
  onRegenerate,
  onTemplate,
  onRecheck,
  onApprove,
  onDiscard,
  onDelete,
  onSend,
  onRefreshBriefing,
  onDownload,
  previewUrl,
  onReload,
}) {
  const [tab, setTab] = useState('email')
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [instructions, setInstructions] = useState('')
  const [discarding, setDiscarding] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [reason, setReason] = useState('')

  // A save or a regeneration replaces the package; the editor follows it.
  useEffect(() => {
    setSubject(pkg?.email_subject || '')
    setBody(pkg?.email_body || '')
  }, [pkg?.id, pkg?.updated_at, pkg?.email_subject, pkg?.email_body])

  // Nothing generated yet (or generated and since gone): the honest screen is
  // an invitation to generate rather than five empty tabs.
  if (!pkg) {
    const title = row?.title || row?.opportunity_title || 'Untitled role'
    const company = row?.company_name || row?.company?.name || 'Unknown company'
    if (loading) {
      return (
        <div className="card">
          <Header row={row} />
          <Loading rows={6} />
        </div>
      )
    }
    if (error) {
      return (
        <div className="card">
          <Header row={row} />
          <ErrorBox error={error} onRetry={onReload} />
        </div>
      )
    }
    const fresh = !row?.package_id
    return (
      <div className="card phase-4 phase-edge">
        <Header row={row} />
        <div className="empty" style={{ padding: '32px 20px' }}>
          <h3>{fresh ? 'Nothing has been generated for this one yet' : 'This job no longer has a live package'}</h3>
          <p>
            {fresh
              ? `Generating produces four documents for ${company}: a CV tailored to ${title}, a briefing on the company, a motivation and fit document, and the email that carries the CV.`
              : 'It was discarded, or generated again elsewhere. Generate it afresh to work on it.'}
          </p>
          <button className="btn btn-phase" disabled={busy === 'gen'} onClick={onGenerate}>
            {busy === 'gen' ? <span className="spinner" /> : 'Generate the four documents'}
          </button>
          {fresh && !row?.contact_email && (
            <p className="small muted" style={{ marginTop: 12 }}>
              There is no contact for {company} yet. You can still generate — the email is written
              to whoever is found later — but it cannot be sent until there is a validated address.
            </p>
          )}
        </div>
      </div>
    )
  }

  const report = pkg.consistency_report || {}
  const findingCount = (report.findings?.length || 0) + (report.leaks?.length || 0)
  const editable = pkg.status !== 'sent' && pkg.status !== 'discarded'
  const hard = hardBlockers(pkg)
  const soft = softBlockers(pkg)
  const sent = pkg.status === 'sent'

  return (
    <div className="col" style={{ gap: 0 }}>
      <div className="card">
        <Header pkg={pkg} row={row} />

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
            onClick={onApprove}
            title={
              canApprove(pkg)
                ? 'Read what will be sent, then approve'
                : hard.map((b) => b.detail).join(' ') || 'Already decided'
            }
          >
            Approve for dispatch
          </button>
          <button className="btn" disabled={busy === 'check'} onClick={onRecheck}>
            {busy === 'check' ? <span className="spinner" /> : 'Re-run the checks'}
          </button>
          <button className="btn btn-phase" disabled={busy === 'send'} onClick={onSend}>
            {busy === 'send' ? (
              <span className="spinner" />
            ) : (
              <>
                <Icon name="send" /> Send email with CV attached
              </>
            )}
          </button>
          <HelpTip term="dry_run" />
          <div className="spacer" />
          {editable && onDiscard && (
            <button className="btn btn-danger" onClick={() => setDiscarding(true)}>
              Discard
            </button>
          )}
          {(pkg.status === 'draft' || pkg.status === 'discarded') && onDelete && (
            <button
              className="btn btn-danger"
              disabled={busy === 'delete'}
              title="Delete this package and its generated files"
              onClick={() => setDeleting(true)}
            >
              {busy === 'delete' ? (
                <span className="spinner" />
              ) : (
                <>
                  <Icon name="trash" /> Delete
                </>
              )}
            </button>
          )}
          {/* A sent package is part of the dispatch record, so the API keeps it
              and the existing Discard route is how it is retired. */}
          {sent && (
            <span
              title="A sent package is kept as a dispatch record — discard it instead"
              style={{ display: 'inline-flex' }}
            >
              <button className="btn btn-danger" disabled>
                <Icon name="trash" /> Delete
              </button>
            </span>
          )}
        </div>

        <p className="tiny muted" style={{ margin: '8px 0 0' }}>
          Pressing Send runs the whole path and tells you where it stopped. It is never disabled,
          because a button that reports is more use than one that is greyed out.
        </p>

        {(sendResult || sendError) && (
          <div style={{ marginTop: 12 }}>
            <SendReport result={sendResult} error={sendError} onDismiss={onDismissSend} />
          </div>
        )}

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
            advisories={advisories}
            draft={{ subject, setSubject, body, setBody, instructions, setInstructions }}
            onSave={onSave}
            onRegenerate={onRegenerate}
          />
        )}

        {tab === 'cv' && (
          <CvPanel
            pkg={pkg}
            templates={templates}
            photo={photo}
            editable={editable}
            busy={busy}
            previewUrl={previewUrl}
            onTemplate={onTemplate}
            onRegenerate={onRegenerate}
            onDownload={onDownload}
          />
        )}

        {tab === 'briefing' && (
          <SeekerOnlyPanel
            pkg={pkg}
            kind="briefing"
            title="The briefing"
            purpose="Everything known about the company and the role — the profile, five years of financials, hiring signals, competitors, and the questions worth asking. Written for you to read before an interview (FR-329)."
            previewUrl={previewUrl}
            onDownload={onDownload}
            action={
              editable &&
              onRefreshBriefing && (
                <button className="btn" disabled={busy === 'brief'} onClick={onRefreshBriefing}>
                  {busy === 'brief' ? <span className="spinner" /> : 'Refresh before an interview'}
                </button>
              )
            }
          />
        )}

        {tab === 'motivation' && (
          <SeekerOnlyPanel
            pkg={pkg}
            kind="motivation"
            title="The motivation document"
            purpose="Why this role, where you meet its requirements and where you do not, and the talking points that follow. Written for you, so the gaps are stated plainly rather than written around (FR-330)."
            previewUrl={previewUrl}
            onDownload={onDownload}
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
                  onClick={() => onRegenerate({ parts: ['motivation'] })}
                >
                  {busy === 'regen' ? <span className="spinner" /> : 'Regenerate'}
                </button>
              )
            }
          />
        )}

        {tab === 'checks' && (
          <ChecksPanel pkg={pkg} busy={busy === 'check'} onRecheck={onRecheck} />
        )}
      </div>

      {/* Discarding is destructive and outward-facing, so it confirms first. */}
      {discarding && onDiscard && (
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
                onClick={async () => {
                  await onDiscard(reason.trim())
                  setDiscarding(false)
                  setReason('')
                }}
              >
                {busy === 'discard' ? <span className="spinner" /> : 'Discard'}
              </button>
            </>
          }
        >
          <p>
            The CV, briefing, motivation document and email for{' '}
            <strong>{pkg.opportunity_title || 'this role'}</strong> at{' '}
            <strong>{pkg.company_name || 'this company'}</strong> stop counting as a live
            application. Nothing is deleted, and you can generate a new package for the same
            opportunity.
          </p>
          <Field label="Why (kept in the audit trail)">
            <input type="text" value={reason} onChange={(e) => setReason(e.target.value)} />
          </Field>
        </Modal>
      )}

      {/* FR-321/FR-331: a draft or discarded package is working material, so it
          is deleted with its generated files; a sent one is kept instead. */}
      {deleting && onDelete && (
        <Modal
          title="Delete this application package?"
          onClose={() => {
            if (busy === 'delete') return
            setDeleting(false)
          }}
          actions={
            <>
              <button
                className="btn"
                disabled={busy === 'delete'}
                onClick={() => setDeleting(false)}
              >
                Keep it
              </button>
              <button
                className="btn btn-danger"
                disabled={busy === 'delete'}
                onClick={async () => {
                  await onDelete()
                  setDeleting(false)
                }}
              >
                {busy === 'delete' ? <span className="spinner" /> : 'Delete'}
              </button>
            </>
          }
        >
          <p>
            The CV, briefing, motivation document and email for{' '}
            <strong>{pkg.opportunity_title || 'this role'}</strong> at{' '}
            <strong>{pkg.company_name || 'this company'}</strong> are deleted from disk with
            the package. This cannot be undone. A package that was sent is kept as a dispatch
            record and is discarded instead.
          </p>
          <p className="small muted" style={{ marginTop: 8 }}>
            You can generate a new package for the same opportunity afterwards.
          </p>
        </Modal>
      )}
    </div>
  )
}

/**
 * The header reads whichever of the flat package preview or a list row it has,
 * so the pane paints the moment a row is clicked rather than flashing empty.
 */
function Header({ pkg, row }) {
  const title = pkg?.opportunity_title || row?.title || row?.opportunity_title || 'Untitled role'
  const company = pkg?.company_name || row?.company_name || row?.company?.name || 'Unknown company'
  const kind = pkg?.opportunity_kind ?? row?.kind
  const status = pkg?.status ?? row?.package_status

  return (
    <div className="card-header">
      <div style={{ minWidth: 0 }}>
        <h3>{title}</h3>
        <div className="small muted">{company}</div>
      </div>
      <div className="spacer" />
      <KindBadge kind={kind} />
      {status && (
        <Badge tone={PACKAGE_STATUS_TONE[status]}>{PACKAGE_STATUS_LABEL[status] || status}</Badge>
      )}
    </div>
  )
}
