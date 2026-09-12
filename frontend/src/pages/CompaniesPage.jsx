/**
 * Companies - search and browse the shared knowledge base (FR-341, FR-345).
 *
 * The screen is deliberately campaign-independent: company research is common
 * property of every job seeker (FR-341) and a shared row carries no link back
 * to the campaign that produced it (FR-344), so there is nothing to scope the
 * list to and no campaign picker here.
 *
 * The freshness mark on every row is the honest part. A profile still inside
 * its staleness window is reused rather than re-crawled (FR-226, FR-343), so
 * what you are reading may be months old - and you have to be able to see that
 * before you act on it.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { Caution, FirstRun, HelpTip, ScreenIntro } from '../components/Help'
import WorkflowMap from '../components/WorkflowMap'
import {
  Badge,
  Empty,
  ErrorBox,
  Field,
  Loading,
  Modal,
  formatDate,
  useFetch,
} from '../components/ui'
import { useSession } from '../session'

const PAGE_SIZE = 25

/** company.size_band, as the profiling prompt and DR-101 define the bands. */
const SIZE_BANDS = ['1-10', '11-50', '51-200', '201-500', '501-1000', '1001-5000', '5000+']

/** company.stage / company.trajectory, from the company table's own comments. */
const STAGES = ['startup', 'scaleup', 'established', 'listed', 'public', 'nonprofit']
const TRAJECTORIES = ['growing', 'stable', 'declining', 'restructuring', 'volatile']

const TRAJECTORY_TONE = {
  growing: 'ok',
  stable: 'info',
  declining: 'warn',
  restructuring: 'warn',
  volatile: 'warn',
}

function queryString(params) {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== '' && value != null) search.set(key, value)
  })
  return search.toString()
}

/** Days since a profile was last collected or refreshed. */
export function ageDays(row) {
  const iso = row?.refreshed_at || row?.collected_at
  if (!iso) return null
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return null
  return Math.max(0, (Date.now() - then) / 86_400_000)
}

/** FR-226 / FR-343: how old this record is, and whether that is past policy. */
function Freshness({ row }) {
  const days = ageDays(row)
  const label = days == null ? 'never collected' : days < 1 ? 'today' : `${Math.round(days)} d old`
  return (
    <div className="col" style={{ gap: 2 }}>
      <span>{row.stale ? <Badge tone="warn">stale</Badge> : <Badge tone="ok">fresh</Badge>}</span>
      <span className="tiny muted nowrap">
        {label}
        {row.refreshed_at || row.collected_at
          ? ` · ${formatDate(row.refreshed_at || row.collected_at)}`
          : ''}
      </span>
    </div>
  )
}

function sectorLabel(codes) {
  if (!Array.isArray(codes) || !codes.length) return '–'
  return codes
    .slice(0, 2)
    .map((c) => c.label || c.code || c.system)
    .filter(Boolean)
    .join(', ')
}

export default function CompaniesPage() {
  const { session } = useSession()
  const isAdmin = Boolean(session?.is_admin)
  // Text filters are drafted and applied on submit: country is an exact
  // two-letter match and sector a LIKE, so re-querying per keystroke would ask
  // the server a question the user has not finished typing.
  const [draft, setDraft] = useState('')
  const [countryDraft, setCountryDraft] = useState('')
  const [sectorDraft, setSectorDraft] = useState('')
  const [filters, setFilters] = useState({
    q: '', country: '', sector: '', size_band: '', ats_vendor: '',
  })

  // FR-345 asks for stage and trajectory too. The search endpoint filters on
  // q/country/sector/size_band only, so these two narrow the page in the
  // browser and say so, rather than pretending to be server-side facets.
  const [stage, setStage] = useState('')
  const [trajectory, setTrajectory] = useState('')
  const [offset, setOffset] = useState(0)

  const { data, error, loading, reload, setData } = useFetch(
    () => api.get(`/companies?${queryString({ ...filters, limit: PAGE_SIZE, offset })}`),
    [filters.q, filters.country, filters.sector, filters.size_band, filters.ats_vendor, offset],
  )

  // FR-401: the watchlist is private, so it is a separate read and must never
  // block the shared list from rendering.
  const watchlist = useFetch(() => api.get('/monitoring/watchlist').catch(() => []))

  const [busyId, setBusyId] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [unwatching, setUnwatching] = useState(null)
  // FR-341: suppression is the admin's reversible removal from the shared list.
  const [suppressing, setSuppressing] = useState(null)
  const [suppressReason, setSuppressReason] = useState('')
  const [suppressBusy, setSuppressBusy] = useState(false)
  const [suppressedNotice, setSuppressedNotice] = useState(null)
  const [undoBusy, setUndoBusy] = useState(false)

  const watched = new Map((watchlist.data || []).map((w) => [w.company_id, w]))
  const total = data?.total ?? 0
  const rows = data?.items ?? []
  const items = rows.filter(
    (c) => (!stage || c.stage === stage) && (!trajectory || c.trajectory === trajectory),
  )
  const staleCount = items.filter((c) => c.stale).length
  const filtered = Boolean(
    filters.q ||
      filters.country ||
      filters.sector ||
      filters.size_band ||
      filters.ats_vendor ||
      stage ||
      trajectory,
  )
  // FR-345: how this inventory was actually built. A corpus can look healthy
  // at a thousand rows and still have been reached through one free job board,
  // with no company's own ATS board read at all - which is exactly the shape
  // that hides the well-known employers. The screen now says so.
  const facets = data?.facets ?? null

  function applySearch(e) {
    e.preventDefault()
    setOffset(0)
    setFilters((f) => ({
      ...f,
      q: draft.trim(),
      country: countryDraft.trim().toUpperCase(),
      sector: sectorDraft.trim(),
    }))
  }

  function setFilter(key, value) {
    setOffset(0)
    setFilters((f) => ({ ...f, [key]: value }))
  }

  function clearAll() {
    setDraft('')
    setCountryDraft('')
    setSectorDraft('')
    setStage('')
    setTrajectory('')
    setOffset(0)
    setFilters({ q: '', country: '', sector: '', size_band: '', ats_vendor: '' })
  }

  async function watch(company) {
    setBusyId(company.id)
    setActionError(null)
    try {
      await api.post('/monitoring/watchlist', { company_id: company.id })
      watchlist.reload()
    } catch (e) {
      setActionError(e)
    } finally {
      setBusyId(null)
    }
  }

  // Removing a watch stops the rechecks that feed notifications (FR-401), so
  // it confirms first like every other destructive action.
  async function unwatch() {
    const entry = unwatching?.entry
    setBusyId(unwatching?.company?.id ?? null)
    setActionError(null)
    try {
      await api.del(`/monitoring/watchlist/${entry.id}`)
      setUnwatching(null)
      watchlist.reload()
    } catch (e) {
      setActionError(e)
    } finally {
      setBusyId(null)
    }
  }

  /**
   * FR-341: a shared company is never deleted, only suppressed, and the row is
   * taken off the page on the server's confirmation rather than waiting for a
   * refetch that would only move the pagination under the reader.
   */
  async function suppress() {
    const company = suppressing
    if (!company || !suppressReason.trim()) return
    setSuppressBusy(true)
    setActionError(null)
    try {
      await api.post(`/companies/${company.id}/suppress`, {
        reason: suppressReason.trim(),
      })
      setData((d) =>
        d
          ? {
              ...d,
              items: (d.items || []).filter((row) => row.id !== company.id),
              total: Math.max(0, (d.total ?? 1) - 1),
            }
          : d,
      )
      setSuppressedNotice({ id: company.id, name: company.name })
      setSuppressing(null)
      setSuppressReason('')
    } catch (e) {
      setActionError(e)
    } finally {
      setSuppressBusy(false)
    }
  }

  async function undoSuppress() {
    const company = suppressedNotice
    if (!company) return
    setUndoBusy(true)
    setActionError(null)
    try {
      await api.del(`/companies/${company.id}/suppress`)
      setSuppressedNotice(null)
      reload()
    } catch (e) {
      setActionError(e)
    } finally {
      setUndoBusy(false)
    }
  }

  /* Why this list is empty — a different question from whether the knowledge
     base is. Saying "606 companies are on record" under a search that matched
     none of them would report the wrong problem. */
  function emptyReason() {
    if (total === 0) {
      return (
        'Nothing in the shared knowledge base matches this search. Try a broader one — the ' +
        'full-text index covers the business summary, products and markets, not only the name.'
      )
    }
    if (stage || trajectory) {
      return (
        `${total} ${total === 1 ? 'company matches' : 'companies match'} the search, but none of ` +
        'the rows on this page has the stage and trajectory you narrowed to. Those two filter ' +
        'this page only — clear them, or page through the rest.'
      )
    }
    return (
      `${total} ${total === 1 ? 'company is' : 'companies are'} on record for this search, but ` +
      'this page of results is empty. Go back a page.'
    )
  }

  return (
    <div className="content-wide">
      <WorkflowMap journey={{}} compact current="companies" />

      <ScreenIntro pathname="/companies" />

      {actionError && <ErrorBox error={actionError} />}

      {suppressedNotice && (
        <div className="alert alert-ok">
          <div style={{ flex: 1 }}>
            <strong>{suppressedNotice.name}</strong> is suppressed and hidden from this list
            and from every count.
          </div>
          <button className="btn btn-sm" disabled={undoBusy} onClick={undoSuppress}>
            {undoBusy ? <span className="spinner" /> : 'Undo'}
          </button>
        </div>
      )}

      {staleCount > 0 && (
        <Caution title={`${staleCount} of these profiles are past their freshness window`}>
          A stale profile is still shown, because old research beats none — but its financial
          figures, headcount and departments describe the company as it was when it was last
          collected. Open one and press “Refresh profile” before you rely on it (FR-226, FR-343).
        </Caution>
      )}

      <div className="card" style={{ marginBottom: 14 }}>
        <form className="row row-wrap" onSubmit={applySearch} style={{ alignItems: 'flex-end' }}>
          <div className="field" style={{ marginBottom: 0, minWidth: 260, flex: 1 }}>
            <label>Search the knowledge base</label>
            <input
              type="text"
              value={draft}
              placeholder="Name, summary, product, market…"
              onChange={(e) => setDraft(e.target.value)}
            />
          </div>

          <div className="field" style={{ marginBottom: 0, width: 110 }}>
            <label>Country</label>
            <input
              type="text"
              value={countryDraft}
              placeholder="BE"
              maxLength={2}
              onChange={(e) => setCountryDraft(e.target.value.toUpperCase())}
            />
          </div>

          <div className="field" style={{ marginBottom: 0, width: 150 }}>
            <label>
              Size band
              <HelpTip term="size_band" />
            </label>
            <select
              value={filters.size_band}
              onChange={(e) => setFilter('size_band', e.target.value)}
            >
              <option value="">Any size</option>
              {SIZE_BANDS.map((b) => (
                <option key={b} value={b}>
                  {b} staff
                </option>
              ))}
            </select>
          </div>

          <div className="field" style={{ marginBottom: 0, width: 150 }}>
            <label>
              Stage
              <HelpTip term="company_stage" />
            </label>
            <select value={stage} onChange={(e) => setStage(e.target.value)}>
              <option value="">Any stage</option>
              {STAGES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>

          <div className="field" style={{ marginBottom: 0, width: 160 }}>
            <label>
              Trajectory
              <HelpTip term="trajectory" />
            </label>
            <select value={trajectory} onChange={(e) => setTrajectory(e.target.value)}>
              <option value="">Any trajectory</option>
              {TRAJECTORIES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>

          <div className="field" style={{ marginBottom: 0, width: 140 }}>
            <label>Sector code</label>
            <input
              type="text"
              value={sectorDraft}
              placeholder="62.01 or NACE"
              onChange={(e) => setSectorDraft(e.target.value)}
            />
          </div>

          {/* The ATS board is the route to a company's own postings. Being able
              to ask "which of these did we reach that way" is what makes a
              starved harvest stage visible from the screen. */}
          <div className="field" style={{ marginBottom: 0, width: 170 }}>
            <label>
              ATS board
              <HelpTip title="ATS board">
                The applicant-tracking system a company publishes its own vacancies on. Reaching a
                company through its board is the freshest and most complete route to what it is
                hiring for; a company found only through an aggregator is known from one advert.
              </HelpTip>
            </label>
            <select
              value={filters.ats_vendor}
              onChange={(e) => setFilter('ats_vendor', e.target.value)}
            >
              <option value="">Any source</option>
              <option value="any">Has a board</option>
              <option value="none">No board known</option>
              {Object.keys(facets?.by_ats_vendor || {}).map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
          </div>

          <button className="btn btn-primary">Search</button>
          {filtered && (
            <button type="button" className="btn btn-ghost" onClick={clearAll}>
              Clear
            </button>
          )}
        </form>

        {(stage || trajectory) && (
          <p className="small muted" style={{ marginTop: 8 }}>
            Stage and trajectory narrow the {rows.length} rows on this page. Search, country, size
            band, sector and ATS board are applied across the whole knowledge base.
          </p>
        )}

        {/* FR-345: where this inventory came from. A thousand companies from one
            aggregator and none read from their own board is a collection
            problem, not a market: say it here rather than leaving it to be
            inferred from who is missing. */}
        {facets && facets.total > 0 && (
          <p className="small muted" style={{ marginTop: 8 }}>
            {facets.total} companies on record ·{' '}
            <strong>{facets.with_ats_board}</strong> reached through their own ATS board
            {facets.with_ats_board === 0 && (
              <>
                {' '}— none yet, so every company here is known from an aggregator advert. The ATS
                harvest runs last in a campaign; raise the page ceiling, or re-run collection.
              </>
            )}
            {Object.keys(facets.by_source || {}).length > 0 && (
              <>
                {' '}· mostly from{' '}
                {Object.entries(facets.by_source)
                  .slice(0, 3)
                  .map(([k, n]) => `${k} (${n})`)
                  .join(', ')}
              </>
            )}
          </p>
        )}
      </div>

      {loading && <Loading rows={6} />}
      {error && <ErrorBox error={error} onRetry={reload} />}

      {/* Three distinct states, and every one of them has to say something:
          rows, a filter that matched nothing, or a knowledge base that has
          never been filled. The empty screen is only the last of those, so it
          is the only one that teaches (FR-345). */}
      {!loading && !error && items.length === 0 && total === 0 && !filtered && (
        <FirstRun
          pathname="/companies"
          action={
            <Link className="btn btn-primary" to="/campaigns">
              Run a campaign to fill it
            </Link>
          }
        >
          The knowledge base is empty. Company profiles arrive when a campaign collects them —
          after that they are shared, reused while they stay fresh, and searchable here without
          picking a campaign.
        </FirstRun>
      )}

      {!loading && !error && items.length === 0 && (filtered || total > 0) && (
        <Empty title="No company matches these filters" action={
          <button className="btn" onClick={clearAll}>
            Show every company
          </button>
        }>
          {emptyReason()}
        </Empty>
      )}

      {!loading && !error && items.length > 0 && (
        <>
          <div className="row" style={{ marginBottom: 10 }}>
            <h3 style={{ margin: 0 }}>
              {total} {total === 1 ? 'company' : 'companies'}
            </h3>
            <span className="small muted">
              shared across every campaign
              <HelpTip term="knowledge_base_reuse" />
            </span>
            <div className="spacer" />
            <span className="small muted">
              {watched.size} on your watchlist
              <HelpTip title="Watchlist" align="right">
                A watched company is rechecked on an interval and anything new it posts becomes a
                notification and, where it fits your directives, an entry in your ranked list
                (FR-401). The watch is private to you; the company profile is not.
              </HelpTip>
            </span>
          </div>

          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Company</th>
                  <th>Country</th>
                  <th>Sector</th>
                  <th>
                    Size
                    <HelpTip term="size_band" />
                  </th>
                  <th>
                    Stage
                    <HelpTip term="company_stage" />
                  </th>
                  <th>
                    Trajectory
                    <HelpTip term="trajectory" />
                  </th>
                  <th>
                    Freshness
                    <HelpTip term="staleness" />
                  </th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {items.map((c) => {
                  const entry = watched.get(c.id)
                  return (
                    <tr key={c.id}>
                      <td>
                        <Link to={`/companies/${c.id}`} style={{ fontWeight: 600 }}>
                          {c.name}
                        </Link>
                        <div className="tiny muted">
                          {c.domain || 'no website on record'}
                          {c.business_summary ? ` · ${c.business_summary.slice(0, 90)}…` : ''}
                        </div>
                      </td>
                      <td className="small">{c.country || '–'}</td>
                      <td className="small">{sectorLabel(c.sector_codes)}</td>
                      <td className="small nowrap">
                        {c.size_band || '–'}
                        {c.size_fte ? <div className="tiny muted">{c.size_fte} FTE</div> : null}
                      </td>
                      <td className="small">{c.stage || '–'}</td>
                      <td>
                        {c.trajectory ? (
                          <Badge tone={TRAJECTORY_TONE[c.trajectory]}>{c.trajectory}</Badge>
                        ) : (
                          <span className="small muted">–</span>
                        )}
                      </td>
                      <td>
                        <Freshness row={c} />
                      </td>
                      <td className="nowrap">
                        <div className="row" style={{ gap: 6 }}>
                          {entry ? (
                            <button
                              className="btn btn-sm"
                              disabled={busyId === c.id}
                              onClick={() => setUnwatching({ company: c, entry })}
                            >
                              Watching
                            </button>
                          ) : (
                            <button
                              className="btn btn-sm"
                              disabled={busyId === c.id}
                              onClick={() => watch(c)}
                            >
                              Watch
                            </button>
                          )}
                          {isAdmin && (
                            <button
                              className="btn btn-sm btn-danger"
                              disabled={busyId === c.id}
                              title="Hide this shared company from browse, search and every count"
                              onClick={() => {
                                setSuppressReason('')
                                setSuppressing(c)
                              }}
                            >
                              Suppress
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          <div className="row" style={{ marginTop: 12 }}>
            <span className="small muted">
              {offset + 1}–{offset + rows.length} of {total}
            </span>
            <div className="spacer" />
            <button
              className="btn btn-sm"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              Previous
            </button>
            <button
              className="btn btn-sm"
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Next
            </button>
          </div>
        </>
      )}

      {unwatching && (
        <Modal
          title={`Stop watching ${unwatching.company.name}?`}
          onClose={() => setUnwatching(null)}
          actions={
            <>
              <button className="btn" onClick={() => setUnwatching(null)}>
                Keep watching
              </button>
              <button className="btn btn-danger" onClick={unwatch}>
                Stop watching
              </button>
            </>
          }
        >
          <p>
            The recheck cycle stops for this company: no more notifications when it posts a role,
            opens an office or files accounts, and nothing new is added to your ranked list from it
            (FR-401). The profile itself stays in the shared knowledge base.
          </p>
        </Modal>
      )}

      {suppressing && (
        <Modal
          title={`Suppress ${suppressing.name}?`}
          onClose={() => {
            if (suppressBusy) return
            setSuppressing(null)
          }}
          actions={
            <>
              <button
                className="btn"
                disabled={suppressBusy}
                onClick={() => setSuppressing(null)}
              >
                Cancel
              </button>
              <button
                className="btn btn-danger"
                disabled={suppressBusy || !suppressReason.trim()}
                onClick={suppress}
              >
                {suppressBusy ? <span className="spinner" /> : 'Suppress'}
              </button>
            </>
          }
        >
          <p>
            A company is shared knowledge (FR-341), so it is never hard-deleted. Suppressing
            hides it from search, browse and every count for everyone but an administrator,
            who can undo the decision from the company page. The profile itself stays.
          </p>
          <Field label="Why (kept in the audit trail)">
            <textarea
              value={suppressReason}
              autoFocus
              onChange={(e) => setSuppressReason(e.target.value)}
              placeholder="Scraped by mistake; duplicate of another record."
            />
          </Field>
        </Modal>
      )}
    </div>
  )
}
