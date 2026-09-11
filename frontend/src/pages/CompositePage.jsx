/**
 * Composite profile and the enrichment review queue (FR-121..FR-127).
 *
 * Two jobs on one screen.
 *
 * The first is traceability. Everything the AI later says about the job seeker
 * comes from this row, so FR-125 requires every statement to name the source
 * that supports it. The backend flattens the stored blocks into
 * `statements` - {id, block, text, source} - and lists the ids it could not
 * trace in `unsupported_statements`. Both are rendered here rather than
 * summarised, because a statement presented without its source is
 * indistinguishable from an invented one.
 *
 * The second is RK-02, the homonym risk: a stranger who shares your name
 * ending up in your CV. The defence is not a better classifier, it is showing
 * the job seeker the evidence. Each finding therefore lists every identity
 * signal that was scored - name variants, employer overlap, cross-links,
 * location, timeline, photo similarity - with its own score, weight and the
 * text that matched, so the decision can actually be made rather than trusted
 * (FR-123, FR-124).
 */

import { useEffect, useState } from 'react'

import { api } from '../api/client'
import { ScreenIntro } from '../components/Help'
import Icon from '../components/Icon'
import WorkflowMap, { deriveJourney } from '../components/WorkflowMap'
import ConsentNotice from '../components/ConsentNotice'
import { ErrorBox, JobProgress, Loading, Tabs, useFetch } from '../components/ui'
import CompositeProfile from './composite/CompositeProfile'
import EnrichmentSettings from './composite/EnrichmentSettings'
import FindingsQueue from './composite/FindingsQueue'

const absent = (e) => {
  if (e.status === 404) return null
  throw e
}

/* --- Page ----------------------------------------------------------------- */

export default function CompositePage() {
  const { data, error, loading, reload } = useFetch(async () => {
    const [composite, findings, settings, consent] = await Promise.all([
      // No composite yet is an empty screen, not a failure.
      api.get('/enrichment/composite').catch(absent),
      api.get('/enrichment/findings'),
      api.get('/enrichment/settings'),
      api.get('/auth/consent'),
    ])
    return { composite, findings, settings, consent }
  }, [])

  const [tab, setTab] = useState('profile')
  const [busy, setBusy] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [job, setJob] = useState(null)

  // NFR-502: an enrichment run is long-running, so its progress is polled and
  // shown rather than left as a spinner.
  useEffect(() => {
    if (!job || !['pending', 'running', 'paused'].includes(job.status)) return
    const t = setTimeout(async () => {
      try {
        const row = await api.get(`/enrichment/run/${job.id}`)
        setJob(row)
        if (!['pending', 'running', 'paused'].includes(row.status)) reload()
      } catch {
        /* a dropped poll is not worth an error banner; the next tick retries */
      }
    }, 2500)
    return () => clearTimeout(t)
    // `reload` is recreated on every render; depending on it would restart the
    // timer before it ever fires.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job])

  const findings = data?.findings ?? []
  const pending = findings.filter(
    (f) => f.status === 'pending' && f.classification !== 'confirmed',
  )

  async function run(key, fn) {
    setBusy(key)
    setActionError(null)
    try {
      await fn()
    } catch (e) {
      setActionError(e.message)
    } finally {
      setBusy(null)
    }
  }

  const llmConsent = data?.consent?.llm_transfer
  const enrichmentOn = data?.settings?.enabled

  if (loading) return <Loading rows={5} />
  if (error) return <ErrorBox error={error} onRetry={reload} />

  return (
    <div className="content-wide phase-1">
      <ScreenIntro pathname="/composite" />

      <WorkflowMap
        compact
        current="composite"
        journey={deriveJourney({ composite: data.composite, counts: {} })}
      />

      {/* CR-410: profile data is processed by DeepSeek, outside the EU. The
          composite cannot be synthesised until this consent is on record - the
          pipeline raises ConsentRequired at the moment of egress. The same
          shared notice as the profile screen renders it and records the one
          `llm_transfer` decision exactly once. */}
      <ConsentNotice
        consent={llmConsent}
        detail="Granted from the composite profile screen (CR-410)."
        onGranted={reload}
        style={{ marginTop: 14 }}
      />

      {actionError && (
        <div className="alert alert-danger" style={{ marginTop: 12 }}>
          <Icon name="error" />
          <div>{actionError}</div>
        </div>
      )}

      {job && (
        <div style={{ marginTop: 14 }}>
          <JobProgress job={job} />
        </div>
      )}

      <div style={{ marginTop: 18 }}>
        <Tabs
          active={tab}
          onChange={setTab}
          tabs={[
            {
              key: 'profile',
              label: (
                <>
                  <Icon name="composite" /> Composite profile
                </>
              ),
            },
            {
              key: 'findings',
              label: (
                <>
                  <Icon name="browser" /> Online findings
                </>
              ),
              count: pending.length || undefined,
            },
            {
              key: 'enrichment',
              label: (
                <>
                  <Icon name="sparkle" /> Enrichment
                </>
              ),
            },
          ]}
        />
      </div>

      {/* The composite blocks are read-only by default, so the two decisions
          that actually change the profile - ruling on online findings and
          switching enrichment on or off - are surfaced here rather than left
          only in their tabs. */}
      {tab === 'profile' && (
        <div className="card phase-1" style={{ marginTop: 12 }}>
          <div className="row row-wrap" style={{ gap: 10, alignItems: 'center' }}>
            <span className="icon-chip phase-chip">
              <Icon name={pending.length > 0 ? 'warning' : 'check'} />
            </span>
            <div style={{ flex: 1, minWidth: 220 }}>
              <strong>
                {pending.length > 0
                  ? `${pending.length} online finding${pending.length === 1 ? '' : 's'} await your decision`
                  : 'No online findings await a decision'}
              </strong>
              <div className="small muted">
                Confirming merges a page into the profile; rejecting hides it from every
                future run. Online enrichment is {enrichmentOn ? 'on' : 'off'}.
              </div>
            </div>
            <button className="btn btn-sm" onClick={() => setTab('findings')}>
              <Icon name="browser" /> Review findings
            </button>
            <button className="btn btn-sm btn-ghost" onClick={() => setTab('enrichment')}>
              <Icon name="sparkle" /> Enrichment settings
            </button>
          </div>
        </div>
      )}

      {tab === 'profile' && (
        <CompositeProfile
          composite={data.composite}
          findings={findings}
          busy={busy}
          onBuild={() =>
            run('build', async () => {
              await api.post('/enrichment/composite', {
                include_enrichment: enrichmentOn,
                language: 'en',
              })
              reload()
            })
          }
          onPatch={(patch) =>
            run('patch', async () => {
              await api.patch(`/enrichment/composite/${data.composite.id}`, patch)
              reload()
            })
          }
        />
      )}

      {tab === 'findings' && (
        <FindingsQueue
          findings={findings}
          enabled={enrichmentOn}
          busy={busy}
          onDecide={(id, decision, body) =>
            run(`finding:${id}`, async () => {
              await api.post(`/enrichment/findings/${id}/${decision}`, body)
              reload()
            })
          }
        />
      )}

      {tab === 'enrichment' && (
        <EnrichmentSettings
          settings={data.settings}
          consent={data.consent}
          busy={busy}
          running={Boolean(job && ['pending', 'running'].includes(job.status))}
          onToggle={(enabled) =>
            run('settings', async () => {
              await api.put('/enrichment/settings', {
                enabled,
                detail: enabled
                  ? 'Online enrichment switched on by the job seeker (FR-126).'
                  : 'Online enrichment switched off by the job seeker (FR-126).',
              })
              reload()
            })
          }
          onRun={() =>
            run('run', async () => {
              const started = await api.post('/enrichment/run', {
                max_queries: 6,
                max_pages: 12,
              })
              setJob(await api.get(`/enrichment/run/${started.job_id}`))
            })
          }
        />
      )}
    </div>
  )
}
