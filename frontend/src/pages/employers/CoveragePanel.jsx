/**
 * Employer coverage, for the administration screen (FR-341, FR-185, NFR-205).
 *
 * "62% resolved" reads as "38% are direct employers" unless the screen also
 * says how many nobody has looked at, what the missing answers are missing
 * *for*, and when the last sweep ran. So this panel states four things a
 * percentage on its own hides:
 *
 *   - unresearched employers as their own number, never inferred from a
 *     difference. An employer nobody has researched is not a direct employer.
 *   - two denominators. Eleven employers hold 515 of the corpus's 2 687
 *     postings, so resolving eleven names covers a fifth of the rows: the
 *     share of *employers* answered and the share of *vacancies* accounted for
 *     are different questions and diverge sharply.
 *   - `cannot_tell` by reason, because "30% unverified" mostly means "no
 *     website is known", not "the classifier is unsure", and the two want
 *     completely different work.
 *   - verdicts flagged for review, which is where a page that tried to
 *     instruct the classifier is recorded (NFR-205). The verdict beside it
 *     stands; the attempt is surfaced rather than swallowed.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { Caution, HelpTip } from '../../components/Help'
import {
  REASON_NEXT_STEP,
  ROLE_AGENCY,
  ROLE_BOARD,
  ROLE_DIRECT,
  ROLE_UNVERIFIED,
  RUNG_WORDS,
  TIER_WORDS,
  reasonWords,
} from '../../components/EmployerKindBadge'
import {
  ErrorBox,
  Loading,
  SectionCard,
  Stat,
  formatDate,
  formatPercent,
  useFetch,
} from '../../components/ui'

const ROLE_WORDS = {
  [ROLE_DIRECT]: 'Direct employer — hires for its own work',
  [ROLE_AGENCY]: 'Agency — posts for employers it does not name',
  [ROLE_BOARD]: 'Job board — relays other organisations’ postings',
  [ROLE_UNVERIFIED]: 'Not verified — we looked and could not tell',
}

export default function EmployerCoveragePanel({ phase = 'phase-0' }) {
  const [busy, setBusy] = useState(false)
  const [started, setStarted] = useState(null)
  const [actionError, setActionError] = useState(null)

  const { data, error, loading, reload } = useFetch(() => api.get('/employers/coverage'), [])

  if (loading) {
    return (
      <SectionCard icon="companies" title="Who is hiring: coverage" phase={phase}>
        <Loading rows={4} />
      </SectionCard>
    )
  }
  if (error) {
    return (
      <SectionCard icon="companies" title="Who is hiring: coverage" phase={phase}>
        <ErrorBox error={error} onRetry={reload} />
      </SectionCard>
    )
  }

  const c = data || {}
  const byRole = c.by_role || {}
  const vacanciesByRole = c.vacancies_by_role || {}
  const reasons = Object.entries(c.cannot_tell_by_reason || {}).sort((a, b) => b[1] - a[1])
  const rungs = Object.entries(c.by_rung || {}).sort((a, b) => b[1] - a[1])
  const tiers = Object.entries(c.by_tier || {}).sort((a, b) => b[1] - a[1])
  const queue = c.queue || []
  const last = c.last_pass

  async function runPass() {
    setBusy(true)
    setActionError(null)
    try {
      setStarted(await api.post('/employers/resolve-batch', { limit: 100 }))
      reload()
    } catch (err) {
      setActionError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <SectionCard
      icon="companies"
      title="Who is hiring: coverage"
      phase={phase}
      actions={
        <button className="btn btn-sm" disabled={busy || !c.queue_depth} onClick={runPass}>
          {busy ? <span className="spinner" /> : `Research the next ${Math.min(100, c.queue_depth || 0)}`}
        </button>
      }
    >
      <div className="grid grid-4" style={{ marginBottom: 14 }}>
        <Stat
          icon="companies"
          value={c.verdicts ?? '–'}
          label={`of ${c.employers_with_vacancies ?? '–'} employers answered`}
          phase={phase}
        />
        <Stat icon="help" value={c.unresearched_employers ?? '–'} label="nobody has looked yet" phase={phase} />
        <Stat
          icon="opportunities"
          value={
            c.vacancy_share_resolved == null ? '–' : formatPercent(c.vacancy_share_resolved, 0)
          }
          label={`of ${c.vacancies_with_employer ?? '–'} postings accounted for`}
          phase={phase}
        />
        <Stat icon="warning" value={c.flagged_for_review ?? 0} label="flagged for review" phase={phase} />
      </div>

      <p className="small muted" style={{ marginTop: 0 }}>
        {c.note}
      </p>

      <h4 style={{ fontSize: 12.5, margin: '16px 0 6px' }}>What the answers say</h4>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Answer</th>
              <th className="num">Employers</th>
              <th className="num">Postings</th>
            </tr>
          </thead>
          <tbody>
            {[ROLE_DIRECT, ROLE_AGENCY, ROLE_BOARD, ROLE_UNVERIFIED].map((role) => (
              <tr key={role}>
                <td>{ROLE_WORDS[role]}</td>
                <td className="num">{byRole[role] ?? 0}</td>
                <td className="num">{vacanciesByRole[role] ?? 0}</td>
              </tr>
            ))}
            <tr>
              <td className="muted">Not researched</td>
              <td className="num muted">{c.unresearched_employers ?? 0}</td>
              <td className="num muted">
                {Math.max((c.vacancies_with_employer ?? 0) - (c.vacancies_covered ?? 0), 0)}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="grid grid-2" style={{ marginTop: 16 }}>
        <div>
          <h4 style={{ fontSize: 12.5, margin: '0 0 6px' }}>
            Which rung answered
            <HelpTip term="resolution_rung" />
          </h4>
          {rungs.length === 0 ? (
            <p className="small muted">No verdicts yet.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <tbody>
                  {rungs.map(([rung, n]) => (
                    <tr key={rung}>
                      <td>{RUNG_WORDS[rung] || rung}</td>
                      <td className="num">{n}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {tiers.length > 0 && (
            <>
              <h4 style={{ fontSize: 12.5, margin: '14px 0 6px' }}>How sure the detector was</h4>
              <div className="table-wrap">
                <table>
                  <tbody>
                    {tiers.map(([tier, n]) => (
                      <tr key={tier}>
                        <td>{TIER_WORDS[tier] || tier}</td>
                        <td className="num">{n}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>

        <div>
          <h4 style={{ fontSize: 12.5, margin: '0 0 6px' }}>Where there is no answer, and why</h4>
          {reasons.length === 0 ? (
            <p className="small muted">
              Nothing came back as “could not tell”. That is either good coverage or a very small
              corpus — read it beside the unresearched count above.
            </p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Reason</th>
                    <th className="num">Employers</th>
                    <th>What would settle it</th>
                  </tr>
                </thead>
                <tbody>
                  {reasons.map(([reason, n]) => (
                    <tr key={reason}>
                      <td>{reasonWords(reason)}</td>
                      <td className="num">{n}</td>
                      <td className="small muted">{REASON_NEXT_STEP[reason] || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {queue.length > 0 && (
        <>
          <h4 style={{ fontSize: 12.5, margin: '16px 0 6px' }}>
            Next in the queue ({c.queue_depth} waiting)
          </h4>
          <p className="small muted" style={{ marginTop: 0 }}>
            Ordered by how many postings a wrong answer would affect, so a pass that is stopped
            half-way has still covered the rows that mattered most.
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Employer</th>
                  <th className="num">Postings</th>
                  <th>State</th>
                </tr>
              </thead>
              <tbody>
                {queue.map((row) => (
                  <tr key={row.company_id}>
                    <td>{row.name || row.company_id}</td>
                    <td className="num">{row.vacancy_count ?? 0}</td>
                    <td className="small muted">
                      {row.kind ? reasonWords(row.reason) || row.kind : 'not researched'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {actionError && <ErrorBox error={actionError} />}

      {started && (
        <div className="alert alert-info" style={{ marginTop: 12 }}>
          <div>
            {started.note ||
              `Requested ${started.requested ?? 0}; resolved ${started.resolved ?? 0}, could not tell ${
                started.cannot_tell ?? 0
              }.`}
          </div>
        </div>
      )}

      {last && (
        <p className="small muted" style={{ marginTop: 12, marginBottom: 0 }}>
          Last pass {last.status} · started {formatDate(last.started_at)} · {last.resolved ?? 0}{' '}
          resolved, {last.cannot_tell ?? 0} could not be told, {last.failed ?? 0} failed
          {last.reason ? ` · ${last.reason}` : ''}.
        </p>
      )}

      <Caution title="Read the two denominators together.">
        The share of employers answered and the share of postings accounted for measure different
        things: a handful of large employers carry a fifth of the corpus. And a company with no
        verdict is not a direct employer — it is a company nobody has looked at, which is why it
        has its own number above rather than being folded into the total.
      </Caution>
    </SectionCard>
  )
}
