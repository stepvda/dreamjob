/**
 * One company, in the standardised layout FR-222 prescribes.
 *
 * The profile is shared knowledge (FR-341): everything on this screen except
 * the watch button and the values comparison is common to every job seeker,
 * and nothing here records that you looked (FR-344).
 *
 * The five-year financial analysis is the centrepiece, because it is what
 * turns "they seem to be doing well" into two scores that can be argued with
 * (FR-243, FR-244). It is advisory, and the screen says so rather than leaving
 * the number to speak for itself (NFR-305).
 */

import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client'
import { Caution, HelpTip, ScreenIntro } from '../components/Help'
import WorkflowMap from '../components/WorkflowMap'
import {
  Badge,
  Empty,
  ErrorBox,
  JobProgress,
  Loading,
  Modal,
  Tabs,
  formatDate,
  useFetch,
} from '../components/ui'
import CompanyFinancials from './company/CompanyFinancials'
import CompanyMarket from './company/CompanyMarket'
import CompanyProfileTab from './company/CompanyProfileTab'
import CompanyValues from './company/CompanyValues'
import { EmployerKindPanel } from './employers'

const TERMINAL = ['done', 'failed', 'cancelled']

const TRAJECTORY_TONE = {
  growing: 'ok',
  stable: 'info',
  declining: 'warn',
  restructuring: 'warn',
  volatile: 'warn',
}

export default function CompanyDetailPage() {
  const { id } = useParams()
  const [tab, setTab] = useState('profile')
  const [actionError, setActionError] = useState(null)
  const [confirmRefresh, setConfirmRefresh] = useState(false)
  const [force, setForce] = useState(false)
  const [reuseNote, setReuseNote] = useState(null)
  const [jobId, setJobId] = useState(null)
  const [job, setJob] = useState(null)
  const [unwatching, setUnwatching] = useState(false)
  const [busy, setBusy] = useState(false)

  const { data, error, loading, reload } = useFetch(() => api.get(`/companies/${id}`), [id])

  // Peers come from their own route because only that one says whether a peer
  // is already on your target list; the profile carries the shared view.
  const competitors = useFetch(
    () => api.get(`/companies/${id}/competitors`).catch(() => null),
    [id],
  )

  // FR-384 lives in the intelligence slice, is private, and is optional: a
  // seeker with no dream-job statement simply has no comparison.
  const values = useFetch(
    () => api.get(`/intelligence/values-match/${id}`).catch(() => null),
    [id],
  )

  const watchlist = useFetch(() => api.get('/monitoring/watchlist').catch(() => []), [id])
  const watchEntry = (watchlist.data || []).find((w) => w.company_id === id)

  // NFR-502: a refresh is a crawl, not a request, so it reports progress.
  useEffect(() => {
    if (!jobId) return undefined
    let live = true
    async function tick() {
      try {
        const row = await api.get(`/companies/refresh-jobs/${jobId}`)
        if (!live) return
        setJob(row)
        if (TERMINAL.includes(row.status)) {
          setJobId(null)
          reload()
          competitors.reload()
        }
      } catch (e) {
        if (!live) return
        setJobId(null)
        setActionError(e)
      }
    }
    tick()
    const timer = setInterval(tick, 3000)
    return () => {
      live = false
      clearInterval(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId])

  async function startRefresh() {
    setBusy(true)
    setActionError(null)
    setReuseNote(null)
    try {
      const res = await api.post(`/companies/${id}/refresh`, { force })
      setConfirmRefresh(false)
      if (res.started) {
        setJob({ id: res.job_id, kind: 'profiling', status: 'running', progress_done: 0 })
        setJobId(res.job_id)
      } else {
        // FR-226: inside the staleness window nothing is re-crawled, and the
        // screen says so instead of pretending to work.
        setReuseNote(res)
      }
    } catch (e) {
      setActionError(e)
    } finally {
      setBusy(false)
    }
  }

  async function watch() {
    setBusy(true)
    setActionError(null)
    try {
      await api.post('/monitoring/watchlist', { company_id: id })
      watchlist.reload()
    } catch (e) {
      setActionError(e)
    } finally {
      setBusy(false)
    }
  }

  async function unwatch() {
    setBusy(true)
    setActionError(null)
    try {
      await api.del(`/monitoring/watchlist/${watchEntry.id}`)
      setUnwatching(false)
      watchlist.reload()
    } catch (e) {
      setActionError(e)
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return (
      <div className="content-wide">
        <Loading rows={6} />
      </div>
    )
  }

  if (error) {
    return (
      <div className="content-wide">
        <ErrorBox error={error} onRetry={reload} />
        <Link className="btn btn-sm" to="/companies">
          Back to companies
        </Link>
      </div>
    )
  }

  if (!data) {
    return (
      <div className="content-wide">
        <Empty
          title="This company is not in the knowledge base"
          action={
            <Link className="btn btn-primary" to="/companies">
              Back to companies
            </Link>
          }
        >
          It may have been merged into another record during de-duplication (FR-184).
        </Empty>
      </div>
    )
  }

  const profile = data
  const identity = profile.identity || {}
  const size = profile.size || {}
  const freshness = profile.freshness || {}
  const financials = profile.financial_summary || {}
  const valuesMatch = values.data
  const signalCount = (profile.hiring_signals || []).length
  const peerCount = competitors.data?.count ?? (profile.competitors || []).length

  return (
    <div className="content-wide">
      <WorkflowMap journey={{}} compact current="companies" />

      <div className="row" style={{ marginBottom: 6 }}>
        <Link className="btn btn-sm btn-ghost" to="/companies">
          ← All companies
        </Link>
      </div>

      <ScreenIntro pathname="/companies" />

      {actionError && <ErrorBox error={actionError} />}

      <div className="card">
        <div className="row row-wrap">
          <div>
            <h2 style={{ margin: 0 }}>{profile.name}</h2>
            <div className="row row-wrap small muted" style={{ marginTop: 6 }}>
              {identity.domain && (
                <a href={`https://${identity.domain}`} target="_blank" rel="noreferrer">
                  {identity.domain}
                </a>
              )}
              {identity.country && <span>· {identity.country}</span>}
              {size.band && <span>· {size.band} staff</span>}
              {size.stage && <span>· {size.stage}</span>}
              {profile.overall_confidence != null && (
                <span>
                  · profile confidence {Math.round(profile.overall_confidence * 100)}%
                  <HelpTip term="provenance" />
                </span>
              )}
            </div>
          </div>

          <div className="spacer" />

          <div className="col" style={{ gap: 6, alignItems: 'flex-end' }}>
            <div className="row">
              {size.trajectory && (
                <Badge tone={TRAJECTORY_TONE[size.trajectory]}>{size.trajectory}</Badge>
              )}
              <Badge tone={freshness.stale ? 'warn' : 'ok'}>
                {freshness.stale ? 'stale' : 'fresh'}
              </Badge>
              <span className="small muted">
                {freshness.age_days != null ? `${Math.round(freshness.age_days)} d old` : 'age unknown'}
                <HelpTip term="staleness" align="right" />
              </span>
            </div>
            <div className="row">
              {watchEntry ? (
                <button className="btn btn-sm" disabled={busy} onClick={() => setUnwatching(true)}>
                  Watching
                </button>
              ) : (
                <button className="btn btn-sm" disabled={busy} onClick={watch}>
                  Watch this company
                </button>
              )}
              <button
                className="btn btn-sm btn-primary"
                disabled={busy || Boolean(jobId)}
                onClick={() => setConfirmRefresh(true)}
              >
                Refresh profile
              </button>
            </div>
            <span className="tiny muted">
              last collected {formatDate(freshness.refreshed_at || freshness.collected_at)}
              {freshness.access_method ? ` · via ${freshness.access_method}` : ''}
            </span>
          </div>
        </div>
      </div>

      {job && (
        <div style={{ marginTop: 12 }}>
          <JobProgress job={job} />
          <p className="tiny muted" style={{ marginTop: 4 }}>
            The crawl is paced deliberately and reads only this company's own pages. Signals and
            competitors are re-derived from what it finds (FR-221, FR-226).
          </p>
        </div>
      )}

      {reuseNote && (
        <div className="alert alert-info" style={{ marginTop: 12 }}>
          <div>
            Nothing was re-crawled: {reuseNote.reason} Last refreshed{' '}
            {formatDate(reuseNote.refreshed_at)}. Tick “force” to collect it again anyway.
          </div>
        </div>
      )}

      <div style={{ marginTop: 12 }} className="stack">
        {/* NFR-305: the scores rank and inform, they do not decide. */}
        <Caution title="The two financial scores are advisory">
          Ability to pay and investment capacity are computed from filed accounts and read by an
          AI. They estimate what a company <em>could</em> do, never what it will do or what it
          would offer you. Where a year is marked estimated (FR-245) or carries a reconciliation
          flag (NFR-404) the score rests on weaker ground — check the table before you quote a
          number in a negotiation.
        </Caution>

        {freshness.stale && (
          <Caution title="This profile is past its freshness window">
            Everything below describes the company as it was {Math.round(freshness.age_days || 0)}{' '}
            days ago. Financial years, headcount and departments in particular go out of date
            quietly. Refresh it before you write to anyone here (FR-226, FR-343).
          </Caution>
        )}

        {/* FR-384: a contradiction with something the seeker called essential
            is a warning on the profile, not a footnote in a document. */}
        {valuesMatch?.has_blocking_warning && (
          <Caution title="This company contradicts something you said matters">
            {(valuesMatch.warnings || [])
              .filter((w) => w.severity === 'blocking')
              .map((w) => w.cue)
              .join(', ')}
            . Open the “Values and culture” tab for the wording it was read from before you decide.
          </Caution>
        )}
      </div>

      {/* FR-143/NFR-402: whether this company is the employer at all, before
          any of the tabs below say anything about "the company". */}
      <EmployerKindPanel companyId={id} companyName={identity.name} />

      <div style={{ marginTop: 16 }}>
        <Tabs
          active={tab}
          onChange={setTab}
          tabs={[
            { key: 'profile', label: 'Profile' },
            { key: 'financials', label: 'Financials', count: (financials.years || []).length },
            { key: 'market', label: 'Market and timing', count: signalCount + peerCount },
            {
              key: 'values',
              label: 'Values and culture',
              count: valuesMatch?.counts?.warnings || undefined,
            },
          ]}
        />

        {tab === 'profile' && <CompanyProfileTab profile={profile} />}
        {tab === 'financials' && <CompanyFinancials financials={financials} />}
        {tab === 'market' && (
          <CompanyMarket
            companyId={id}
            profile={profile}
            competitors={competitors.data}
            onAdopted={() => {
              competitors.reload()
              watchlist.reload()
            }}
          />
        )}
        {tab === 'values' && <CompanyValues profile={profile} valuesMatch={valuesMatch} />}
      </div>

      {confirmRefresh && (
        <Modal
          title={`Refresh ${profile.name}?`}
          onClose={() => setConfirmRefresh(false)}
          actions={
            <>
              <button className="btn" onClick={() => setConfirmRefresh(false)}>
                Cancel
              </button>
              <button className="btn btn-primary" disabled={busy} onClick={startRefresh}>
                {busy ? <span className="spinner" /> : 'Start the crawl'}
              </button>
            </>
          }
        >
          <p>
            This opens pages on {identity.domain || 'the company website'} from your machine, at a
            deliberately human pace, and re-derives the profile, the hiring signals and the
            competitor suggestions from what it reads. Roughly a minute for a thirty-page crawl.
          </p>
          <label className="checkline">
            <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
            Collect again even though the profile is still fresh
          </label>
          <p className="small muted" style={{ marginTop: 8 }}>
            Without this, a profile inside its staleness window is reused rather than re-fetched —
            which is what makes the shared knowledge base cheaper than collecting per campaign
            (FR-226, FR-342).
          </p>
        </Modal>
      )}

      {unwatching && (
        <Modal
          title={`Stop watching ${profile.name}?`}
          onClose={() => setUnwatching(false)}
          actions={
            <>
              <button className="btn" onClick={() => setUnwatching(false)}>
                Keep watching
              </button>
              <button className="btn btn-danger" disabled={busy} onClick={unwatch}>
                Stop watching
              </button>
            </>
          }
        >
          <p>
            The rechecks stop: no notification when this company posts a role, and nothing more
            added to your ranked list from it (FR-401). The shared profile stays.
          </p>
        </Modal>
      )}
    </div>
  )
}
