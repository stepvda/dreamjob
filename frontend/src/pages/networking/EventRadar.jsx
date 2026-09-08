/**
 * FR-462: the event and community radar.
 *
 * The radar answers one question per event - is it worth going? - and it
 * answers it in the order the ranking actually weights: who from a target
 * company is there, then which companies it is linked to, then what it is
 * about, then how far away it is. Every one of those is shown, because a
 * single "match" number would hide which of them earned it.
 *
 * Travel is a filter, not a score: an event beyond the travel tolerance in
 * your directives is left out unless you ask for it. Online events have no
 * distance at all and are always reachable.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  Meter,
  Modal,
  Provenance,
  formatDate,
  useFetch,
} from '../../components/ui'

const HORIZONS = [
  { value: 30, label: 'Next 30 days' },
  { value: 90, label: 'Next 3 months' },
  { value: 120, label: 'Next 4 months' },
  { value: 180, label: 'Next 6 months' },
  { value: 365, label: 'Next year' },
]

const FORMAT_LABEL = { online: 'Online', in_person: 'In person', hybrid: 'Hybrid' }

export default function EventRadar({ campaignId }) {
  const [horizon, setHorizon] = useState(120)
  const [includeUnreachable, setIncludeUnreachable] = useState(false)
  const [actionError, setActionError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [confirmCalendar, setConfirmCalendar] = useState(null)
  const [busyId, setBusyId] = useState(null)

  const radar = useFetch(
    () =>
      api.get(
        `/networking/events?horizon_days=${horizon}&limit=50&include_unreachable=${includeUnreachable}${
          campaignId ? `&campaign_id=${encodeURIComponent(campaignId)}` : ''
        }`,
      ),
    [horizon, includeUnreachable, campaignId],
  )

  const data = radar.data
  const events = data?.events || []
  const counts = data?.counts || {}

  /** FR-462: the calendar entry is only written when the seeker asks for it. */
  async function addToCalendar(event) {
    setConfirmCalendar(null)
    setActionError(null)
    setNotice(null)
    setBusyId(event.id)
    try {
      const res = await api.post(`/networking/events/${event.id}/calendar`, {
        campaign_id: campaignId || null,
        note: null,
      })
      setNotice(
        res.calendar_event_url
          ? `“${event.name}” was added to your connected calendar.`
          : `“${event.name}” was written as a calendar file. Use “Download .ics” to import it.`,
      )
      radar.reload()
    } catch (err) {
      setActionError(err)
    } finally {
      setBusyId(null)
    }
  }

  async function setInterest(event, status) {
    setActionError(null)
    setBusyId(event.id)
    try {
      await api.post(`/networking/events/${event.id}/interest`, {
        status,
        campaign_id: campaignId || null,
      })
      radar.reload()
    } catch (err) {
      setActionError(err)
    } finally {
      setBusyId(null)
    }
  }

  async function downloadIcs(event) {
    setActionError(null)
    try {
      await api.download(`/networking/events/${event.id}/calendar.ics`, `${slug(event.name)}.ics`)
    } catch (err) {
      setActionError(err)
    }
  }

  return (
    <>
      <div className="card">
        <div className="row row-wrap">
          <label className="small muted">Horizon</label>
          <select
            value={horizon}
            onChange={(e) => setHorizon(Number(e.target.value))}
            style={{ maxWidth: 200 }}
          >
            {HORIZONS.map((h) => (
              <option key={h.value} value={h.value}>
                {h.label}
              </option>
            ))}
          </select>

          <label className="checkline">
            <input
              type="checkbox"
              checked={includeUnreachable}
              onChange={(e) => setIncludeUnreachable(e.target.checked)}
            />
            <span style={{ display: 'flex', alignItems: 'center' }}>
              Show events beyond my travel tolerance
              <HelpTip term="travel_tolerance" />
            </span>
          </label>

          <div className="spacer" />
          {data?.travel_ceiling_km != null && (
            <span className="small muted">
              Travelling up to {Math.round(data.travel_ceiling_km)} km
            </span>
          )}
          {data?.discretion_mode && <Badge tone="warn">Discretion mode active</Badge>}
        </div>

        {!radar.loading && !radar.error && (
          <div className="row row-wrap small muted" style={{ marginTop: 10 }}>
            <span>{counts.in_window ?? 0} events in the window</span>
            <span>· {counts.matched ?? 0} matched</span>
            <span>· {counts.returned ?? 0} shown</span>
            <span style={{ display: 'flex', alignItems: 'center' }}>
              · {counts.target_companies ?? 0} target companies
              <HelpTip term="target_attendee" />
            </span>
            {counts.excluded_by_discretion > 0 && (
              <span>· {counts.excluded_by_discretion} hidden by discretion mode</span>
            )}
          </div>
        )}

        {data?.note && (
          <p className="small muted" style={{ margin: '8px 0 0', lineHeight: 1.55 }}>
            {data.note}
          </p>
        )}
      </div>

      {actionError && <ErrorBox error={actionError} />}
      {notice && (
        <div className="alert alert-ok">
          <div style={{ flex: 1 }}>{notice}</div>
        </div>
      )}

      {radar.loading && <Loading rows={4} />}
      {radar.error && <ErrorBox error={radar.error} onRetry={radar.reload} />}

      {!radar.loading && !radar.error && events.length === 0 && (
        <Empty title="Nothing on the radar in this window">
          Either no events have been collected yet, or none of them fall inside your travel
          tolerance. Widen the horizon, tick “show events beyond my travel tolerance”, or
          collect programme pages and community calendars for this campaign first.
        </Empty>
      )}

      <div className="stack">
        {events.map((event) => (
          <EventCard
            key={event.id}
            event={event}
            busy={busyId === event.id}
            onCalendar={() => setConfirmCalendar(event)}
            onIcs={() => downloadIcs(event)}
            onInterest={(status) => setInterest(event, status)}
          />
        ))}
      </div>

      {confirmCalendar && (
        <Modal
          title={`Add “${confirmCalendar.name}” to your calendar?`}
          onClose={() => setConfirmCalendar(null)}
          actions={
            <>
              <button className="btn" onClick={() => setConfirmCalendar(null)}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={() => addToCalendar(confirmCalendar)}>
                Add to calendar
              </button>
            </>
          }
        >
          <p className="small" style={{ lineHeight: 1.6 }}>
            {formatWhen(confirmCalendar.starts_at)}
            {confirmCalendar.location ? ` · ${confirmCalendar.location}` : ''}
          </p>
          <p className="small muted" style={{ lineHeight: 1.6 }}>
            If you have connected a calendar, the entry is written there. Otherwise a calendar
            file is produced that you can import yourself. Nothing registers you for the event
            and no organiser is contacted — attending is your decision and your booking.
          </p>
        </Modal>
      )}
    </>
  )
}

/* --- One event ------------------------------------------------------------ */

function EventCard({ event, busy, onCalendar, onIcs, onInterest }) {
  const match = event.match || {}
  const interest = event.interest
  const attendees = match.target_attendees || []
  const companies = match.target_companies || []

  return (
    <div className="card">
      <div className="card-header">
        <Icon name="calendar" />
        <h3>{event.name}</h3>
        {event.kind && <Badge tone="info">{event.kind}</Badge>}
        {event.format && <Badge>{FORMAT_LABEL[event.format] || event.format}</Badge>}
        {interest?.status && <Badge tone="ok">{interest.status}</Badge>}
        <div className="spacer" />
        <div style={{ minWidth: 140 }}>
          <Meter value={(match.score ?? 0) * 100} />
        </div>
        <HelpTip title="Match score" align="right">
          Who is there matters most, then which target companies it is linked to, then how
          closely the topics track your dream job, and least of all how far away it is. A
          webinar with the hiring manager on it beats a perfect-topic conference nobody
          relevant attends.
        </HelpTip>
      </div>

      <div className="row row-wrap small muted" style={{ marginBottom: 8 }}>
        <span>
          <Icon name="clock" /> {formatWhen(event.starts_at)}
        </span>
        {event.location && <span>· {event.location}</span>}
        {event.country && <span>· {event.country}</span>}
        {event.organiser && <span>· organised by {event.organiser}</span>}
        {event.language && <span>· {event.language}</span>}
      </div>

      {match.travel_note && (
        <div className="small" style={{ marginBottom: 8 }}>
          <Badge tone={match.reachable ? 'ok' : 'warn'}>{match.travel_note}</Badge>
        </div>
      )}

      {/* FR-462: target-company attendees, linked to the company profile. */}
      {attendees.length > 0 && (
        <div style={{ marginBottom: 10 }}>
          <div className="small" style={{ display: 'flex', alignItems: 'center', marginBottom: 4 }}>
            <strong>From your target companies</strong>
            <HelpTip term="target_attendee" />
          </div>
          <div className="chips">
            {attendees.map((person, i) => (
              <Link
                key={`${person.name}-${i}`}
                className="chip clickable"
                style={{ textDecoration: 'none' }}
                to={`/companies/${person.company_id}`}
                title={`${person.role || 'role not known'} · ${person.company_name}`}
              >
                {person.name} · {person.company_name}
                {person.kind ? ` (${person.kind})` : ''}
              </Link>
            ))}
          </div>
        </div>
      )}

      {companies.length > 0 && (
        <div className="row row-wrap small" style={{ marginBottom: 10 }}>
          <span className="muted">Linked companies</span>
          {companies.map((c) => (
            <Link key={c.company_id} to={`/companies/${c.company_id}`}>
              {c.name}
            </Link>
          ))}
        </div>
      )}

      {match.reasons?.length > 0 && (
        <ul className="help-tips" style={{ marginBottom: 10 }}>
          {match.reasons.map((reason, i) => (
            <li key={i}>{reason}</li>
          ))}
        </ul>
      )}

      {/* Discretion mode: somebody who would notice you there (FR-385). */}
      {match.discretion_flags?.length > 0 && (
        <div className="alert alert-warn">
          <div style={{ flex: 1 }}>
            <strong>Attending would be visible.</strong>
            <div className="small" style={{ marginTop: 4 }}>
              {match.discretion_flags.join(' · ')}
            </div>
          </div>
        </div>
      )}

      <div className="row row-wrap" style={{ marginTop: 10 }}>
        <button className="btn btn-sm btn-primary" disabled={busy} onClick={onCalendar}>
          {busy ? <span className="spinner" /> : <Icon name="calendar" />} Add to calendar
        </button>
        <button className="btn btn-sm" onClick={onIcs}>
          <Icon name="download" /> Download .ics
        </button>
        <button
          className="btn btn-sm"
          disabled={busy}
          onClick={() => onInterest('interested')}
        >
          Interested
        </button>
        <button
          className="btn btn-sm btn-ghost"
          disabled={busy}
          onClick={() => onInterest('dismissed')}
        >
          Not for me
        </button>
        <div className="spacer" />
        {event.registration_url && (
          <a className="btn btn-sm" href={event.registration_url} target="_blank" rel="noreferrer">
            <Icon name="external" /> Register
          </a>
        )}
        {event.url && (
          <a className="btn btn-sm btn-ghost" href={event.url} target="_blank" rel="noreferrer">
            <Icon name="external" /> Event page
          </a>
        )}
      </div>

      {/* NFR-402: every collected field says where it came from. */}
      <div className="row small" style={{ marginTop: 8 }}>
        <Provenance source={event.url || event.source} confidence={event.confidence} />
        {event.collected_at && (
          <span className="tiny muted">read {formatDate(event.collected_at)}</span>
        )}
      </div>
    </div>
  )
}

/* --- Helpers -------------------------------------------------------------- */

function formatWhen(iso) {
  if (!iso) return 'Date not known'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const date = d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })
  const time = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
  return time === '00:00' ? date : `${date}, ${time}`
}

function slug(name) {
  return (name || 'event')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 60) || 'event'
}
