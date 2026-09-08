/**
 * Watched companies (FR-401).
 *
 * A watch is a standing instruction, not a one-off search: every `n` days the
 * five channels are re-read, anything new raises a notification, and a matching
 * vacancy is put into the ranked list of the campaign the watch names. The
 * table therefore has to answer three questions at a glance - when was this
 * last looked at, what changed, and did anything break - because a watch that
 * is quietly failing looks exactly like a company that is quietly not hiring.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import { Badge, Empty, ErrorBox, Loading, Modal, formatDate } from '../../components/ui'
import { CHANNEL_LABEL, TimingBadge, relativeDays } from './shared'
import { ReportModal, SettingsModal } from './WatchModals'

/** What the last pass produced, from `watchlist_entry.last_result`. */
function LastResult({ entry }) {
  const result = entry.last_result
  if (!result) return <span className="muted small">not checked yet</span>
  const failed = (result.channels || []).filter((c) => c.error)
  return (
    <div className="col" style={{ gap: 3 }}>
      <span className="small">
        {result.new_vacancies || 0} vacancies · {result.new_signals || 0} signals ·{' '}
        {result.new_filings || 0} filings
      </span>
      {result.opportunities_added > 0 && (
        <span>
          <Badge tone="accent">{result.opportunities_added} added to the ranked list</Badge>
        </span>
      )}
      {result.timing_flag && (
        <span>
          <TimingBadge flag={result.timing_flag} />
        </span>
      )}
      {failed.length > 0 && (
        <span className="tiny muted" title={failed.map((c) => c.error).join('\n')}>
          {failed.length} channel{failed.length === 1 ? '' : 's'} could not be read
        </span>
      )}
    </div>
  )
}

export default function Watchlist({
  entries,
  channels,
  campaigns,
  loading,
  error,
  reload,
  onAdd,
}) {
  const [busyId, setBusyId] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [editing, setEditing] = useState(null)
  const [removing, setRemoving] = useState(null)
  const [report, setReport] = useState(null)
  const [cycle, setCycle] = useState(null)
  const [running, setRunning] = useState(false)

  async function act(entry, fn) {
    setBusyId(entry.id)
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

  async function checkNow(entry) {
    setBusyId(entry.id)
    setActionError(null)
    try {
      setReport(await api.post(`/monitoring/watchlist/${entry.id}/check`))
      reload()
    } catch (e) {
      setActionError(e)
    } finally {
      setBusyId(null)
    }
  }

  // The manual equivalent of the scheduler's hourly pass: it rechecks every
  // watch of yours that is due, and nothing that is not (FR-401).
  async function runCycle() {
    setRunning(true)
    setActionError(null)
    setCycle(null)
    try {
      setCycle(await api.post('/monitoring/watchlist/run'))
      reload()
    } catch (e) {
      setActionError(e)
    } finally {
      setRunning(false)
    }
  }

  // Removing a watch stops the rechecks that feed the notifications, so it
  // confirms first like every other destructive action.
  async function remove() {
    const entry = removing
    setBusyId(entry.id)
    setActionError(null)
    try {
      await api.del(`/monitoring/watchlist/${entry.id}`)
      setRemoving(null)
      reload()
    } catch (e) {
      setActionError(e)
    } finally {
      setBusyId(null)
    }
  }

  const rows = entries || []

  return (
    <>
      <div className="card">
        <div className="card-header">
          <h3>Watched companies</h3>
          <div className="spacer" />
          <button className="btn btn-sm" onClick={runCycle} disabled={running || !rows.length}>
            {running ? <span className="spinner" /> : 'Run the cycle now'}
          </button>
          <button className="btn btn-sm btn-primary" onClick={onAdd}>
            Watch a company
          </button>
        </div>

        {actionError && <ErrorBox error={actionError} />}

        {/* NFR-502 asks for progress on long work. This endpoint is synchronous
            and returns no job id, so there is nothing to poll, pause or cancel:
            an indeterminate bar and an honest sentence is all it can offer. */}
        {running && (
          <div style={{ marginBottom: 12 }}>
            <div className="progress-track">
              <div className="progress-fill indeterminate" />
            </div>
            <p className="small muted" style={{ marginTop: 6 }}>
              Rechecking every watch that is due. Each company takes a few seconds; the pass
              cannot be interrupted once it has started.
            </p>
          </div>
        )}

        {cycle && !running && (
          <div className="alert alert-ok">
            <div>
              Checked {cycle.checked} of {cycle.due} due · {cycle.new_vacancies ?? 0} new
              vacancies · {cycle.new_signals ?? 0} new signals ·{' '}
              {cycle.opportunities_added ?? 0} added to a ranked list.
              {cycle.errors?.length > 0 && ` ${cycle.errors.length} failed.`}
            </div>
          </div>
        )}

        {loading && <Loading rows={4} />}
        {error && <ErrorBox error={error} onRetry={reload} />}

        {!loading && !error && !rows.length && (
          <Empty
            title="No company is being watched"
            action={
              <button className="btn btn-primary" onClick={onAdd}>
                Watch a company
              </button>
            }
          >
            Watching a company rechecks its careers page, its ATS board, its newsroom and its
            filed accounts on a schedule, and tells you what changed.
          </Empty>
        )}

        {!loading && !error && rows.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>
                    Company
                    <HelpTip term="watched_company" />
                  </th>
                  <th>
                    Every
                    <HelpTip title="Check interval">
                      How often this company is rechecked, between 1 and 90 days. Weekly is the
                      default. A shorter interval does not find more; it finds the same thing
                      sooner, at the cost of more requests to a site that did not ask for them.
                    </HelpTip>
                  </th>
                  <th>
                    Last checked
                    <HelpTip term="staleness" />
                  </th>
                  <th>
                    What changed
                    <HelpTip title="Measured since the last pass">
                      “New” means new since this watch last ran, not new to the world. Each new
                      thing is announced once — a vacancy that stays open for six weeks does not
                      reappear in your notifications every week.
                    </HelpTip>
                  </th>
                  <th>Status</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((entry) => (
                  <tr key={entry.id}>
                    <td>
                      <div>
                        <Link to={`/companies/${entry.company_id}`}>{entry.company_name}</Link>
                      </div>
                      {entry.reason && <div className="tiny muted">{entry.reason}</div>}
                      {entry.check_sources && (
                        <div className="tiny muted">
                          {entry.check_sources
                            .map((c) => CHANNEL_LABEL[c] || c)
                            .join(', ')}
                        </div>
                      )}
                    </td>
                    <td className="nowrap">{entry.check_interval_days} d</td>
                    <td className="nowrap">
                      <div>{relativeDays(entry.last_checked_at)}</div>
                      <div className="tiny muted">
                        {entry.next_check_at
                          ? `next ${formatDate(entry.next_check_at)}`
                          : 'due on the next cycle'}
                      </div>
                    </td>
                    <td>
                      <LastResult entry={entry} />
                    </td>
                    <td>
                      {!entry.active && <Badge>paused</Badge>}
                      {entry.active && entry.consecutive_failures > 0 && (
                        <Badge tone="danger">
                          failing ({entry.consecutive_failures})
                        </Badge>
                      )}
                      {entry.active && !entry.consecutive_failures && <Badge tone="ok">active</Badge>}
                      {entry.last_error && (
                        <div className="tiny muted" title={entry.last_error}>
                          {entry.last_error.slice(0, 60)}
                        </div>
                      )}
                    </td>
                    <td>
                      <div className="row" style={{ gap: 5, justifyContent: 'flex-end' }}>
                        <button
                          className="btn btn-sm"
                          disabled={busyId === entry.id}
                          onClick={() => checkNow(entry)}
                        >
                          Check now
                        </button>
                        <button
                          className="btn btn-sm"
                          disabled={busyId === entry.id}
                          onClick={() =>
                            act(entry, () =>
                              api.patch(`/monitoring/watchlist/${entry.id}`, {
                                active: !entry.active,
                              }),
                            )
                          }
                        >
                          {entry.active ? 'Pause' : 'Resume'}
                        </button>
                        <button className="btn btn-sm" onClick={() => setEditing(entry)}>
                          Settings
                        </button>
                        <button
                          className="btn btn-sm btn-danger"
                          onClick={() => setRemoving(entry)}
                        >
                          Remove
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {editing && (
        <SettingsModal
          entry={editing}
          channels={channels}
          campaigns={campaigns}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            reload()
          }}
        />
      )}

      {removing && (
        <Modal
          title={`Stop watching ${removing.company_name}?`}
          onClose={() => setRemoving(null)}
          actions={
            <>
              <button className="btn" onClick={() => setRemoving(null)}>
                Keep watching
              </button>
              <button
                className="btn btn-danger"
                onClick={remove}
                disabled={busyId === removing.id}
              >
                Remove from watchlist
              </button>
            </>
          }
        >
          <p>
            The rechecks stop, and you will no longer be told when this company posts a vacancy
            or does something that usually precedes hiring.
          </p>
          <p className="small muted">
            Nothing already collected is deleted: the company profile, its vacancies and its
            signals belong to the shared knowledge base and stay there. Only your watch goes.
          </p>
        </Modal>
      )}

      {report && <ReportModal report={report} onClose={() => setReport(null)} />}
    </>
  )
}
