/**
 * One campaign: plan review, knowledge-base saving, live dashboard, re-runs.
 *
 * The order on this screen is the order the specification puts them in, and it
 * is deliberate:
 *
 *   FR-163  nothing is fetched until the job seeker has seen every source, its
 *           native query, what it will cost and how long it will take, and has
 *           had the chance to exclude any of it.
 *   FR-342  before that decision, the knowledge base is compared against the
 *           plan and the saving is reported per entity type - what is already
 *           fresh is reused rather than collected again.
 *   FR-361  once it runs, progress, errors, token consumption and cost are
 *   NFR-502 visible per adapter, with pause, resume and cancel.
 *   NFR-603 any stage can be re-run on its own from what the previous stage
 *           persisted, without repeating the whole campaign.
 */

import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client'
import { Caution, ScreenIntro } from '../components/Help'
import Icon from '../components/Icon'
import WorkflowMap from '../components/WorkflowMap'
import { Badge, Empty, ErrorBox, Loading, Modal, Tabs, formatDuration, useFetch } from '../components/ui'

import LiveDashboard from './campaign/LiveDashboard'
import PlanReview from './campaign/PlanReview'
import StageRerun from './campaign/StageRerun'
import { STATUS_TONE, cost, num } from './campaign/shared'

/** FR-361: refresh cadence while collection is running; nothing polls when it is not. */
const POLL_MS = 4000

export default function CampaignDetailPage() {
  const { id } = useParams()

  const { data, error, loading, reload, setData } = useFetch(async () => {
    const [campaign, plan, live, stages, ack] = await Promise.all([
      api.get(`/campaigns/${id}`),
      api.get(`/campaigns/${id}/plan`),
      api.get(`/campaigns/${id}/status`),
      api.get('/campaigns/stages').catch(() => []),
      // CR-401 lives on the browser screen; this page only reads and records it.
      api.get('/browser/acknowledgement').catch(() => null),
    ])
    return { campaign, plan, live, stages: stages || [], ack }
  }, [id])

  const journey = useFetch(() => api.get('/overview/journey').catch(() => null))

  const [tab, setTab] = useState(null)
  const [busy, setBusy] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [confirm, setConfirm] = useState(null)
  const [planNotes, setPlanNotes] = useState(null)
  const [rerunResult, setRerunResult] = useState(null)

  const campaign = data?.campaign
  const plan = data?.plan
  const live = data?.live
  const items = plan?.items ?? []
  const status = live?.status ?? campaign?.status
  const running = status === 'running'

  // NFR-401: a job the backend marked "interrupted by restart" comes back as
  // `pending` with its checkpoint intact.  It is resumable, but it is not
  // `paused`, so without this it offered "Launch collection" — which starts a
  // fresh run and abandons the work already done.
  const job = live?.job ?? live?.jobs?.[0]
  const doneSoFar = Number(job?.progress_done ?? live?.progress_done ?? 0)
  const interrupted =
    !running && status !== 'paused' && doneSoFar > 0 &&
    ['pending', 'paused', 'running'].includes(String(job?.status ?? status))

  // FR-361: poll only while there is something moving, and stop the moment it
  // stops - a completed campaign that keeps polling is just wasted requests.
  useEffect(() => {
    if (!running) return undefined
    let alive = true
    const timer = setInterval(async () => {
      try {
        const next = await api.get(`/campaigns/${id}/status`)
        if (alive) setData((d) => (d ? { ...d, live: next } : d))
      } catch {
        // A dropped poll is not an error state; the next tick retries.
      }
    }, POLL_MS)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [id, running, setData])

  const active = tab || (status && status !== 'draft' && status !== 'planned' ? 'live' : 'plan')

  async function refresh() {
    const [nextCampaign, nextPlan, nextLive] = await Promise.all([
      api.get(`/campaigns/${id}`),
      api.get(`/campaigns/${id}/plan`),
      api.get(`/campaigns/${id}/status`),
    ])
    setData((d) => ({ ...d, campaign: nextCampaign, plan: nextPlan, live: nextLive }))
  }

  async function run(key, fn) {
    setBusy(key)
    setActionError(null)
    try {
      await fn()
    } catch (e) {
      setActionError(e)
    } finally {
      setBusy(null)
      setConfirm(null)
    }
  }

  const generatePlan = () =>
    run('plan', async () => {
      const summary = await api.post(`/campaigns/${id}/plan`, {
        use_llm: true,
        assess_knowledge_base: true,
      })
      // FR-164: the rejections and any degradation are part of the answer.
      setPlanNotes({
        rejected: summary.rejected_sources || [],
        degraded: summary.degraded_reason,
        llmUsed: summary.llm_used,
        countries: summary.countries || [],
      })
      await refresh()
      setTab('plan')
    })

  const patchItem = (itemId, body) =>
    run(itemId, async () => {
      await api.patch(`/campaigns/${id}/plan/${itemId}`, body)
      const nextPlan = await api.get(`/campaigns/${id}/plan`)
      setData((d) => ({ ...d, plan: nextPlan }))
    })

  const recheckReuse = () =>
    run('reuse', async () => {
      const report = await api.post(`/campaigns/${id}/plan/reuse`)
      setData((d) => ({ ...d, plan: { ...d.plan, reuse_report: report } }))
    })

  const launch = () =>
    run('launch', async () => {
      await api.post(`/campaigns/${id}/launch`)
      await refresh()
      setTab('live')
    })

  const control = (verb) => run(verb, async () => {
    await api.post(`/campaigns/${id}/${verb}`)
    await refresh()
  })

  const rerun = (stage) =>
    run(`stage:${stage}`, async () => {
      const result = await api.post(`/campaigns/${id}/stages/${stage}/rerun`, { options: {} })
      setRerunResult({ stage, result })
      await refresh()
    })

  if (loading) return <Loading rows={6} />
  if (error) return <ErrorBox error={error} onRetry={reload} />
  if (!campaign)
    return (
      <Empty
        title={
          <>
            <Icon name="campaign" /> Campaign not found
          </>
        }
      >
        It may have been deleted.
      </Empty>
    )

  // CR-401: a plan containing LinkedIn or any browser-driven source carries the
  // terms warning, because that is the source the user is about to run.
  const browserSources = items.filter(
    (i) => !i.excluded_by_user && (i.source_type === 'linkedin' || i.access_method === 'browser'),
  )
  const ack = data?.ack
  const budget = live?.budget || {}
  const ratio = budget.token_budget ? (budget.tokens_used || 0) / budget.token_budget : 0

  return (
    // Plan phase: the tabs, cards and stat tiles below all take its blue.
    <div className="content-wide phase-2">
      <WorkflowMap journey={journey.data?.journey || {}} compact current="collection" />

      <div className="row row-wrap" style={{ marginBottom: 10 }}>
        <Link className="btn btn-sm btn-ghost" to="/campaigns">
          ← All campaigns
        </Link>
        <h3 style={{ margin: 0, display: 'flex', alignItems: 'center', gap: 8 }}>
          <Icon name="campaign" className="phase-ink" />
          {campaign.name}
        </h3>
        <Badge tone={STATUS_TONE[status]}>{status}</Badge>
        {campaign.stage && <span className="small muted">stage: {campaign.stage}</span>}
        <div className="spacer" />
        <button className="btn btn-sm" onClick={generatePlan} disabled={busy === 'plan' || running}>
          {busy === 'plan' ? (
            <span className="spinner" />
          ) : (
            <>
              <Icon name="sparkle" /> {items.length ? 'Re-plan' : 'Generate plan'}
            </>
          )}
        </button>
        {interrupted && (
          <button
            className="btn btn-sm btn-primary"
            onClick={() => control('resume')}
            disabled={busy === 'resume'}
            title={`Continue from item ${doneSoFar}; nothing already collected is repeated`}
          >
            {busy === 'resume' ? <span className="spinner" /> : <Icon name="play" />}
            Resume collection
          </button>
        )}
        {!running && status !== 'paused' && !interrupted && (
          <button
            className="btn btn-sm btn-primary"
            onClick={() => setConfirm('launch')}
            disabled={!plan?.totals?.sources}
            title={plan?.totals?.sources ? undefined : 'Every source is excluded or already reused'}
          >
            <Icon name="play" /> Launch collection
          </button>
        )}
        {running && (
          <button className="btn btn-sm" onClick={() => control('pause')} disabled={busy === 'pause'}>
            <Icon name="pause" /> Pause
          </button>
        )}
        {status === 'paused' && (
          <button
            className="btn btn-sm btn-primary"
            onClick={() => control('resume')}
            disabled={busy === 'resume'}
          >
            <Icon name="play" /> Resume
          </button>
        )}
        {(running || status === 'paused') && (
          <button className="btn btn-sm btn-danger" onClick={() => setConfirm('cancel')}>
            <Icon name="stop" /> Cancel
          </button>
        )}
      </div>

      <ScreenIntro pathname="/campaigns" />

      {actionError && <ErrorBox error={actionError} />}

      {browserSources.length > 0 && (
        <Caution
          title="This plan drives a browser session on a site whose terms forbid automation"
          acknowledge="I understand and accept the risk"
          acknowledged={Boolean(ack?.acknowledged)}
          onAcknowledge={() =>
            run('ack', async () => {
              const next = await api.post('/browser/acknowledgement', { granted: true })
              setData((d) => ({ ...d, ack: next }))
            })
          }
        >
          {ack?.warning ||
            'LinkedIn’s user agreement prohibits automated access, including through a session you logged into yourself. Your account could be restricted.'}{' '}
          Affected sources: {browserSources.map((i) => i.display_name || i.adapter_key).join(', ')}.
          The session is one you open and log into yourself — see{' '}
          <Link to="/browser">Browser session</Link>.
        </Caution>
      )}

      {ratio >= 0.8 && (
        <Caution title="Close to the token budget">
          {num(budget.tokens_used)} of {num(budget.token_budget)} tokens are spent. Beyond this
          point the pipeline sheds optional work first — speculative openings for low-ranked
          companies — rather than stopping in the middle of the campaign (NFR-104).
        </Caution>
      )}

      <Tabs
        tabs={[
          {
            key: 'plan',
            label: (
              <>
                <Icon name="document" /> Plan review
              </>
            ),
            count: items.length || undefined,
          },
          {
            key: 'live',
            label: (
              <>
                <Icon name="chart" /> Live dashboard
              </>
            ),
          },
          {
            key: 'stages',
            label: (
              <>
                <Icon name="refresh" /> Re-run a stage
              </>
            ),
            count: data.stages.length || undefined,
          },
        ]}
        active={active}
        onChange={setTab}
      />

      {active === 'plan' && (
        <PlanReview
          plan={plan}
          notes={planNotes}
          busy={busy}
          running={running}
          onPatch={patchItem}
          onGenerate={generatePlan}
          onRecheckReuse={recheckReuse}
        />
      )}

      {active === 'live' && (
        <LiveDashboard
          // No `plan`: the dashboard used to join each source's estimate from
          // `/plan`, which returns one page of 100 items, so 6,424 rows of a
          // 6,524-item plan drew their bar against a hard-coded guess. The
          // estimate now comes off the source row itself (FR-186).
          live={live}
          campaign={campaign}
          busy={busy}
          onPause={() => control('pause')}
          onResume={() => control('resume')}
          onCancel={() => setConfirm('cancel')}
        />
      )}

      {active === 'stages' && (
        <StageRerun
          stages={data.stages}
          busy={busy}
          result={rerunResult}
          disabled={running}
          onRerun={(stage) => setConfirm({ stage })}
        />
      )}

      {/* Every outward-facing action confirms first: launching starts fetching
          real pages from real sites, and cancelling throws work away. */}
      {confirm === 'launch' && (
        <Modal
          title="Launch collection?"
          onClose={() => setConfirm(null)}
          actions={
            <>
              <button className="btn" onClick={() => setConfirm(null)}>
                Not yet
              </button>
              <button className="btn btn-primary" onClick={launch} disabled={busy === 'launch'}>
                {busy === 'launch' ? (
                  <span className="spinner" />
                ) : (
                  <>
                    <Icon name="play" /> Launch
                  </>
                )}
              </button>
            </>
          }
        >
          <p style={{ marginTop: 0 }}>
            {plan.totals.sources} sources, about {num(plan.totals.estimated_pages)} pages,{' '}
            {formatDuration(plan.totals.estimated_seconds)} and{' '}
            {cost(plan.totals.estimated_cost_eur)} estimated. Excluded sources and anything the
            knowledge base already covers are not fetched.
          </p>
          <p className="small muted">
            You can pause, resume or cancel at any point. A crash loses at most the page in flight —
            jobs resume from their last checkpoint (NFR-401).
          </p>
        </Modal>
      )}

      {confirm === 'cancel' && (
        <Modal
          title="Cancel this collection?"
          onClose={() => setConfirm(null)}
          actions={
            <>
              <button className="btn" onClick={() => setConfirm(null)}>
                Keep running
              </button>
              <button
                className="btn btn-danger"
                onClick={() => control('cancel')}
                disabled={busy === 'cancel'}
              >
                {busy === 'cancel' ? (
                  <span className="spinner" />
                ) : (
                  <>
                    <Icon name="stop" /> Cancel collection
                  </>
                )}
              </button>
            </>
          }
        >
          <p style={{ marginTop: 0 }}>
            Records already collected are kept and stay linked to their source plan item (FR-166).
            Sources that have not run yet stay planned, so you can launch again later.
          </p>
        </Modal>
      )}

      {confirm?.stage && (
        <Modal
          title={`Re-run “${confirm.stage}”?`}
          onClose={() => setConfirm(null)}
          actions={
            <>
              <button className="btn" onClick={() => setConfirm(null)}>
                Cancel
              </button>
              <button
                className="btn btn-primary"
                onClick={() => rerun(confirm.stage)}
                disabled={busy === `stage:${confirm.stage}`}
              >
                {busy === `stage:${confirm.stage}` ? (
                  <span className="spinner" />
                ) : (
                  <>
                    <Icon name="refresh" /> Re-run
                  </>
                )}
              </button>
            </>
          }
        >
          <p style={{ marginTop: 0 }}>
            This stage runs again from what the previous stage persisted (NFR-603). It replaces the
            artefacts that stage produced — a re-score overwrites scores, a re-synthesis overwrites
            opportunities — and it can consume tokens against this campaign&apos;s budget.
          </p>
        </Modal>
      )}
    </div>
  )
}
