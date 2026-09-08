/**
 * Timing intelligence (FR-402).
 *
 * Each hiring signal opens a window: a funding round is useful about two weeks
 * later and stays useful for nine months, a run of open postings is useful now
 * and stale in six weeks. This panel reads the window the backend computed for
 * every watched company and puts the evidence next to it, because "the moment
 * is favourable" is worthless unless you can see what made it so.
 *
 * The recommendation is about *when*, never about *whether*: it does not touch
 * the score of a single opportunity (NFR-305).
 */

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../../api/client'
import { Caution, HelpTip } from '../../components/Help'
import { Empty, ErrorBox, Loading, Meter, Provenance, formatDate } from '../../components/ui'
import { FAVOURABLE, TIMING, TimingBadge, signalLabel } from './shared'

/** One company's window, with the signals that opened it. */
function TimingCard({ row }) {
  const timing = row.timing || {}
  const drivers = timing.drivers || []

  return (
    <div className="card">
      <div className="card-header">
        <h3>
          <Link to={`/companies/${row.company_id}`}>{row.company_name}</Link>
        </h3>
        <div className="spacer" />
        <TimingBadge flag={timing.timing_flag} />
      </div>

      <div className="row" style={{ marginBottom: 10 }}>
        <span className="small muted nowrap">
          Signal strength
          <HelpTip title="Signal strength">
            The combined weight of every signal whose window is open today, after recent
            evidence is favoured over old and near-identical announcements are discounted. It
            is a measure of the moment, not of the company.
          </HelpTip>
        </span>
        <Meter value={Math.round((timing.score || 0) * 100)} />
      </div>

      <div className="small" style={{ marginBottom: 10 }}>
        <strong>
          Recommended window
          <HelpTip term="timing_window" />
        </strong>{' '}
        {timing.window_start || timing.window_end ? (
          <>
            {timing.window_start ? formatDate(timing.window_start) : 'now'} –{' '}
            {timing.window_end ? formatDate(timing.window_end) : 'open ended'}
          </>
        ) : (
          'no window; nothing on record opens one'
        )}
      </div>

      <p className="small muted" style={{ marginTop: 0 }}>
        {timing.rationale}
      </p>

      {drivers.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div className="small muted" style={{ marginBottom: 6 }}>
            What makes the moment
          </div>
          {drivers.map((d, i) => (
            <div className="mon-driver" key={`${d.signal_type}-${i}`}>
              <div className="small">
                <strong>{signalLabel(d.signal_type)}</strong>
                {d.occurred_at ? ` · ${formatDate(d.occurred_at)}` : ''}
              </div>
              <div className="tiny muted">{d.description || d.reason}</div>
              <div className="tiny muted">
                {d.reason}
                {d.source_url && <Provenance source={d.source_url} />}
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="small muted" style={{ marginTop: 10 }}>
        {(row.signals || []).length} signal{(row.signals || []).length === 1 ? '' : 's'} on record
        for this company.
      </div>
    </div>
  )
}

export default function Timing({ entries, watchLoading, watchError, onAdd }) {
  const ids = (entries || []).filter((e) => e.active).map((e) => e.company_id)
  const key = ids.join(',')
  const [state, setState] = useState({ loading: true, error: null, rows: [] })

  useEffect(() => {
    if (!ids.length) {
      setState({ loading: false, error: null, rows: [] })
      return undefined
    }
    let live = true
    setState({ loading: true, error: null, rows: [] })
    // One request per watched company: /monitoring/timing takes a single
    // company id, so there is no batch form to ask for. A company whose
    // timing cannot be read is skipped rather than failing the panel.
    Promise.all(
      ids.map((id) =>
        api
          .get(`/monitoring/timing/${id}`)
          .then((r) => ({ company_id: id, ...r }))
          .catch(() => null),
      ),
    )
      .then((rows) => live && setState({ loading: false, error: null, rows: rows.filter(Boolean) }))
      .catch((e) => live && setState({ loading: false, error: e, rows: [] }))
    return () => {
      live = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  const names = new Map((entries || []).map((e) => [e.company_id, e.company_name]))
  const rows = state.rows
    .map((r) => ({ ...r, company_name: names.get(r.company_id) || r.company_id }))
    .sort((a, b) => (b.timing?.score || 0) - (a.timing?.score || 0))

  const favourable = rows.filter((r) => FAVOURABLE.includes(r.timing?.timing_flag))
  const rest = rows.filter((r) => !FAVOURABLE.includes(r.timing?.timing_flag))

  if (watchLoading || state.loading) return <Loading rows={5} />
  if (watchError) return <ErrorBox error={watchError} />
  if (state.error) return <ErrorBox error={state.error} />

  if (!ids.length) {
    return (
      <Empty
        title="Nothing to time yet"
        action={
          <button className="btn btn-primary" onClick={onAdd}>
            Watch a company
          </button>
        }
      >
        Timing is computed from the hiring signals of the companies you watch. Add one, and each
        recheck refreshes its window.
      </Empty>
    )
  }

  return (
    <>
      {/* NFR-305, FR-402: advisory only. The flag says the moment is good; it
          never says the role is, and it never moves a score. */}
      <Caution title="A favourable moment is not a good job">
        Timing is derived from public events — a funding round, a reorganisation, a new office —
        and says only that a company is more likely to be hiring than usual. It never changes an
        opportunity’s score, and no application is sent because of it.
      </Caution>

      {favourable.length === 0 && (
        <Empty title="No company is in a favourable window right now">
          Nothing on record opens a window today. The windows below say when the next one might.
        </Empty>
      )}

      {favourable.length > 0 && (
        <>
          <p className="section-intro">
            {favourable.length} watched compan{favourable.length === 1 ? 'y is' : 'ies are'} in a
            window where an approach is more likely to land.
          </p>
          <div className="grid grid-2">
            {favourable.map((r) => (
              <TimingCard key={r.company_id} row={r} />
            ))}
          </div>
        </>
      )}

      {rest.length > 0 && (
        <div className="card" style={{ marginTop: 14 }}>
          <div className="card-header">
            <h3>The rest of the watchlist</h3>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Company</th>
                  <th>Moment</th>
                  <th className="num">Strength</th>
                  <th>Window</th>
                  <th>Why</th>
                </tr>
              </thead>
              <tbody>
                {rest.map((r) => (
                  <tr key={r.company_id}>
                    <td>
                      <Link to={`/companies/${r.company_id}`}>{r.company_name}</Link>
                    </td>
                    <td>
                      <TimingBadge flag={r.timing?.timing_flag} />
                    </td>
                    <td className="num">{Math.round((r.timing?.score || 0) * 100)}</td>
                    <td className="nowrap small">
                      {r.timing?.window_start
                        ? `${formatDate(r.timing.window_start)} – ${
                            r.timing.window_end ? formatDate(r.timing.window_end) : 'open'
                          }`
                        : '–'}
                    </td>
                    <td className="small muted">
                      {r.timing?.rationale ||
                        TIMING[r.timing?.timing_flag]?.label ||
                        'nothing on record'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  )
}
