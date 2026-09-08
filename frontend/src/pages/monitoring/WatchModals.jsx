/**
 * The dialogs the watchlist opens (FR-401).
 *
 * They live apart from the table because they are the only place a watch is
 * *written*: interval, channels, the campaign new vacancies land in, and the
 * recheck report. Keeping them together keeps every field that the API accepts
 * in one file, next to the constraint the backend enforces on it.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import {
  Badge,
  ChipSelect,
  ErrorBox,
  Field,
  Loading,
  Modal,
  useFetch,
} from '../../components/ui'
import { CHANNEL_LABEL, TimingBadge, signalLabel } from './shared'

/** watchlist.MIN_INTERVAL_DAYS / MAX_INTERVAL_DAYS. */
const MIN_INTERVAL = 1
const MAX_INTERVAL = 90

function channelOptions(channels) {
  return (channels?.channels || Object.keys(CHANNEL_LABEL)).map((c) => ({
    value: c,
    label: CHANNEL_LABEL[c] || c,
  }))
}

/** The report one recheck returns, shown rather than summarised into a toast. */
export function ReportModal({ report, onClose }) {
  return (
    <Modal title={`Recheck of ${report.company_name || 'the company'}`} onClose={onClose} wide>
      <div className="row row-wrap" style={{ marginBottom: 12 }}>
        <Badge tone="info">{report.new_vacancies.length} new vacancies</Badge>
        <Badge tone="info">{report.new_signals.length} new signals</Badge>
        <Badge tone="info">{report.new_filings.length} new filings</Badge>
        <Badge tone="accent">{report.opportunities_added.length} added to the ranked list</Badge>
        <Badge>{report.notifications} notifications raised</Badge>
        {report.timing?.timing_flag && <TimingBadge flag={report.timing.timing_flag} />}
      </div>

      <h4 className="small muted">Channels</h4>
      <div className="table-wrap" style={{ marginBottom: 14 }}>
        <table>
          <thead>
            <tr>
              <th>Channel</th>
              <th>Read</th>
              <th className="num">Records</th>
              <th>Problem</th>
            </tr>
          </thead>
          <tbody>
            {report.channels.map((c) => (
              <tr key={c.channel}>
                <td>{CHANNEL_LABEL[c.channel] || c.channel}</td>
                <td>{c.checked ? <Badge tone="ok">yes</Badge> : <Badge>skipped</Badge>}</td>
                <td className="num">{c.found}</td>
                <td className="small muted">{c.error || '–'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {report.new_vacancies.length > 0 && (
        <>
          <h4 className="small muted">New vacancies</h4>
          <ul className="help-tips" style={{ marginBottom: 14 }}>
            {report.new_vacancies.map((v) => (
              <li key={v.id}>
                {v.title} {v.location ? `· ${v.location}` : ''}{' '}
                {v.source_url && (
                  <a href={v.source_url} target="_blank" rel="noreferrer">
                    source
                  </a>
                )}
              </li>
            ))}
          </ul>
        </>
      )}

      {report.new_signals.length > 0 && (
        <>
          <h4 className="small muted">New signals</h4>
          <ul className="help-tips">
            {report.new_signals.map((s) => (
              <li key={s.id}>
                <strong>{signalLabel(s.signal_type)}</strong> — {s.description || 'no description'}
              </li>
            ))}
          </ul>
        </>
      )}

      {report.timing?.rationale && (
        <p className="small muted" style={{ marginTop: 14 }}>
          {report.timing.rationale}
        </p>
      )}
    </Modal>
  )
}

/** Interval, channels, campaign and reason — the whole editable surface. */
export function SettingsModal({ entry, channels, campaigns, onClose, onSaved }) {
  const [interval, setInterval] = useState(entry.check_interval_days || 7)
  const [selected, setSelected] = useState(entry.check_sources || channels?.default || [])
  const [campaignId, setCampaignId] = useState(entry.campaign_id || '')
  const [reason, setReason] = useState(entry.reason || '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function save() {
    setBusy(true)
    setError(null)
    try {
      await api.patch(`/monitoring/watchlist/${entry.id}`, {
        interval_days: Number(interval),
        channels: selected.length ? selected : null,
        campaign_id: campaignId || null,
        reason: reason || null,
      })
      onSaved()
    } catch (e) {
      setError(e)
      setBusy(false)
    }
  }

  return (
    <Modal
      title={`Watch settings — ${entry.company_name}`}
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={save} disabled={busy}>
            {busy ? <span className="spinner" /> : 'Save'}
          </button>
        </>
      }
    >
      {error && <ErrorBox error={error} />}
      <Field
        label="Check every (days)"
        hint={`Between ${MIN_INTERVAL} and ${MAX_INTERVAL}. Weekly is the default because most careers pages change no faster.`}
      >
        <input
          type="number"
          min={MIN_INTERVAL}
          max={MAX_INTERVAL}
          value={interval}
          onChange={(e) => setInterval(e.target.value)}
        />
      </Field>

      <Field
        label={
          <>
            Channels
            <HelpTip term="check_channel" />
          </>
        }
        hint="Each is read independently; a company with no newsroom still gets its careers page checked."
      >
        <ChipSelect
          options={channelOptions(channels)}
          value={selected}
          onChange={setSelected}
        />
      </Field>

      {/* FR-401: a vacancy found here is synthesised into an opportunity for
          the named campaign, with that campaign's directives still applied. */}
      <Field
        label="Add new vacancies to"
        hint="Leave empty to be notified only. The campaign's directives still filter what is added."
      >
        <select value={campaignId} onChange={(e) => setCampaignId(e.target.value)}>
          <option value="">Notify me only</option>
          {(campaigns || []).map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
      </Field>

      <Field label="Why you are watching this company" hint="For your own reference only.">
        <input
          type="text"
          maxLength={500}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Dream employer; waiting for a data role to open"
        />
      </Field>
    </Modal>
  )
}

/** Put a company from the shared knowledge base on the watchlist (FR-401). */
export function AddWatchModal({ channels, campaigns, onClose, onAdded }) {
  const [draft, setDraft] = useState('')
  const [query, setQuery] = useState('')
  const [picked, setPicked] = useState(null)
  const [interval, setInterval] = useState(channels?.default_interval_days || 7)
  const [campaignId, setCampaignId] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const results = useFetch(
    () => (query ? api.get(`/companies?q=${encodeURIComponent(query)}&limit=8`) : null),
    [query],
  )

  async function add() {
    setBusy(true)
    setError(null)
    try {
      await api.post('/monitoring/watchlist', {
        company_id: picked.id,
        interval_days: Number(interval),
        campaign_id: campaignId || null,
        reason: reason || null,
      })
      onAdded()
    } catch (e) {
      setError(e)
      setBusy(false)
    }
  }

  return (
    <Modal
      title="Watch a company"
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={add} disabled={!picked || busy}>
            {busy ? <span className="spinner" /> : 'Add to watchlist'}
          </button>
        </>
      }
    >
      {error && <ErrorBox error={error} />}

      {/* Only companies already profiled can be watched: watchlist.add() looks
          the company up in the shared knowledge base and refuses otherwise. */}
      <Field
        label="Company"
        hint="Search the shared knowledge base. Run a campaign, or open Companies, if the one you want is not there yet."
      >
        <form
          className="row"
          onSubmit={(e) => {
            e.preventDefault()
            setQuery(draft.trim())
          }}
        >
          <input
            type="search"
            value={draft}
            placeholder="Company name…"
            onChange={(e) => setDraft(e.target.value)}
            autoFocus
          />
          <button className="btn">Search</button>
        </form>
      </Field>

      {results.loading && query && <Loading rows={2} />}
      {results.error && <ErrorBox error={results.error} onRetry={results.reload} />}
      {query && !results.loading && !results.error && !(results.data?.items || []).length && (
        <p className="small muted">
          Nothing in the knowledge base matches “{query}”.{' '}
          <Link to="/companies">Browse companies</Link> or collect it with a campaign first.
        </p>
      )}
      {(results.data?.items || []).length > 0 && (
        <div className="chips" style={{ marginBottom: 14 }}>
          {results.data.items.map((c) => (
            <span
              key={c.id}
              className={`chip clickable${picked?.id === c.id ? ' on' : ''}`}
              onClick={() => setPicked(c)}
            >
              {c.name}
              {c.country ? ` · ${c.country}` : ''}
            </span>
          ))}
        </div>
      )}

      <Field
        label="Check every (days)"
        hint="A new watch is due at once, so the first pass runs on the next cycle rather than in a week."
      >
        <input
          type="number"
          min={MIN_INTERVAL}
          max={MAX_INTERVAL}
          value={interval}
          onChange={(e) => setInterval(e.target.value)}
        />
      </Field>

      <Field
        label="Add new vacancies to"
        hint="Leave empty to be notified only."
      >
        <select value={campaignId} onChange={(e) => setCampaignId(e.target.value)}>
          <option value="">Notify me only</option>
          {(campaigns || []).map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
      </Field>

      <Field label="Why (optional)">
        <input
          type="text"
          maxLength={500}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
      </Field>
    </Modal>
  )
}
