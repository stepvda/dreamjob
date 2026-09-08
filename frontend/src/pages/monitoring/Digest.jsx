/**
 * The weekly digest (FR-403).
 *
 * One digest per week: what arrived, what answered, what is overdue, what
 * changed at the companies you watch — and exactly one recommended next
 * action. The single action is the point. A digest that lists nine things you
 * could do is a to-do list, and you already have one; this one takes a
 * position and says why it beat everything else.
 *
 * Two response shapes arrive here and they are not the same. A *stored* digest
 * (GET /digests, /digests/{id}) keeps its figures under `content` and its
 * action title in `recommended_action` with the detail beside it; a *freshly
 * generated* one (POST /digests) returns `counts`, `sections` and
 * `recommended_action` as an object. `normalise` folds both into one shape so
 * the rendering below never has to know which it is looking at.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../../api/client'
import { Caution, HelpTip } from '../../components/Help'
import {
  Badge,
  Empty,
  ErrorBox,
  KindBadge,
  Loading,
  formatDate,
} from '../../components/ui'
import { Stat, relativeDays } from './shared'

function normalise(row) {
  if (!row) return null
  const content = row.content || {}
  const action =
    row.recommended_action && typeof row.recommended_action === 'object'
      ? row.recommended_action
      : row.recommended_action_detail || null
  return {
    id: row.id ?? null,
    period_start: row.period_start,
    period_end: row.period_end,
    counts: row.counts || content.counts || {},
    sections: row.sections || content.sections || {},
    action,
    action_title:
      action?.title ||
      (typeof row.recommended_action === 'string' ? row.recommended_action : null),
    emailed_at: row.emailed_at ?? null,
    email_error: row.email_error ?? null,
    created_at: row.created_at ?? null,
  }
}

function Section({ title, items, count, children }) {
  if (!count) return null
  return (
    <div style={{ marginTop: 16 }}>
      <div className="card-header" style={{ marginBottom: 8 }}>
        <h3 className="small">{title}</h3>
        <div className="spacer" />
        <Badge>{count}</Badge>
      </div>
      {children}
      {count > (items || []).length && (
        <div className="tiny muted" style={{ marginTop: 6 }}>
          … and {count - items.length} more, counted but not listed.
        </div>
      )}
    </div>
  )
}

function DigestBody({ digest }) {
  const s = digest.sections || {}
  const c = digest.counts || {}
  const pipeline = s.pipeline || {}
  const nothing = Object.values(c).every((v) => !v)

  return (
    <>
      <div className="grid grid-4" style={{ marginBottom: 4 }}>
        <Stat
          label="New opportunities"
          value={c.new_opportunities}
          tip={
            <HelpTip title="Already in your ranked list">
              A vacancy found at a watched company is added to the campaign the watch names, so
              these are counted here and ranked there. They arrive unscored and are picked up by
              the next scoring pass, which is why one may show no score yet.
            </HelpTip>
          }
        />
        <Stat label="Replies" value={c.replies} />
        <Stat label="Follow-ups due" value={c.follow_ups_due} />
        <Stat label="Watchlist changes" value={c.watchlist_changes} />
      </div>

      {/* FR-403: exactly one recommended action, with the reason it won. */}
      {digest.action_title && (
        <div className="mon-action" style={{ marginTop: 14 }}>
          <div className="small muted">
            The one thing to do next
            <HelpTip term="digest_action" />
          </div>
          <div style={{ fontWeight: 650, margin: '3px 0 4px' }}>{digest.action_title}</div>
          {digest.action?.reason && <div className="small">{digest.action.reason}</div>}
          {digest.action?.url && (
            <div style={{ marginTop: 9 }}>
              <Link className="btn btn-sm btn-primary" to={digest.action.url}>
                Go there
              </Link>
            </div>
          )}
        </div>
      )}

      {nothing && (
        <p className="section-intro" style={{ marginTop: 14 }}>
          Nothing new in this period. That is information too: if your campaigns have finished
          collecting, the next move is yours rather than the system’s.
        </p>
      )}

      <Section
        title="New opportunities"
        count={c.new_opportunities}
        items={s.new_opportunities || []}
      >
        <div className="table-wrap">
          <table>
            <tbody>
              {(s.new_opportunities || []).map((o) => (
                <tr key={o.id}>
                  <td>
                    <Link to={`/opportunities/${o.id}`}>{o.title}</Link>
                    <div className="tiny muted">{o.company_name || 'unknown company'}</div>
                  </td>
                  <td>
                    {/* FR-263: a speculative opening is never shown as a vacancy. */}
                    <KindBadge kind={o.kind} />
                  </td>
                  <td className="num">{o.score == null ? '–' : Math.round(o.score)}</td>
                  <td>{o.timing_flag && <Badge tone="ok">timing favourable</Badge>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="Replies received" count={c.replies} items={s.replies || []}>
        <div className="table-wrap">
          <table>
            <tbody>
              {(s.replies || []).map((r) => (
                <tr key={r.id}>
                  <td>
                    {r.company_name || r.from_address}
                    <div className="tiny muted">{r.subject}</div>
                  </td>
                  <td>
                    <Badge tone={r.classification === 'rejection' ? 'danger' : 'info'}>
                      {String(r.classification || 'unclassified').replace(/_/g, ' ')}
                    </Badge>
                  </td>
                  <td className="small muted nowrap">{formatDate(r.received_at)}</td>
                  <td>{r.handled ? <Badge tone="ok">handled</Badge> : <Badge tone="warn">open</Badge>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="Follow-ups due" count={c.follow_ups_due} items={s.follow_ups_due || []}>
        <div className="table-wrap">
          <table>
            <tbody>
              {(s.follow_ups_due || []).map((f) => (
                <tr key={f.dispatch_id}>
                  <td>
                    {f.company_name || f.recipient_email}
                    <div className="tiny muted">{f.opportunity_title || f.subject}</div>
                  </td>
                  <td className="small nowrap">
                    due {formatDate(f.follow_up_due_at)} · {relativeDays(f.follow_up_due_at)}
                  </td>
                  <td>
                    <Link to="/pipeline">Open the pipeline</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section
        title="Watchlist changes"
        count={c.watchlist_changes}
        items={s.watchlist_changes || []}
      >
        <ul className="help-tips">
          {(s.watchlist_changes || []).map((w) => (
            <li key={w.id}>
              <strong>{w.title}</strong>
              {w.body ? ` — ${w.body}` : ''}
            </li>
          ))}
        </ul>
      </Section>

      <Section title="Drafts waiting for you" count={c.drafts_waiting} items={s.drafts_waiting || []}>
        <ul className="help-tips">
          {(s.drafts_waiting || []).map((d) => (
            <li key={d.id}>
              {d.subject || 'untitled draft'}{' '}
              <span className="muted">({String(d.kind || '').replace(/_/g, ' ')})</span>
            </li>
          ))}
        </ul>
      </Section>

      <Section title="Pipeline movements" count={c.stage_changes} items={s.stage_changes || []}>
        <ul className="help-tips">
          {(s.stage_changes || []).map((k) => (
            <li key={k.id}>
              <strong>{k.opportunity_title || 'an application'}</strong> at{' '}
              {k.company_name || 'a company'} moved to <Badge tone="info">{k.stage}</Badge>
            </li>
          ))}
        </ul>
      </Section>

      {pipeline.total > 0 && (
        <div className="small muted" style={{ marginTop: 16 }}>
          Pipeline:{' '}
          {Object.entries(pipeline.stages || {})
            .filter(([, v]) => v)
            .map(([k, v]) => `${k} ${v}`)
            .join(' · ')}
          {c.silent_applications > 0 &&
            ` · ${c.silent_applications} never got an answer`}
        </div>
      )}
    </>
  )
}

export default function Digest({ digests, loading, error, reload }) {
  const [selectedId, setSelectedId] = useState(null)
  const [periodDays, setPeriodDays] = useState(7)
  const [email, setEmail] = useState(false)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState(null)
  const [fresh, setFresh] = useState(null)

  const rows = digests || []
  // The list is ordered by period_end descending, so the latest digest is the
  // first row. Reading it from here rather than from /digests/latest keeps the
  // "you have none yet" case out of the error path — that endpoint answers 404.
  const stored = rows.find((d) => d.id === selectedId) || rows[0] || null
  const digest = normalise(fresh || stored)

  async function generate() {
    setBusy(true)
    setActionError(null)
    try {
      setFresh(await api.post('/monitoring/digests', { period_days: Number(periodDays), email }))
      setSelectedId(null)
      reload()
    } catch (e) {
      setActionError(e)
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <Loading rows={5} />
  if (error) return <ErrorBox error={error} onRetry={reload} />

  return (
    <>
      {/* NFR-305: the digest recommends, it never acts. */}
      <Caution title="The digest suggests; it never sends">
        The recommended action is a suggestion with a link. No e-mail is written, sent or
        scheduled because it appears here, and the ranking behind “new opportunities” stays
        advisory.
      </Caution>

      <div className="card">
        <div className="card-header">
          <h3>
            Generate a digest
            <HelpTip title="Re-running is safe">
              A digest is stored once per period. Generating one for a period that already has a
              digest refreshes it in place rather than adding a second.
            </HelpTip>
          </h3>
        </div>
        <div className="row row-wrap" style={{ alignItems: 'flex-end' }}>
          <div className="field" style={{ marginBottom: 0, width: 160 }}>
            <label>Period (days)</label>
            <input
              type="number"
              min={1}
              max={60}
              value={periodDays}
              onChange={(e) => setPeriodDays(e.target.value)}
            />
          </div>
          <label className="checkline">
            <input type="checkbox" checked={email} onChange={(e) => setEmail(e.target.checked)} />
            Also e-mail it to me
          </label>
          <div className="spacer" />
          <button className="btn btn-primary" onClick={generate} disabled={busy}>
            {busy ? <span className="spinner" /> : 'Generate now'}
          </button>
        </div>
        {actionError && <ErrorBox error={actionError} />}
        {digest?.email_error && (
          <div className="alert alert-warn" style={{ marginTop: 10 }}>
            <div>
              The digest was stored and shown here, but not e-mailed: {digest.email_error}
            </div>
          </div>
        )}
      </div>

      {!digest && (
        <Empty
          title="No digest has been generated yet"
          action={
            <button className="btn btn-primary" onClick={generate} disabled={busy}>
              Generate the first one
            </button>
          }
        >
          The scheduler writes one a week for every active job seeker. You can also ask for one
          at any moment — it reads the same data, so nothing is lost by generating it early.
        </Empty>
      )}

      {digest && (
        <div className="card">
          <div className="card-header">
            <h3>
              Digest for {formatDate(digest.period_start)} – {formatDate(digest.period_end)}
            </h3>
            <div className="spacer" />
            {digest.emailed_at && <Badge tone="ok">e-mailed {formatDate(digest.emailed_at)}</Badge>}
            {fresh && <Badge tone="accent">just generated</Badge>}
          </div>
          <DigestBody digest={digest} />
        </div>
      )}

      {rows.length > 1 && (
        <div className="card">
          <div className="card-header">
            <h3>Past digests</h3>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Period</th>
                  <th>Recommended action</th>
                  <th className="num">New</th>
                  <th className="num">Replies</th>
                  <th className="num">Due</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const d = normalise(row)
                  return (
                    <tr key={row.id}>
                      <td className="nowrap">{formatDate(d.period_end)}</td>
                      <td className="small">{d.action_title || <span className="muted">none</span>}</td>
                      <td className="num">{d.counts.new_opportunities ?? 0}</td>
                      <td className="num">{d.counts.replies ?? 0}</td>
                      <td className="num">{d.counts.follow_ups_due ?? 0}</td>
                      <td>
                        <button
                          className="btn btn-sm"
                          onClick={() => {
                            setFresh(null)
                            setSelectedId(row.id)
                          }}
                        >
                          View
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  )
}
