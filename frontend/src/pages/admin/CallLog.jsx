/**
 * Browsing the AI call log (FR-364).
 *
 * The listing carries no prompt or response text — the backend leaves it out
 * of the list query on purpose, so casual browsing never renders somebody's
 * profile. One call at a time can be opened, and that open is written to the
 * audit trail under the administrator's name.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  Modal,
  SectionCard,
  useFetch,
} from '../../components/ui'
import { eur, num, stamp, tokens as fmtTokens } from './format'

const LIMIT = 50

export default function CallLog() {
  const [task, setTask] = useState('')
  const [status, setStatus] = useState('')
  const [page, setPage] = useState(0)
  const [openId, setOpenId] = useState(null)

  const log = useFetch(() => {
    const q = new URLSearchParams({ limit: String(LIMIT), offset: String(page * LIMIT) })
    if (task) q.set('task', task)
    if (status) q.set('status', status)
    return api.get(`/admin/llm-calls?${q.toString()}`)
  }, [task, status, page])

  const items = log.data?.items || []
  const taskOptions = [...new Set(items.map((i) => i.task))].sort()

  return (
    <SectionCard
      icon="sparkle"
      title="AI call log"
      phase="phase-0"
      actions={log.data ? <Badge>{num(log.data.total)} calls</Badge> : null}
    >
      <p className="small muted" style={{ marginTop: 0 }}>
        Which model was asked what, when, at what cost. Prompt and response text is not in
        the listing — it is fetched one call at a time, and opening one is itself written
        to the audit trail.
        <HelpTip
          title="Why opening a prompt is audited"
          align="right"
        >
          A prompt can contain a job seeker&rsquo;s profile. Reading one is a legitimate
          administrative act and a privacy-relevant one, so it leaves a trace under your
          name rather than happening invisibly.
        </HelpTip>
      </p>

      <div className="row row-wrap" style={{ marginBottom: 12 }}>
        <select value={task} onChange={(e) => { setTask(e.target.value); setPage(0) }} style={{ maxWidth: 240 }}>
          <option value="">Every task</option>
          {taskOptions.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
        <select value={status} onChange={(e) => { setStatus(e.target.value); setPage(0) }} style={{ maxWidth: 180 }}>
          <option value="">Any outcome</option>
          <option value="ok">Succeeded</option>
          <option value="error">Failed</option>
          {/* A model can answer 200 with nothing in it — a reasoning model that
              spent its whole budget thinking, or one that simply said nothing.
              Both are logged as what they were rather than as successes, so
              both have to be findable here. */}
          <option value="truncated">Truncated</option>
          <option value="empty">Empty answer</option>
        </select>
      </div>

      {log.loading ? (
        <Loading rows={5} />
      ) : log.error ? (
        <ErrorBox error={log.error} onRetry={log.reload} />
      ) : !items.length ? (
        <Empty title="No calls match this filter">
          Either nothing has run yet, or the text has already been redacted and the filter
          is narrower than the log. Clear the filters to see the whole log.
        </Empty>
      ) : (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th className="nowrap">When</th>
                  <th>Task</th>
                  <th>Model</th>
                  <th style={{ textAlign: 'right' }}>Tokens</th>
                  <th style={{ textAlign: 'right' }}>Cost</th>
                  <th style={{ textAlign: 'right' }}>Latency</th>
                  <th>Text</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {items.map((c) => (
                  <tr key={c.id}>
                    <td className="small nowrap">{stamp(c.created_at)}</td>
                    <td className="mono tiny">{c.task}</td>
                    <td className="small">
                      {c.model}
                      <div className="tiny muted">{c.provider}</div>
                    </td>
                    <td style={{ textAlign: 'right' }} className="small">
                      {fmtTokens((c.input_tokens || 0) + (c.output_tokens || 0))}
                    </td>
                    <td style={{ textAlign: 'right' }} className="small">
                      {eur(c.cost_eur)}
                    </td>
                    <td style={{ textAlign: 'right' }} className="small">
                      {c.latency_ms == null ? '–' : `${num(c.latency_ms)} ms`}
                    </td>
                    <td>
                      {c.redacted_at ? (
                        <Badge tone="ok">redacted</Badge>
                      ) : c.has_prompt_text ? (
                        <Badge>held</Badge>
                      ) : (
                        <Badge tone="info">none</Badge>
                      )}
                      {c.status !== 'ok' && <Badge tone="danger">{c.status}</Badge>}
                    </td>
                    <td style={{ textAlign: 'right' }}>
                      <button
                        className="btn btn-sm"
                        disabled={!c.has_prompt_text && !c.has_response_text}
                        onClick={() => setOpenId(c.id)}
                      >
                        Open
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="row" style={{ marginTop: 10 }}>
            <span className="small muted">
              {page * LIMIT + 1}–{page * LIMIT + items.length} of {num(log.data.total)}
            </span>
            <div className="spacer" />
            <button className="btn btn-sm" disabled={page === 0} onClick={() => setPage(page - 1)}>
              Newer
            </button>
            <button
              className="btn btn-sm"
              disabled={items.length < LIMIT}
              onClick={() => setPage(page + 1)}
            >
              Older
            </button>
          </div>
        </>
      )}

      {openId && <CallModal callId={openId} onClose={() => setOpenId(null)} />}
    </SectionCard>
  )
}

function CallModal({ callId, onClose }) {
  const call = useFetch(() => api.get(`/admin/llm-calls/${callId}`), [callId])
  return (
    <Modal title="AI call" onClose={onClose} wide>
      {call.loading && <Loading rows={4} />}
      {call.error && <ErrorBox error={call.error} onRetry={call.reload} />}
      {call.data && (
        <>
          <div className="row row-wrap" style={{ marginBottom: 12 }}>
            <Badge>{call.data.task}</Badge>
            <Badge>{call.data.model}</Badge>
            <Badge tone={call.data.status === 'ok' ? 'ok' : 'danger'}>{call.data.status}</Badge>
            <span className="small muted">{stamp(call.data.created_at)}</span>
            <span className="small muted">{eur(call.data.cost_eur)}</span>
          </div>
          {call.data.error && <ErrorBox error={{ message: call.data.error }} />}
          <h4>Prompt</h4>
          <div className="doc-preview tiny">
            {call.data.prompt_text || 'Redacted or never stored.'}
          </div>
          <h4 style={{ marginTop: 14 }}>Response</h4>
          <div className="doc-preview tiny">
            {call.data.response_text || 'Redacted or never stored.'}
          </div>
        </>
      )}
    </Modal>
  )
}

