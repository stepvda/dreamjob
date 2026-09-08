/**
 * Applications: generate, review, approve (FR-321..324, FR-329..331, NFR-206).
 *
 * Master-detail, because the two questions a job seeker has here are different
 * questions: "which of these is ready?" is a list, and "is this one right?" is
 * a document. The list carries the state that decides the first - kind, status
 * and whether the checks passed - and the detail pane carries the five tabs
 * that answer the second.
 *
 * Nothing is approved from the list. Every approval, one or twenty, goes
 * through the same modal, which shows what will be sent and to whom before it
 * asks for a decision (FR-324). That is the whole point of this screen, so the
 * bulk path and the single path deliberately share one gate rather than two.
 */

import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { Caution, FirstRun, HelpTip, ScreenIntro } from '../components/Help'
import WorkflowMap from '../components/WorkflowMap'
import {
  Badge,
  ErrorBox,
  JobProgress,
  KindBadge,
  Loading,
  useFetch,
} from '../components/ui'

import BulkApprovalModal from './applications/BulkApprovalModal'
import PackageDetail from './applications/PackageDetail'
import {
  ConsistencyBadge,
  PACKAGE_STATUS_LABEL,
  PACKAGE_STATUS_TONE,
  canApprove,
  hardBlockers,
  photoBlocked,
} from './applications/shared'

/** The backend budgets 45 seconds per package when it estimates a batch. */
const SECONDS_PER_PACKAGE = 45
const POLL_MS = 4000

const FILTERS = [
  { key: 'all', label: 'All' },
  { key: 'draft', label: 'Draft' },
  { key: 'approved', label: 'Approved' },
  { key: 'sent', label: 'Sent' },
  { key: 'discarded', label: 'Discarded' },
]

export default function ApplicationsPage() {
  const { data, error, loading, reload } = useFetch(async () => {
    const [list, templates, profile] = await Promise.all([
      api.get('/applications/'),
      api.get('/applications/templates').catch(() => null),
      // FR-106: only the profile knows whether the photograph may be used.
      api.get('/profile/').catch(() => null),
    ])
    return { list, templates, profile }
  })

  // Orientation, not data the screen depends on: never block on it.
  const journey = useFetch(() => api.get('/overview/journey').catch(() => null))

  const [filter, setFilter] = useState('all')
  const [selectedId, setSelectedId] = useState(null)
  const [checked, setChecked] = useState([])
  const [approving, setApproving] = useState(null)
  const [gen, setGen] = useState(null)
  const [actionError, setActionError] = useState(null)

  const packages = data?.list?.packages || []
  const counts = data?.list?.counts || {}

  const visible = useMemo(
    () => (filter === 'all' ? packages : packages.filter((p) => p.status === filter)),
    [packages, filter],
  )
  const selected = packages.find((p) => p.id === selectedId) || visible[0] || null

  /* --- Generation (FR-321) ------------------------------------------------ */

  const madeSoFar = gen ? Math.max(0, packages.length - gen.base) : 0

  // The API starts the batch but exposes no route to read the job back, so the
  // watch is on its output: poll the list, and give up at half again the
  // backend's own estimate, since a package regenerated in place adds no row.
  useEffect(() => {
    if (!gen) return undefined
    const deadline = gen.startedAt + Math.max(60, gen.expected * SECONDS_PER_PACKAGE * 1.5) * 1000
    const timer = setInterval(() => {
      if (Date.now() > deadline) setGen(null)
      else reload()
    }, POLL_MS)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gen?.startedAt, gen?.expected])

  useEffect(() => {
    if (gen && madeSoFar >= gen.expected) setGen(null)
  }, [gen, madeSoFar])

  async function generate() {
    setActionError(null)
    try {
      const res = await api.post('/applications/generate', { selected_only: true, use_llm: true })
      if (res.mode === 'inline') {
        setSelectedId(res.package?.id || null)
        reload()
      } else {
        // NFR-502: a batch runs as a job; this screen watches it by its output.
        setGen({ expected: res.count, base: packages.length, startedAt: Date.now() })
      }
    } catch (e) {
      setActionError(e)
    }
  }

  /* --- Photograph state (FR-106) ------------------------------------------ */

  const photo = useMemo(() => {
    const profile = data?.profile
    if (!profile) return { known: false, included: false, reason: '' }
    if (photoBlocked(profile.do_not_disclose))
      return {
        known: true,
        included: false,
        reason: 'You marked the photograph "do not disclose", so it is left out of every generated document.',
      }
    if (!profile.photo_path)
      return {
        known: true,
        included: false,
        reason: 'Your profile has no photograph. Upload a CV that contains one to add it.',
      }
    return {
      known: true,
      included: true,
      reason: 'Taken from your profile. Both templates place it beside the name block.',
    }
  }, [data?.profile])

  /* --- Selection for bulk approval (FR-324) ------------------------------- */

  const approvable = visible.filter(canApprove)
  const checkedIds = checked.filter((id) => approvable.some((p) => p.id === id))

  function toggle(id) {
    setChecked((c) => (c.includes(id) ? c.filter((x) => x !== id) : [...c, id]))
  }

  const awaitingDispatch = packages.filter((p) => p.status === 'approved').length

  return (
    <div className="content-wide">
      <WorkflowMap journey={journey.data?.journey || {}} compact current="documents" />

      <ScreenIntro pathname="/applications" />

      {actionError && <ErrorBox error={actionError} />}
      {error && <ErrorBox error={error} onRetry={reload} />}

      {gen && (
        <div style={{ marginBottom: 14 }}>
          <JobProgress
            job={{
              kind: 'Generating application packages',
              status: 'running',
              progress_done: madeSoFar,
              progress_total: gen.expected,
              estimated_seconds: Math.max(0, (gen.expected - madeSoFar) * SECONDS_PER_PACKAGE),
            }}
          />
          <p className="small muted" style={{ margin: '6px 0 0' }}>
            Each package is four documents, so this takes a while. It continues on the server if you
            leave the screen; the list below fills in as packages are finished.
          </p>
        </div>
      )}

      {/* RK-05: approval is the decision to send; the caps are the next screen's. */}
      {awaitingDispatch > 0 && (
        <Caution title={`${awaitingDispatch} approved and waiting to be sent`}>
          Approving authorises dispatch but sends nothing by itself. The messages leave from your own
          mailbox on the <Link to="/mail">Mail screen</Link>, spread over the day and inside the
          daily sending cap, so a large batch goes out over several days rather than in one burst.
        </Caution>
      )}

      {/* A reload keeps the previous data, so only the first load blanks the screen -
          otherwise saving an email would throw the reader back to the top. */}
      {loading && !data && <Loading rows={5} />}

      {data && packages.length === 0 && (
        <FirstRun
          pathname="/applications"
          action={
            <div className="row row-wrap">
              <button className="btn btn-primary" onClick={generate}>
                Generate for my selected opportunities
              </button>
              <Link className="btn" to="/opportunities">
                Choose opportunities first
              </Link>
            </div>
          }
        >
          Nothing has been written yet. Packages are generated for the opportunities you marked as
          selected in the ranked list — four documents each: a tailored CV, a briefing, a motivation
          and fit document, and the introduction email.
        </FirstRun>
      )}

      {data && packages.length > 0 && (
        <div className="apl-layout">
          <div className="card apl-side">
            <div className="chips" style={{ marginBottom: 10 }}>
              {FILTERS.map((f) => (
                <span
                  key={f.key}
                  className={`chip clickable${filter === f.key ? ' on' : ''}`}
                  onClick={() => setFilter(f.key)}
                >
                  {f.label}
                  <span className="muted" style={{ marginLeft: 5 }}>
                    {f.key === 'all'
                      ? packages.length
                      : counts[f.key] ?? packages.filter((p) => p.status === f.key).length}
                  </span>
                </span>
              ))}
            </div>

            <div className="row row-wrap small" style={{ marginBottom: 8 }}>
              <button className="btn btn-sm" onClick={generate}>
                Generate more
              </button>
              <div className="spacer" />
              {approvable.length > 0 && (
                <button
                  className="btn btn-sm btn-ghost"
                  onClick={() =>
                    setChecked(
                      checkedIds.length === approvable.length ? [] : approvable.map((p) => p.id),
                    )
                  }
                >
                  {checkedIds.length === approvable.length ? 'Clear' : `Select all ${approvable.length}`}
                </button>
              )}
            </div>

            <div className="apl-list">
              {visible.length === 0 && (
                <p className="small muted" style={{ padding: '10px 2px' }}>
                  Nothing with this status.
                </p>
              )}
              {visible.map((p) => (
                <div
                  key={p.id}
                  className={`apl-row${p.id === selected?.id ? ' on' : ''}${
                    p.opportunity_kind === 'speculative' ? ' speculative' : ''
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={checked.includes(p.id)}
                    disabled={!canApprove(p)}
                    title={
                      canApprove(p)
                        ? 'Include in a bulk approval'
                        : hardBlockers(p).map((b) => b.detail).join(' ') || 'Already decided'
                    }
                    onChange={() => toggle(p.id)}
                  />
                  <button className="apl-row-main" onClick={() => setSelectedId(p.id)}>
                    <span className="apl-row-title">{p.opportunity_title || 'Untitled role'}</span>
                    <span className="small muted">{p.company_name || 'Unknown company'}</span>
                    <span className="row row-wrap" style={{ gap: 5, marginTop: 4 }}>
                      <Badge tone={PACKAGE_STATUS_TONE[p.status]}>
                        {PACKAGE_STATUS_LABEL[p.status] || p.status}
                      </Badge>
                      {p.consistency_status !== 'pass' && (
                        <ConsistencyBadge status={p.consistency_status} />
                      )}
                      {p.opportunity_kind === 'speculative' && (
                        <KindBadge kind={p.opportunity_kind} />
                      )}
                    </span>
                  </button>
                </div>
              ))}
            </div>

            {checkedIds.length > 0 && (
              <div className="apl-bulk">
                <span className="small">
                  {checkedIds.length} selected
                  <HelpTip term="bulk_approval_summary" />
                </span>
                <div className="spacer" />
                <button className="btn btn-primary btn-sm" onClick={() => setApproving(checkedIds)}>
                  Review and approve
                </button>
              </div>
            )}
          </div>

          <div>
            {selected ? (
              <PackageDetail
                key={selected.id}
                pkg={selected}
                templates={data?.templates}
                photo={photo}
                onChanged={reload}
                onApprove={setApproving}
              />
            ) : (
              <div className="card">
                <p className="muted">Choose an application on the left.</p>
              </div>
            )}
          </div>
        </div>
      )}

      {/* FR-324: one gate for every approval, single or bulk. */}
      {approving && (
        <BulkApprovalModal
          packageIds={approving}
          onClose={() => {
            setApproving(null)
            reload()
          }}
          onApproved={() => {
            setChecked([])
            reload()
          }}
        />
      )}
    </div>
  )
}
