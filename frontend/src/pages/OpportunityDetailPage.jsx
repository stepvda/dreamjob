/**
 * One opportunity, in full (FR-261..265, FR-281..285, FR-383, FR-402).
 *
 * The ranked list has to be scannable, so it shows a score and a paragraph.
 * This screen is where the same row is allowed to take up space: the normalised
 * record, the company it belongs to, the compensation estimate with the sources
 * it was built from, who to write to and how to reach them, and the whole score
 * breakdown behind the single number.
 *
 * For a speculative opening (FR-263) the difference is structural, not
 * cosmetic: there is no source vacancy to link to, the plausibility and the
 * evidence behind it take that section's place, and the disclosure note the
 * backend supplies is shown verbatim rather than paraphrased.
 */

import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { api } from '../api/client'
import { Caution, HelpTip, ScreenIntro } from '../components/Help'
import {
  Badge,
  ErrorBox,
  Field,
  KindBadge,
  Loading,
  Meter,
  Modal,
  Provenance,
  SubScores,
  ValidationBadge,
  formatDate,
  formatMoney,
  formatPercent,
  useFetch,
} from '../components/ui'
import EmployerKindBadge from '../components/EmployerKindBadge'
import { PostingOnBehalfNote, UndisclosedEmployer } from './employers'

const TIMING_LABELS = { apply_now: 'Apply now', favourable: 'Favourable window' }

const TAGS = [
  { value: 'destination', label: 'Destination' },
  { value: 'stepping_stone', label: 'Stepping stone' },
]

function human(value) {
  if (!value) return '–'
  return String(value).replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase())
}

/** `speculative_rationale` is stored as a JSON document but travels as text. */
function parseRationale(raw) {
  if (!raw) return null
  if (typeof raw === 'object') return raw
  try {
    const parsed = JSON.parse(raw)
    return typeof parsed === 'object' ? parsed : { rationale: String(raw) }
  } catch {
    return { rationale: String(raw) }
  }
}

function Row({ label, children, tip }) {
  return (
    <div
      className="row"
      style={{ alignItems: 'baseline', gap: 10, padding: '5px 0', flexWrap: 'wrap' }}
    >
      <span className="small muted" style={{ minWidth: 170 }}>
        {label}
        {tip}
      </span>
      <span style={{ flex: 1, minWidth: 180 }}>{children}</span>
    </div>
  )
}

function SkillList({ title, skills }) {
  const list = skills || []
  if (!list.length) return null
  return (
    <div>
      <h4>{title}</h4>
      <div className="chips">
        {list.map((s, i) => (
          <span className="chip" key={`${s}-${i}`}>
            {typeof s === 'string' ? s : s.name || s.skill || JSON.stringify(s)}
          </span>
        ))}
      </div>
    </div>
  )
}

/** FR-383: the fit meter as the requirement words it - met, partly, violated. */
function DreamFit({ detail }) {
  if (!detail) {
    return (
      <p className="small muted">
        This opportunity has not been scored yet, so there is no fit meter. Recalculate the
        campaign from the ranked list.
      </p>
    )
  }
  const groups = [
    { key: 'violated', label: 'Violated', tone: 'danger' },
    { key: 'partially_met', label: 'Partly met', tone: 'warn' },
    { key: 'met', label: 'Met', tone: 'ok' },
    { key: 'unknown', label: 'Not recorded either way', tone: undefined },
  ]
  return (
    <div className="col" style={{ gap: 12 }}>
      <Meter value={detail.score} />
      <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
        {groups.map((g) => (
          <Badge key={g.key} tone={g.tone}>
            {g.label}: {(detail[g.key] || []).length}
          </Badge>
        ))}
      </div>
      {groups.map((g) => {
        const list = detail[g.key] || []
        if (!list.length) return null
        return (
          <div key={g.key}>
            <h4>{g.label}</h4>
            <div className="col" style={{ gap: 5 }}>
              {list.map((c) => (
                <div
                  key={c.id}
                  className={g.key === 'violated' ? 'confidence-low' : undefined}
                  style={{ fontSize: 12.5, lineHeight: 1.5 }}
                >
                  <strong style={{ fontWeight: 550 }}>{c.criterion}</strong>
                  {c.importance && (
                    <span className="tiny muted"> · {human(c.importance)}</span>
                  )}
                  {c.explanation && <div className="muted">{c.explanation}</div>}
                </div>
              ))}
            </div>
          </div>
        )
      })}
      {detail.note && (
        <p className="tiny muted" style={{ margin: 0 }}>
          {detail.note}
        </p>
      )}
    </div>
  )
}

/* --- The screen ------------------------------------------------------------ */

export default function OpportunityDetailPage() {
  const { id } = useParams()
  const navigate = useNavigate()

  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')
  const [actionError, setActionError] = useState(null)
  const [busy, setBusy] = useState(false)

  const { data, error, loading, reload, setData } = useFetch(
    () => api.get(`/opportunities/${id}`),
    [id],
  )

  // FR-264 packages the range, its confidence and its source list together, so
  // the estimate can be read without unpacking the stored columns by hand.
  const comp = useFetch(
    () => api.get(`/opportunities/${id}/compensation`).catch(() => null),
    [id],
  )

  // FR-283: the shared vacancy row this was synthesised from. A speculative
  // opening has none, and the API says so with a 404 rather than an empty body.
  const vacancy = useFetch(
    () => (data?.vacancy_id ? api.get(`/opportunities/${id}/vacancy`).catch(() => null) : null),
    [id, data?.vacancy_id],
  )

  if (loading) {
    return (
      <div className="content-narrow">
        <Loading rows={6} />
      </div>
    )
  }

  if (error) {
    return (
      <div className="content-narrow">
        <ErrorBox error={error} onRetry={reload} />
        <Link className="btn" to="/opportunities">
          Back to the ranked list
        </Link>
      </div>
    )
  }

  if (!data) return null

  const o = data
  const spec = parseRationale(o.speculative_rationale)
  const tags = o.tags || []
  const contacts = o.contacts || []
  const paths = o.introduction_paths || []
  const compSources = comp.data?.sources?.sources || o.comp_sources?.sources || []
  const compMethod = comp.data?.sources?.method || o.comp_sources?.method

  const subscores = {
    score_profile_fit: o.score_profile_fit,
    score_dream_fit: o.score_dream_fit,
    score_directive_fit: o.score_directive_fit,
    score_company: o.score_company,
    score_compensation: o.score_compensation,
    score_plausibility: o.score_plausibility,
    score_reachability: o.score_reachability,
  }
  const components = o.score_detail?.components || {}

  async function patch(body) {
    setActionError(null)
    try {
      const updated = await api.patch(`/opportunities/${id}`, body)
      setData((d) => ({ ...d, ...updated }))
    } catch (e) {
      setActionError(e)
    }
  }

  async function reject() {
    if (!reason.trim()) return
    await patch({ user_status: 'not_interested', not_interested_reason: reason.trim() })
    setRejecting(false)
    setReason('')
  }

  async function rescore() {
    setBusy(true)
    setActionError(null)
    try {
      const updated = await api.post(`/opportunities/${id}/rescore`)
      setData((d) => ({ ...d, ...updated }))
    } catch (e) {
      setActionError(e)
    } finally {
      setBusy(false)
    }
  }

  function toggleTag(tag) {
    patch({ tags: tags.includes(tag) ? tags.filter((t) => t !== tag) : [...tags, tag] })
  }

  return (
    <div className="content-wide">
      <div className="row" style={{ marginBottom: 12 }}>
        <Link className="btn btn-sm btn-ghost" to="/opportunities">
          ← Ranked list
        </Link>
      </div>

      <ScreenIntro pathname="/opportunities">
        The whole record behind one row: what the role is, who it is with, what it is likely to
        pay, how you would reach them, and every number that put it where it is in the ranking.
      </ScreenIntro>

      {actionError && <ErrorBox error={actionError} />}

      {/* NFR-305 / CR-405: the score orders a list; it decides nothing. */}
      <Caution title="Scores are advisory">
        {o.advisory ||
          'Scores are advisory. They order a list; they do not decide anything. Every decision to keep, reject or apply is yours.'}
      </Caution>

      {/* FR-263: the disclosure the backend wrote, shown verbatim. */}
      {o.is_speculative && (
        <Caution title="This role is not advertised">
          {o.disclosure_note}{' '}
          <HelpTip term="speculative_opening" />
        </Caution>
      )}

      <div className="card">
        <div className="row row-wrap" style={{ gap: 8 }}>
          <h3 style={{ margin: 0, fontSize: 18 }}>{o.title}</h3>
          <KindBadge kind={o.kind} />
          {/* FR-143: a different axis from KindBadge - who is hiring, not how
              the opening was found. */}
          <EmployerKindBadge
            tag={o.employer}
            companyId={o.company_id}
            companyName={o.company_name}
            onChange={reload}
          />
          {o.timing_flag && (
            <Badge tone="warn">{TIMING_LABELS[o.timing_flag] || human(o.timing_flag)}</Badge>
          )}
          {o.pinned && <Badge tone="accent">Pinned</Badge>}
          {o.manual_rank != null && <Badge tone="accent">Your position #{o.manual_rank}</Badge>}
          {o.user_status === 'not_interested' && <Badge tone="danger">Not interested</Badge>}
          {o.user_status === 'applied' && <Badge tone="ok">Applied</Badge>}
          <div className="spacer" />
          <label className="checkline">
            <input
              type="checkbox"
              checked={Boolean(o.selected)}
              onChange={(e) => patch({ selected: e.target.checked })}
            />
            On my shortlist
          </label>
        </div>

        <div className="opp-company" style={{ marginTop: 4 }}>
          {o.company_id ? (
            <Link to={`/companies/${o.company_id}`}>{o.company_name || 'Company profile'}</Link>
          ) : (
            o.company_name || 'Company not identified'
          )}
          {o.location ? ` · ${o.location}` : ''}
          {o.country ? ` · ${o.country}` : ''}
          {o.posted_at ? ` · posted ${formatDate(o.posted_at)}` : ''}
        </div>

        <div className="row row-wrap" style={{ gap: 5, marginTop: 10 }}>
          {TAGS.map((t) => (
            <span
              key={t.value}
              className={`chip clickable${tags.includes(t.value) ? ' on' : ''}`}
              onClick={() => toggleTag(t.value)}
            >
              {t.label}
            </span>
          ))}
          {tags
            .filter((t) => !TAGS.some((k) => k.value === t))
            .map((t) => (
              <span className="chip" key={t}>
                {t}
              </span>
            ))}
          <HelpTip term="stepping_stone" />
        </div>

        <div className="row row-wrap" style={{ gap: 8, marginTop: 14 }}>
          <button className="btn btn-sm" onClick={() => patch({ pinned: !o.pinned })}>
            {o.pinned ? 'Unpin' : 'Pin above the computed order'}
          </button>
          {o.user_status === 'not_interested' ? (
            <button className="btn btn-sm" onClick={() => patch({ user_status: 'interested' })}>
              Reconsider
            </button>
          ) : (
            <button className="btn btn-sm btn-danger" onClick={() => setRejecting(true)}>
              Not interested
            </button>
          )}
          <button className="btn btn-sm" disabled={busy} onClick={rescore}>
            {busy ? <span className="spinner" /> : 'Rescore this one'}
          </button>
          <div className="spacer" />
          {(o.links?.source_vacancy || o.source_url) && (
            <a
              className="btn btn-sm"
              href={o.links?.source_vacancy || o.source_url}
              target="_blank"
              rel="noreferrer noopener"
            >
              Open the source posting ↗
            </a>
          )}
        </div>
      </div>

      <div className="grid grid-2" style={{ marginTop: 14 }}>
        {/* --- Score breakdown (FR-281, FR-282) ------------------------------ */}
        <div className="card">
          <div className="card-header">
            <h3>Why it ranks here</h3>
          </div>
          <Meter value={o.score} />
          {o.rationale && (
            <p className="opp-rationale" style={{ marginTop: 10 }}>
              {o.rationale}
            </p>
          )}
          <div style={{ marginTop: 14 }}>
            <h4>
              Sub-scores
              <HelpTip term="reachability" />
            </h4>
            <SubScores scores={subscores} />
          </div>
          {Object.keys(components).length > 0 && (
            <div style={{ marginTop: 14 }}>
              <h4>What each component actually looked at</h4>
              <div className="col" style={{ gap: 8 }}>
                {Object.entries(components).map(([name, c]) => (
                  <div key={name}>
                    <div className="row" style={{ gap: 6 }}>
                      <strong style={{ fontSize: 12.5 }}>{human(name)}</strong>
                      <Badge>{c.method}</Badge>
                      <span className="small muted">{c.score == null ? '–' : c.score}</span>
                    </div>
                    <ul style={{ margin: '2px 0 0', paddingLeft: 18 }}>
                      {(c.reasons || []).slice(0, 4).map((r, i) => (
                        <li key={i} className="small muted" style={{ lineHeight: 1.45 }}>
                          {r}
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            </div>
          )}
          {o.scored_at && (
            <p className="tiny muted" style={{ marginTop: 12 }}>
              Last scored {formatDate(o.scored_at)}. A recalculation rewrites these numbers and
              never your position, pin, tags or shortlist (FR-284).
            </p>
          )}
        </div>

        {/* --- Dream-job fit (FR-383) ---------------------------------------- */}
        <div className="card">
          <div className="card-header">
            <h3>
              Dream-job fit
              <HelpTip term="dream_job_fit" />
            </h3>
          </div>
          <DreamFit detail={o.dream_fit_detail} />
        </div>
      </div>

      {/* --- The record itself (FR-261) -------------------------------------- */}
      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-header">
          <h3>The role</h3>
        </div>
        <div className="grid grid-3" style={{ marginBottom: 12 }}>
          <Row label="Function">{human(o.function_family)}</Row>
          <Row label="Seniority">{human(o.seniority)}</Row>
          <Row label="Work arrangement">
            {human(o.work_arrangement)}
            {o.remote_days != null ? ` · ${o.remote_days} remote days` : ''}
          </Row>
          <Row label="Contract">
            {human(o.contract_type)}
            {o.fte_percentage != null ? ` · ${o.fte_percentage}% FTE` : ''}
          </Row>
          <Row label="Language of the posting">{(o.language || 'en').toUpperCase()}</Row>
          <Row label="Application channel">{human(o.application_channel)}</Row>
        </div>

        {o.description ? (
          <div className="doc-preview">{o.description}</div>
        ) : (
          <p className="small muted">No description was collected for this role.</p>
        )}

        <div className="grid grid-2" style={{ marginTop: 14 }}>
          <SkillList title="Required" skills={o.required_skills} />
          <SkillList title="Desirable" skills={o.desirable_skills} />
        </div>
      </div>

      {/* FR-143/FR-281: when the employer is not the poster, the company card
          below is about the agency.  This says so, in words, before the reader
          gets there - and offers the questions that would make the employer
          knowable. */}
      <UndisclosedEmployer tag={o.employer} />
      <PostingOnBehalfNote tag={o.employer} />

      <div className="grid grid-2" style={{ marginTop: 14 }}>
        {/* --- Company (FR-222) ---------------------------------------------- */}
        <div className="card">
          <div className="card-header">
            <h3>Company</h3>
            <div className="spacer" />
            {o.company_id && (
              <Link className="btn btn-sm" to={`/companies/${o.company_id}`}>
                Full profile
              </Link>
            )}
          </div>
          <Row label="Name">{o.company_name || '–'}</Row>
          <Row label="Country">{o.company_country || '–'}</Row>
          <Row label="Size">{human(o.company_size_band)}</Row>
          <Row label="Stage">{human(o.company_stage)}</Row>
          <Row label="Trajectory">{human(o.company_trajectory)}</Row>
          <Row label="Site">
            {o.company_domain ? (
              <a
                href={`https://${o.company_domain}`}
                target="_blank"
                rel="noreferrer noopener"
              >
                {o.company_domain}
              </a>
            ) : (
              '–'
            )}
          </Row>
          {o.company_careers_url && (
            <Row label="Careers page">
              <a href={o.company_careers_url} target="_blank" rel="noreferrer noopener">
                {o.company_careers_url}
              </a>
            </Row>
          )}
          {o.employer_rating != null && (
            <Row label="Employer rating">
              {o.employer_rating.toFixed(1)} / 5
              <span className="tiny muted"> · advisory input only (FR-265)</span>
            </Row>
          )}
        </div>

        {/* --- Compensation (FR-264, FR-265) --------------------------------- */}
        <div className="card">
          <div className="card-header">
            <h3>
              Compensation estimate
              <HelpTip term="compensation_estimate" />
            </h3>
          </div>
          {o.comp_max == null ? (
            <p className="small muted">
              No range has been estimated for this role yet. Recalculating the campaign fills
              this in from comparable posted ranges and market observations; “Rescore” on this
              page does the same for this role alone.
            </p>
          ) : (
            <>
              <div style={{ fontSize: 19, fontWeight: 600, letterSpacing: '-0.01em' }}>
                {formatMoney(o.comp_min, o.comp_currency || 'EUR')} –{' '}
                {formatMoney(o.comp_max, o.comp_currency || 'EUR')}
              </div>
              <div className="row row-wrap" style={{ gap: 6, marginTop: 6 }}>
                <Badge tone={o.comp_is_stated ? 'ok' : 'info'}>
                  {o.comp_is_stated ? 'Stated by the employer' : 'Estimated'}
                </Badge>
                {o.comp_confidence != null && (
                  <Badge tone={o.comp_confidence >= 0.6 ? 'ok' : 'warn'}>
                    Confidence {formatPercent(o.comp_confidence, 0)}
                  </Badge>
                )}
                {compMethod && <Badge>{human(compMethod)}</Badge>}
              </div>
            </>
          )}

          {compSources.length > 0 && (
            <div style={{ marginTop: 14 }}>
              <h4>
                Sources it was built from
                <HelpTip term="provenance" />
              </h4>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Source</th>
                      <th className="num">Range</th>
                      <th className="num">Weight</th>
                      <th>Used</th>
                    </tr>
                  </thead>
                  <tbody>
                    {compSources.map((s, i) => (
                      <tr key={i}>
                        <td>
                          {s.label || human(s.kind)}
                          {s.url && (
                            <div>
                              <Provenance source={s.url} />
                            </div>
                          )}
                          {s.note && <div className="tiny muted">{s.note}</div>}
                        </td>
                        <td className="num">
                          {formatMoney(s.low, s.currency || 'EUR')}–
                          {formatMoney(s.high, s.currency || 'EUR')}
                        </td>
                        <td className="num">{s.weight?.toFixed?.(2) ?? '–'}</td>
                        <td>{s.used ? 'yes' : 'no'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
          {comp.data?.note && (
            <p className="tiny muted" style={{ marginTop: 10 }}>
              {comp.data.note}
            </p>
          )}
        </div>
      </div>

      <div className="grid grid-2" style={{ marginTop: 14 }}>
        {/* --- Contact and introduction (FR-301..305, FR-461) ---------------- */}
        <div className="card">
          <div className="card-header">
            <h3>
              Getting to a person
              <HelpTip term="reachability" />
            </h3>
          </div>
          {contacts.length === 0 && paths.length === 0 && (
            <p className="small muted">
              No contact has been found for this company yet, and no warm route is on record. A
              role you cannot reach scores lower than one you can, which is why reachability is one
              of the seven sub-scores.
            </p>
          )}

          {contacts.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Role</th>
                    <th>Email</th>
                    <th>Checked</th>
                  </tr>
                </thead>
                <tbody>
                  {contacts.slice(0, 8).map((c) => (
                    <tr key={c.id}>
                      <td>{c.full_name || <span className="muted">shared mailbox</span>}</td>
                      <td>
                        {c.role_title || '–'}
                        {c.is_generic_mailbox ? (
                          <>
                            {' '}
                            <HelpTip term="role_address" />
                          </>
                        ) : null}
                      </td>
                      <td className="mono">{c.email || '–'}</td>
                      <td>
                        <ValidationBadge result={c.email_validation} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {paths.length > 0 && (
            <div style={{ marginTop: 14 }}>
              <h4>Introduction routes</h4>
              <div className="col" style={{ gap: 8 }}>
                {paths.map((p) => (
                  <div key={p.id} className="row row-wrap" style={{ gap: 8 }}>
                    <Badge tone="accent">{human(p.relationship)}</Badge>
                    <strong style={{ fontSize: 12.5 }}>{p.intermediary_name || 'Unnamed'}</strong>
                    <span className="small muted">{p.intermediary_role || ''}</span>
                    <span className="small muted">
                      strength {Math.round((p.strength ?? 0) * 100)}%
                    </span>
                    <Badge>{p.status}</Badge>
                  </div>
                ))}
              </div>
              <p className="tiny muted" style={{ marginTop: 8 }}>
                Routes are drafted and sent from the Networking screen.
              </p>
            </div>
          )}
        </div>

        {/* --- Provenance: the source vacancy, or the speculative case ------- */}
        <div className="card">
          <div className="card-header">
            <h3>
              {o.is_speculative ? 'Why we think this role exists' : 'Where this came from'}
              {o.is_speculative && <HelpTip term="plausibility" />}
            </h3>
          </div>

          {o.is_speculative ? (
            <div className="col" style={{ gap: 10 }}>
              <Row label="Plausibility">
                <Meter value={o.plausibility == null ? null : o.plausibility * 100} />
              </Row>
              {spec?.rationale && <p style={{ fontSize: 13, lineHeight: 1.6 }}>{spec.rationale}</p>}
              {spec?.timing && (
                <Row label="Timing" tip={<HelpTip term="timing_window" />}>
                  {spec.timing}
                </Row>
              )}
              {spec?.likely_department && (
                <Row label="Likely department">{spec.likely_department}</Row>
              )}
              {spec?.likely_decision_maker_role && (
                <Row label="Likely decision maker">{spec.likely_decision_maker_role}</Row>
              )}
              {spec?.basis && <Row label="Basis">{spec.basis}</Row>}
              {Array.isArray(spec?.risks) && spec.risks.length > 0 && (
                <div>
                  <h4>What could make this wrong</h4>
                  <ul className="help-tips">
                    {spec.risks.map((r, i) => (
                      <li key={i}>{r}</li>
                    ))}
                  </ul>
                </div>
              )}
              {Array.isArray(spec?.evidence) && spec.evidence.length > 0 && (
                <div>
                  <h4>Evidence</h4>
                  <ul className="help-tips">
                    {spec.evidence.map((e, i) => (
                      <li key={i}>{typeof e === 'string' ? e : e.summary || JSON.stringify(e)}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          ) : (
            <div className="col" style={{ gap: 6 }}>
              <Row label="Collected by">{o.source_adapter || '–'}</Row>
              <Row label="Posted">{formatDate(o.posted_at || o.vacancy_posted_at)}</Row>
              <Row label="Source" tip={<HelpTip term="provenance" />}>
                {o.links?.source_vacancy ? (
                  <a href={o.links.source_vacancy} target="_blank" rel="noreferrer noopener">
                    {o.links.source_vacancy}
                  </a>
                ) : (
                  '–'
                )}
              </Row>
              {vacancy.data && (
                <>
                  <Row label="Freshness" tip={<HelpTip term="staleness" />}>
                    Collected {formatDate(vacancy.data.collected_at)} · confidence{' '}
                    {formatPercent(vacancy.data.confidence, 0)}
                  </Row>
                  <Row label="Access method">{human(vacancy.data.access_method)}</Row>
                  {vacancy.data.company_name_raw && (
                    <Row label="Employer as advertised">{vacancy.data.company_name_raw}</Row>
                  )}
                </>
              )}
              <p className="tiny muted" style={{ marginTop: 6 }}>
                The advertisement lives in the shared knowledge base and is re-collected as it goes
                stale; this record is your own stable snapshot of it.
              </p>
            </div>
          )}
        </div>
      </div>

      {o.user_status === 'not_interested' && o.not_interested_reason && (
        <div className="card" style={{ marginTop: 14 }}>
          <h4>Why you rejected this</h4>
          <p style={{ margin: 0, fontSize: 13 }}>{o.not_interested_reason}</p>
          <p className="tiny muted" style={{ marginTop: 6 }}>
            Reasons like this re-tune your scoring weights (FR-285).
          </p>
        </div>
      )}

      <div className="row" style={{ marginTop: 18, gap: 8 }}>
        <button
          className="btn btn-primary"
          onClick={async () => {
            if (!o.selected) await patch({ selected: true })
            navigate('/applications')
          }}
        >
          Prepare an application for this role
        </button>
        <Link className="btn" to="/opportunities">
          Back to the ranked list
        </Link>
      </div>

      {rejecting && (
        <Modal
          title={`Not interested: ${o.title}`}
          onClose={() => setRejecting(false)}
          actions={
            <>
              <button className="btn" onClick={() => setRejecting(false)}>
                Cancel
              </button>
              <button className="btn btn-danger" disabled={!reason.trim()} onClick={reject}>
                Mark not interested
              </button>
            </>
          }
        >
          <p className="section-intro">
            The reason is required. FR-285 re-tunes your scoring weights from what you reject and
            why, so an unexplained rejection teaches the ranking nothing.
          </p>
          <Field label="Why not?" hint="A sentence is enough. Only you ever read it.">
            <textarea
              value={reason}
              autoFocus
              onChange={(e) => setReason(e.target.value)}
              placeholder="Too junior; the commute is impossible; I do not want agency work."
            />
          </Field>
        </Modal>
      )}
    </div>
  )
}
