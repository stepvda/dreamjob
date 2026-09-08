/**
 * Notifications (FR-401, FR-403).
 *
 * One row per genuinely new thing: a vacancy at a watched company, a hiring
 * signal, a follow-up whose date has passed, a reply, the weekly digest. The
 * backend de-duplicates on a `dedup_key`, so a vacancy that stays open for six
 * weeks is announced once — which is what makes this list worth reading rather
 * than worth muting.
 *
 * Marking read is a local decision and never deletes anything; dismissing
 * hides the row for good, which is why the two are separate buttons.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import { Badge, Empty, ErrorBox, Loading, formatDate } from '../../components/ui'
import { KIND_LABEL, NOTIFICATION_KINDS, SEVERITY_TONE, relativeDays } from './shared'

/** Where a notification's payload points, per `watchlist._notify` and the schedulers. */
function targetLink(item) {
  const p = item.payload || {}
  if (p.company_id) return { to: `/companies/${p.company_id}`, label: 'Open the company' }
  if (p.pipeline_card_id) return { to: '/pipeline', label: 'Open the pipeline' }
  if (p.opportunity_id) return { to: `/opportunities/${p.opportunity_id}`, label: 'Open the opportunity' }
  return null
}

function NotificationRow({ item, busy, onRead, onDismiss }) {
  const unread = !item.read_at
  const link = targetLink(item)
  const external = item.payload?.source_url
  // A digest notification carries the whole rendered digest in its body; the
  // digest tab renders it properly, so only the first lines belong here.
  const body =
    item.kind === 'digest' ? String(item.body || '').split('\n').slice(0, 3).join(' ') : item.body

  return (
    <div className={`mon-notif${unread ? ' unread' : ''} ${item.severity || 'info'}`}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="row row-wrap" style={{ gap: 7 }}>
          <strong>{item.title}</strong>
          <Badge>{KIND_LABEL[item.kind] || item.kind}</Badge>
          {item.severity && item.severity !== 'info' && (
            <Badge tone={SEVERITY_TONE[item.severity]}>{item.severity}</Badge>
          )}
          {item.payload?.added_to_ranked_list && (
            <Badge tone="accent">added to the ranked list</Badge>
          )}
        </div>
        {body && <div className="mon-notif-body">{body}</div>}
        <div className="row row-wrap tiny muted" style={{ gap: 8, marginTop: 4 }}>
          <span title={item.created_at}>
            {relativeDays(item.created_at)} · {formatDate(item.created_at)}
          </span>
          {link && <Link to={link.to}>{link.label}</Link>}
          {external && (
            <a href={external} target="_blank" rel="noreferrer">
              Source
            </a>
          )}
        </div>
      </div>
      <div className="col" style={{ gap: 5 }}>
        {unread && (
          <button className="btn btn-sm" disabled={busy} onClick={onRead}>
            Mark read
          </button>
        )}
        <button className="btn btn-sm btn-ghost" disabled={busy} onClick={onDismiss}>
          Dismiss
        </button>
      </div>
    </div>
  )
}

export default function Notifications({ data, loading, error, reload, onAdd }) {
  const [kind, setKind] = useState('')
  const [unreadOnly, setUnreadOnly] = useState(false)
  const [busyId, setBusyId] = useState(null)
  const [actionError, setActionError] = useState(null)

  const all = data?.notifications || []
  const items = all.filter(
    (n) => (!kind || n.kind === kind) && (!unreadOnly || !n.read_at),
  )

  async function act(id, fn) {
    setBusyId(id)
    setActionError(null)
    try {
      await fn()
      reload()
    } catch (e) {
      setActionError(e)
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="card">
      <div className="card-header">
        <h3>
          Notifications
          <HelpTip title="Announced once">
            Each new vacancy, signal or filing raises exactly one notification, however many
            times the recheck sees it afterwards. An empty list means nothing changed, not that
            nothing was looked at — the watchlist shows when each company was last read.
          </HelpTip>
        </h3>
        <div className="spacer" />
        <span className="small muted">{data?.unread ?? 0} unread</span>
        <button
          className="btn btn-sm"
          disabled={!data?.unread || busyId === 'all'}
          onClick={() => act('all', () => api.post('/monitoring/notifications/read-all'))}
        >
          Mark all read
        </button>
      </div>

      {actionError && <ErrorBox error={actionError} />}

      <div className="row row-wrap" style={{ marginBottom: 12 }}>
        <div className="chips">
          <span
            className={`chip clickable${kind === '' ? ' on' : ''}`}
            onClick={() => setKind('')}
          >
            Everything
          </span>
          {NOTIFICATION_KINDS.map((k) => {
            const count = all.filter((n) => n.kind === k.key).length
            return (
              <span
                key={k.key}
                className={`chip clickable${kind === k.key ? ' on' : ''}`}
                onClick={() => setKind(kind === k.key ? '' : k.key)}
              >
                {k.label} {count > 0 ? `(${count})` : ''}
              </span>
            )
          })}
        </div>
        <div className="spacer" />
        <label className="checkline">
          <input
            type="checkbox"
            checked={unreadOnly}
            onChange={(e) => setUnreadOnly(e.target.checked)}
          />
          Unread only
        </label>
      </div>

      {loading && <Loading rows={4} />}
      {error && <ErrorBox error={error} onRetry={reload} />}

      {!loading && !error && !all.length && (
        <Empty
          title="Nothing has been announced yet"
          action={
            <button className="btn btn-primary" onClick={onAdd}>
              Watch a company
            </button>
          }
        >
          Notifications are raised by the watchlist when a company posts a vacancy or does
          something that usually precedes hiring, by the follow-up sweep when a follow-up falls
          due, and by the weekly digest.
        </Empty>
      )}

      {!loading && !error && all.length > 0 && !items.length && (
        <Empty title="Nothing matches this filter">
          Clear the filter to see the other {all.length} notifications.
        </Empty>
      )}

      {items.map((item) => (
        <NotificationRow
          key={item.id}
          item={item}
          busy={busyId === item.id}
          onRead={() =>
            act(item.id, () => api.post(`/monitoring/notifications/${item.id}/read`))
          }
          onDismiss={() =>
            act(item.id, () => api.post(`/monitoring/notifications/${item.id}/dismiss`))
          }
        />
      ))}
    </div>
  )
}
