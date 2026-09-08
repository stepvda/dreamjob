/**
 * The periodic loop behind everything else on this screen (FR-401, FR-403).
 *
 * Six recurring tasks — reply classification, the watchlist pass, the
 * follow-up sweep, the weekly digests, third-party retention (NFR-303) and LLM
 * redaction (FR-364). Whether they run inside the API process is a deployment
 * decision, so the loop can be off here and still be running from cron; that is
 * why "stopped" is stated plainly rather than raised as an error.
 *
 * Starting, stopping and toggling a task are administrator actions. Everyone
 * can read the status, because "why did nothing happen this week?" is a
 * question the answer to lives here.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import {
  Badge,
  ErrorBox,
  Loading,
  Modal,
  formatDate,
  formatDuration,
} from '../../components/ui'
import { relativeDays } from './shared'

/** `last_run` is whatever the setting holds: {at, error, summary}, a string, or null. */
function lastRunOf(task) {
  const value = task.last_run
  if (!value) return { at: null, error: null, summary: null }
  if (typeof value === 'string') return { at: value, error: null, summary: null }
  return { at: value.at || null, error: value.error || null, summary: value.summary || null }
}

function summaryText(summary) {
  if (!summary) return null
  const parts = Object.entries(summary)
    .filter(([, v]) => v !== null && v !== '' && v !== false)
    .map(([k, v]) => `${k.replace(/_/g, ' ')} ${v}`)
  return parts.length ? parts.join(' · ') : null
}

export default function Scheduler({ status, loading, error, reload, isAdmin }) {
  const [busy, setBusy] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [stopping, setStopping] = useState(false)
  const [ran, setRan] = useState(null)

  async function act(id, fn) {
    setBusy(id)
    setActionError(null)
    try {
      const result = await fn()
      if (result) setRan({ id, result })
      reload()
    } catch (e) {
      setActionError(e)
    } finally {
      setBusy(null)
    }
  }

  if (loading) return <Loading rows={4} />
  if (error) return <ErrorBox error={error} onRetry={reload} />

  const tasks = status?.tasks || []

  return (
    <>
      <div className="card">
        <div className="card-header">
          <h3>
            Scheduler
            <HelpTip title="Where the loop runs">
              The loop can run inside the API process or from cron, and the two look identical
              from here except for this switch. If it says stopped and the tasks still show
              recent runs, something outside the application is driving them.
            </HelpTip>
          </h3>
          <div className="spacer" />
          {status?.running ? (
            <Badge tone="ok">running</Badge>
          ) : (
            <Badge tone="warn">stopped in this process</Badge>
          )}
          {isAdmin && !status?.running && (
            <button
              className="btn btn-sm"
              disabled={busy === 'start'}
              onClick={() => act('start', () => api.post('/monitoring/scheduler/start'))}
            >
              Start
            </button>
          )}
          {isAdmin && status?.running && (
            <button className="btn btn-sm btn-danger" onClick={() => setStopping(true)}>
              Stop
            </button>
          )}
        </div>

        {actionError && <ErrorBox error={actionError} />}

        <div className="row row-wrap small muted" style={{ marginBottom: 12 }}>
          <span>Started {status?.started_at ? formatDate(status.started_at) : '–'}</span>
          <span>· {status?.ticks ?? 0} ticks</span>
          <span>· one tick every {formatDuration(status?.tick_seconds)}</span>
        </div>

        {!isAdmin && (
          <p className="small muted">
            Only an administrator can start, stop or re-run these tasks. You can still recheck
            your own watchlist from the Watchlist tab — that runs your watches and nobody
            else’s.
          </p>
        )}

        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Task</th>
                <th>Every</th>
                <th>
                  Last run
                  <HelpTip title="What the last run did">
                    The counts the task returned, stored with it. A task with no last run has
                    not executed since this deployment was set up.
                  </HelpTip>
                </th>
                <th>
                  Due
                  <HelpTip title="Due">
                    Its interval has elapsed, so the next tick will run it. A task that is due
                    while the loop is stopped stays due and runs on the next start.
                  </HelpTip>
                </th>
                {isAdmin && <th />}
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => {
                const last = lastRunOf(task)
                const summary = summaryText(last.summary)
                return (
                  <tr key={task.name}>
                    <td>
                      <div>
                        <strong>{task.name}</strong>{' '}
                        {!task.enabled && <Badge>disabled</Badge>}
                      </div>
                      <div className="tiny muted">{task.description}</div>
                    </td>
                    <td className="nowrap">{formatDuration(task.interval_seconds)}</td>
                    <td>
                      <div className="small nowrap">{relativeDays(last.at)}</div>
                      {summary && <div className="tiny muted">{summary}</div>}
                      {last.error && (
                        <div className="tiny" title={last.error}>
                          <Badge tone="danger">failed</Badge>
                        </div>
                      )}
                    </td>
                    <td>{task.due ? <Badge tone="accent">due</Badge> : <Badge tone="ok">waiting</Badge>}</td>
                    {isAdmin && (
                      <td>
                        <div className="row" style={{ gap: 5, justifyContent: 'flex-end' }}>
                          <button
                            className="btn btn-sm"
                            disabled={busy === task.name}
                            onClick={() =>
                              act(task.name, () =>
                                api.post(`/monitoring/scheduler/tasks/${task.name}/run`),
                              )
                            }
                          >
                            Run now
                          </button>
                          <button
                            className="btn btn-sm"
                            disabled={busy === task.name}
                            onClick={() =>
                              act(`${task.name}-toggle`, () =>
                                api.post(`/monitoring/scheduler/tasks/${task.name}`, {
                                  enabled: !task.enabled,
                                }),
                              )
                            }
                          >
                            {task.enabled ? 'Disable' : 'Enable'}
                          </button>
                        </div>
                      </td>
                    )}
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>

        {ran && (
          <div className="alert alert-ok" style={{ marginTop: 12 }}>
            <div>
              <strong>{ran.id}</strong> finished.{' '}
              <span className="mono small">{JSON.stringify(ran.result).slice(0, 300)}</span>
            </div>
          </div>
        )}
      </div>

      {stopping && (
        <Modal
          title="Stop the scheduler?"
          onClose={() => setStopping(false)}
          actions={
            <>
              <button className="btn" onClick={() => setStopping(false)}>
                Leave it running
              </button>
              <button
                className="btn btn-danger"
                disabled={busy === 'stop'}
                onClick={async () => {
                  await act('stop', () => api.post('/monitoring/scheduler/stop'))
                  setStopping(false)
                }}
              >
                Stop it
              </button>
            </>
          }
        >
          <p>
            Watched companies stop being rechecked, follow-ups stop raising notifications and no
            weekly digest is written until it is started again — for every job seeker on this
            deployment, not only for you.
          </p>
          <p className="small muted">
            Retention deletion (NFR-303) and LLM redaction (FR-364) also stop. Both have legal
            deadlines attached, so if the loop is to stay off, run them from cron instead.
          </p>
        </Modal>
      )}
    </>
  )
}
