/**
 * The right half: one job, one tab per artefact.
 *
 * The order of the tabs is the order the four artefacts matter in on this
 * screen, not the order they are generated in. The email is first because it
 * is the thing that leaves and the only thing you write back into; the CV is
 * second because it is what the email points at and the file that is attached;
 * the two seeker-only documents come next, marked as such; the checks come
 * last because they are what you read before you commit.
 *
 * The send control sits in the header rather than at the bottom of a tab, so
 * it is in the same place whichever artefact you were reading, and it is never
 * disabled — see SendReport for why. Approval and dispatch are two separate
 * decisions and two separate buttons, because FR-324 makes approval a human
 * act and the send window (FR-325) often means the right moment to send is
 * hours after the right moment to approve.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, ErrorBox, KindBadge, Loading, Tabs } from '../../components/ui'

import ChecksTab from './ChecksTab'
import CvTab from './CvTab'
import EmailTab from './EmailTab'
import SeekerDocTab from './SeekerDocTab'
import SendReport from './SendReport'
import applyApi from './api'
import { ConsistencyBadge, StateBadge, documentName, hardBlockers } from './shared'

export default function JobDetail({
  row,
  detail,
  loading,
  error,
  templates,
  photo,
  busy,
  sendResult,
  sendError,
  onDismissSend,
  onGenerate,
  onSave,
  onRegenerate,
  onTemplate,
  onRecheck,
  onApprove,
  onSend,
  onReload,
}) {
  const [tab, setTab] = useState('email')

  if (!row) {
    return (
      <div className="card">
        <div className="empty" style={{ padding: '40px 20px' }}>
          <h3>Pick a job on the left</h3>
          <p>Its email, its CV, its briefing, its motivation document and its checks appear here.</p>
        </div>
      </div>
    )
  }

  const title = row.title || 'Untitled role'
  const company = row.company_name || 'Unknown company'

  // Nothing generated yet: the detail endpoint has nothing to return, and the
  // honest screen is an invitation to generate rather than five empty tabs.
  if (!row.package_id) {
    return (
      <div className="card phase-4 phase-edge">
        <Header row={row} />
        <div className="empty" style={{ padding: '32px 20px' }}>
          <h3>Nothing has been generated for this one yet</h3>
          <p>
            Generating produces four documents for {company}: a CV tailored to {title}, a briefing
            on the company, a motivation and fit document, and the email that carries the CV.
          </p>
          <button className="btn btn-phase" disabled={busy === 'gen'} onClick={onGenerate}>
            {busy === 'gen' ? <span className="spinner" /> : 'Generate the four documents'}
          </button>
          {!row.contact_email && (
            <p className="small muted" style={{ marginTop: 12 }}>
              There is no contact for {company} yet. You can still generate — the email is written
              to whoever is found later — but it cannot be sent until there is a validated address.
            </p>
          )}
        </div>
      </div>
    )
  }

  if (loading && !detail) {
    return (
      <div className="card">
        <Header row={row} />
        <Loading rows={6} />
      </div>
    )
  }

  if (error && !detail) {
    return (
      <div className="card">
        <Header row={row} />
        <ErrorBox error={error} onRetry={onReload} />
      </div>
    )
  }

  // The row says a package exists but the detail call came back empty: the
  // package was discarded, or removed between the two requests. Saying so
  // beats an empty pane that looks like a broken screen.
  if (!detail) {
    return (
      <div className="card phase-4 phase-edge">
        <Header row={row} />
        <div className="empty" style={{ padding: '32px 20px' }}>
          <h3>This job no longer has a live package</h3>
          <p>It was discarded, or generated again elsewhere. Generate it afresh to work on it.</p>
          <button className="btn btn-phase" disabled={busy === 'gen'} onClick={onGenerate}>
            {busy === 'gen' ? <span className="spinner" /> : 'Generate the four documents'}
          </button>
        </div>
      </div>
    )
  }

  const report = detail.consistency?.report || {}
  const highFindings = (report.findings || []).concat(report.leaks || []).filter(
    (f) => f.severity === 'high',
  ).length
  const blocked = hardBlockers(detail.package).length > 0
  const approved = detail.package?.status === 'approved'
  const sent = detail.state === 'sent'
  const previewUrl = (kind) => applyApi.documentUrl(row.opportunity_id, kind)
  const download = (kind, extension) =>
    applyApi.download(row.opportunity_id, kind, documentName(row, kind, extension))

  const tabs = [
    { key: 'email', label: 'Email' },
    { key: 'cv', label: 'CV' },
    { key: 'briefing', label: 'Briefing' },
    { key: 'motivation', label: 'Motivation' },
    { key: 'checks', label: 'Checks', count: highFindings || undefined },
  ]

  return (
    <div className="card phase-4 phase-edge">
      <Header row={row} detail={detail} />

      {/* The two decisions, kept apart and kept in one place. */}
      <div className="row row-wrap" style={{ margin: '10px 0 4px', gap: 8 }}>
        {!sent && (
          <button
            className="btn"
            disabled={approved || blocked || busy === 'approve'}
            onClick={onApprove}
            title={
              blocked
                ? 'The checks below have to pass, or the blocker has to be cleared, first'
                : 'Authorise dispatch. Sends nothing by itself.'
            }
          >
            {busy === 'approve' ? (
              <span className="spinner" />
            ) : approved ? (
              <>
                <Icon name="check" /> Approved
              </>
            ) : (
              'Approve for dispatch'
            )}
          </button>
        )}

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
        <button className="btn btn-sm btn-ghost" onClick={onReload}>
          <Icon name="refresh" /> Refresh
        </button>
      </div>

      <p className="tiny muted" style={{ margin: '0 0 10px' }}>
        Pressing Send runs the whole path and tells you where it stopped. It is never disabled,
        because a button that reports is more use than one that is greyed out.
      </p>

      {(sendResult || sendError) && (
        <div style={{ marginBottom: 12 }}>
          <SendReport result={sendResult} error={sendError} onDismiss={onDismissSend} />
        </div>
      )}

      <Tabs tabs={tabs} active={tab} onChange={setTab} />

      <div style={{ paddingTop: 14 }}>
        {tab === 'email' && (
          <EmailTab detail={detail} busy={busy} onSave={onSave} onRegenerate={onRegenerate} />
        )}
        {tab === 'cv' && (
          <CvTab
            detail={detail}
            templates={templates}
            photo={photo}
            busy={busy}
            previewUrl={previewUrl('cv_pdf')}
            onTemplate={onTemplate}
            onLanguage={(language) =>
              onRegenerate({ parts: ['cv', 'briefing', 'motivation', 'email'], language })
            }
            onRegenerate={onRegenerate}
            onDownload={download}
          />
        )}
        {tab === 'briefing' && (
          <SeekerDocTab
            kind="briefing"
            detail={detail}
            busy={busy}
            previewUrl={previewUrl('briefing')}
            onRegenerate={onRegenerate}
            onDownload={download}
          />
        )}
        {tab === 'motivation' && (
          <SeekerDocTab
            kind="motivation"
            detail={detail}
            busy={busy}
            previewUrl={previewUrl('motivation')}
            onRegenerate={onRegenerate}
            onDownload={download}
          />
        )}
        {tab === 'checks' && <ChecksTab detail={detail} busy={busy} onRecheck={onRecheck} />}
      </div>
    </div>
  )
}

/**
 * The list row is flat and the detail response is nested, so the header reads
 * whichever it has. That is what lets the pane paint the moment a row is
 * clicked, before the detail request comes back, rather than flashing empty.
 */
function Header({ row, detail }) {
  const opportunity = detail?.opportunity || { title: row.title, kind: row.kind }
  const company = detail?.company || { id: row.company_id, name: row.company_name }
  const contact =
    detail?.contact || {
      name: row.contact_name,
      email: row.contact_email,
      email_validation: row.contact_email_validation,
    }

  return (
    <div>
      <div className="row row-wrap" style={{ gap: 8, alignItems: 'baseline' }}>
        <h3 style={{ margin: 0 }}>{opportunity?.title || 'Untitled role'}</h3>
        <KindBadge kind={opportunity?.kind} />
        <StateBadge state={detail?.state || row.state} />
        {detail?.consistency?.status && (
          <ConsistencyBadge status={detail.consistency.status} />
        )}
      </div>
      <div className="row row-wrap small muted" style={{ marginTop: 4, gap: 10 }}>
        {company?.id ? (
          <Link to={`/companies/${company.id}`}>
            <Icon name="companies" /> {company.name}
          </Link>
        ) : (
          <span>{company?.name || 'Unknown company'}</span>
        )}
        {contact?.email && (
          <span>
            <Icon name="contacts" /> {contact.name || contact.email}
            {contact.email_validation && <Badge>{contact.email_validation}</Badge>}
          </span>
        )}
        {row.opportunity_id && (
          <Link to={`/opportunities/${row.opportunity_id}`}>
            <Icon name="external" /> the ranked entry
          </Link>
        )}
      </div>
    </div>
  )
}
