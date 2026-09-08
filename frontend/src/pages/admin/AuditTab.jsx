/**
 * The audit trail (NFR-702).
 *
 * The requirement is specific: an immutable record of who approved and sent
 * each application, and which versions were used. Two actions carry that —
 * `application_package.approve` and `application.sent` — so the default view
 * is those two, merged, rather than the whole undifferentiated stream. The
 * rest of the trail is one filter away.
 *
 * GET /api/admin/audit takes a single `action`, so the merged view is two
 * requests joined here. The alternative — asking for everything and filtering
 * in the browser — would silently miss older entries once the trail grows past
 * the page limit.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import { Badge, Empty, ErrorBox, Loading, SectionCard, useFetch } from '../../components/ui'
import { parseJson, stamp } from './format'

/** Actions worth offering by name; anything else is reachable via "everything". */
const ACTIONS = [
  { value: '__applications', label: 'Applications — approved and sent' },
  { value: '', label: 'Everything' },
  { value: 'application_package.approve', label: 'Application approved' },
  { value: 'application.sent', label: 'Application sent' },
  { value: 'dispatch.queued', label: 'Dispatch held' },
  { value: 'dispatch.failed', label: 'Dispatch failed' },
  { value: 'admin.source_acknowledged', label: 'Source terms acknowledged' },
  { value: 'admin.source_acknowledgement_revoked', label: 'Source acknowledgement withdrawn' },
  { value: 'admin.source_configured', label: 'Source configured' },
  { value: 'admin.llm_config_changed', label: 'Model configuration changed' },
  { value: 'admin.llm_log_redacted', label: 'Call log redacted' },
  { value: 'admin.llm_call_viewed', label: 'Call opened by an administrator' },
  { value: 'admin.role_changed', label: 'Administrator role changed' },
  { value: 'consent.granted', label: 'Consent granted' },
  { value: 'consent.withdrawn', label: 'Consent withdrawn' },
  { value: 'job_seeker.exported', label: 'Data exported' },
]

const TONE = {
  'application.sent': 'accent',
  application_package: 'ok',
  'dispatch.failed': 'danger',
  'admin.source_acknowledged': 'warn',
  'admin.source_acknowledgement_revoked': 'warn',
  'consent.withdrawn': 'warn',
}

/** The fields NFR-702 names by hand, surfaced before the raw detail blob. */
const VERSIONED = [
  ['approved_by', 'Approved by'],
  ['profile_version_id', 'Profile version'],
  ['company_snapshot_at', 'Company snapshot'],
  ['recipient', 'Sent to'],
  ['subject', 'Subject'],
  ['message_id', 'Message id'],
  ['backend', 'Mail backend'],
  ['override_reason', 'Override reason'],
]

const LIMIT = 100

export default function AuditTab() {
  const [action, setAction] = useState('__applications')
  const [seekerId, setSeekerId] = useState('')
  const [since, setSince] = useState('')
  const [page, setPage] = useState(0)

  const seekers = useFetch(() => api.get('/admin/seekers'), [])

  const events = useFetch(() => {
    const base = (a) => {
      const q = new URLSearchParams({ limit: String(LIMIT), offset: String(page * LIMIT) })
      if (a) q.set('action', a)
      if (seekerId) q.set('job_seeker_id', seekerId)
      if (since) q.set('since', since)
      return `/admin/audit?${q.toString()}`
    }
    if (action !== '__applications') return api.get(base(action))
    // NFR-702's own pairing: the approval and the dispatch it authorised.
    return Promise.all([
      api.get(base('application_package.approve')),
      api.get(base('application.sent')),
    ]).then(([a, b]) =>
      [...a, ...b].sort((x, y) => (x.created_at < y.created_at ? 1 : -1)),
    )
  }, [action, seekerId, since, page])

  const names = Object.fromEntries(
    (seekers.data || []).map((s) => [s.id, s.display_name || s.email]),
  )

  return (
    <div className="stack">
      <SectionCard
        icon="lock"
        title={
          <>
            Audit trail
            <HelpTip term="audit_trail" />
          </>
        }
        phase="phase-0"
        actions={
          <button className="btn btn-sm" onClick={events.reload}>
            Refresh
          </button>
        }
      >
        <p className="small muted" style={{ marginTop: 0 }}>
          Append-only. There is no route in the API that updates or deletes an entry, and
          nothing on this screen writes to it except by acting elsewhere in the product.
        </p>

        <div className="row row-wrap" style={{ marginBottom: 12 }}>
          <select
            value={action}
            onChange={(e) => {
              setAction(e.target.value)
              setPage(0)
            }}
            style={{ maxWidth: 320 }}
          >
            {ACTIONS.map((a) => (
              <option key={a.value || 'all'} value={a.value}>
                {a.label}
              </option>
            ))}
          </select>

          <select
            value={seekerId}
            onChange={(e) => {
              setSeekerId(e.target.value)
              setPage(0)
            }}
            style={{ maxWidth: 260 }}
          >
            <option value="">Every job seeker</option>
            {(seekers.data || []).map((s) => (
              <option key={s.id} value={s.id}>
                {s.display_name} · {s.email}
              </option>
            ))}
          </select>

          <input
            type="date"
            value={since}
            onChange={(e) => {
              setSince(e.target.value)
              setPage(0)
            }}
            style={{ maxWidth: 170 }}
            aria-label="Only entries from this date onwards"
          />
        </div>

        {events.loading ? (
          <Loading rows={5} />
        ) : events.error ? (
          <ErrorBox error={events.error} onRetry={events.reload} />
        ) : !events.data?.length ? (
          <Empty title="Nothing in the trail for this filter">
            {action === '__applications'
              ? 'No application has been approved or sent yet. Entries appear here the moment one is — with who approved it, who it went to, and which profile version was used.'
              : 'Widen the filter, or clear the date, to see whether anything was recorded at all.'}
          </Empty>
        ) : (
          <>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th className="nowrap">When</th>
                    <th>Action</th>
                    <th>Actor</th>
                    <th>Subject of the entry</th>
                    <th>What was recorded</th>
                  </tr>
                </thead>
                <tbody>
                  {events.data.map((e) => (
                    <AuditRow key={e.id} event={e} names={names} />
                  ))}
                </tbody>
              </table>
            </div>

            <div className="row" style={{ marginTop: 10 }}>
              <span className="small muted">
                Showing {events.data.length} entries, newest first.
              </span>
              <div className="spacer" />
              <button className="btn btn-sm" disabled={page === 0} onClick={() => setPage(page - 1)}>
                Newer
              </button>
              <button
                className="btn btn-sm"
                disabled={events.data.length < LIMIT}
                onClick={() => setPage(page + 1)}
              >
                Older
              </button>
            </div>
          </>
        )}
      </SectionCard>
    </div>
  )
}

function AuditRow({ event, names }) {
  const [open, setOpen] = useState(false)
  // audit_event.detail is a TEXT column holding JSON, so it arrives as a string.
  const detail = parseJson(event.detail, null)
  const tone = TONE[event.action] || TONE[event.action?.split('.')[0]]
  const named = VERSIONED.filter(([k]) => detail && detail[k] != null)

  return (
    <>
      <tr>
        <td className="small nowrap">{stamp(event.created_at)}</td>
        <td>
          <Badge tone={tone}>{event.action}</Badge>
        </td>
        <td className="small">
          {names[event.actor] || names[event.job_seeker_id] || (
            <span className="muted">{event.actor ? 'unknown actor' : 'system'}</span>
          )}
        </td>
        <td className="small">
          {event.entity_type ? (
            <>
              {event.entity_type}
              {event.entity_id && <div className="mono tiny muted">{event.entity_id}</div>}
            </>
          ) : (
            <span className="muted">–</span>
          )}
        </td>
        <td className="small">
          {named.length > 0 ? (
            <div className="chips">
              {named.slice(0, 3).map(([k, label]) => (
                <span key={k} className="chip">
                  {label}: {String(detail[k]).slice(0, 42)}
                </span>
              ))}
            </div>
          ) : (
            <span className="muted">–</span>
          )}
          {detail && (
            <button className="btn btn-sm btn-ghost" onClick={() => setOpen(!open)}>
              {open ? 'Hide detail' : 'Detail'}
            </button>
          )}
        </td>
      </tr>
      {open && detail && (
        <tr>
          <td colSpan={5}>
            <div className="doc-preview tiny">{JSON.stringify(detail, null, 2)}</div>
          </td>
        </tr>
      )}
    </>
  )
}
