/**
 * The three steps (FR-121..128, FR-161..166, FR-181..186, FR-321..325).
 *
 * The product has one promise: give it your profile, it finds and ranks the
 * market on its own, and you choose who to write to. This screen is that
 * promise made literal — three steps, one action each, with everything the
 * pipeline does between them running in the background and reporting itself
 * here.
 *
 * It is deliberately not the detailed journey map ("Where I am"): that shows the
 * fifteen internal stages and leaves the job seeker to work out which one they
 * are meant to move. This shows the three steps a person actually does — set up
 * the profile, choose among the opportunities, apply — and the only decisions on
 * it are "start", "choose", "apply". The map is the detail behind it, one click
 * away under Advanced.
 */

import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { api } from '../api/client'
import { Caution, HelpTip, ScreenIntro } from '../components/Help'
import Icon from '../components/Icon'
import {
  Badge,
  ErrorBox,
  Loading,
  Stat,
  formatDuration,
  useFetch,
} from '../components/ui'
import { useSession } from '../session'

export default function HomePage() {
  const { session } = useSession()
  const navigate = useNavigate()

  const journey = useFetch(() => api.get('/overview/journey').catch(() => null), [])
  const autopilot = useFetch(() => api.get('/autopilot/status').catch(() => null), [])
  const preflight = useFetch(() => api.get('/autopilot/preflight').catch(() => null), [])

  const [starting, setStarting] = useState(false)
  const [actionError, setActionError] = useState(null)

  const counts = journey.data?.counts || {}
  const run = autopilot.data?.run || null
  const ready = preflight.data?.ready
  const blockers = preflight.data?.blockers || []
  const warnings = preflight.data?.warnings || []

  // Poll only while a run is in flight.
  const running = run && ['pending', 'running', 'paused'].includes(run.status)
  useEffect(() => {
    if (!running) return
    const t = setInterval(() => {
      autopilot.reload()
      journey.reload()
    }, 3000)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running])

  // When a run finishes, refresh the counts so step 2 lights up.
  useEffect(() => {
    if (run && ['done', 'failed', 'cancelled'].includes(run.status)) {
      journey.reload()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.status])

  async function startSearch() {
    setStarting(true)
    setActionError(null)
    try {
      await api.post('/autopilot/start', {})
      autopilot.reload()
    } catch (err) {
      setActionError(err)
    } finally {
      setStarting(false)
    }
  }

  async function stopSearch() {
    try {
      await api.post(`/autopilot/${run.job_id}/cancel`, {})
      autopilot.reload()
    } catch (err) {
      setActionError(err)
    }
  }

  const hasOpportunities = (counts.opportunities || 0) > 0
  const hasPackages = (counts.packages || 0) > 0
  const hasSent = (counts.sent || 0) > 0

  return (
    <div className="content-narrow">
      <ScreenIntro pathname="/home" />

      {actionError && <ErrorBox error={actionError} />}

      {/* --- Step 1 ------------------------------------------------------- */}
      <Step
        n={1}
        title="Your profile"
        icon="profile"
        phase="phase-1"
        state={ready ? 'done' : 'current'}
        summary={
          ready
            ? 'Your profile, composite and dream job are in place.'
            : blockers[0]?.message || 'Import a LinkedIn export or a CV to begin.'
        }
        action={
          !ready ? (
            <Link className="btn btn-primary" to={blockers[0]?.screen || '/profile'}>
              <Icon name="upload" />
              Set up my profile
            </Link>
          ) : null
        }
      >
        {ready && warnings.length > 0 && !running && (
          <div className="alert alert-warn" style={{ marginBottom: 0 }}>
            <Icon name="info" />
            <div>
              {warnings[0].message}{' '}
              <Link to={warnings[0].screen}>Fix that</Link>.
            </div>
          </div>
        )}

        {ready && (
          <div className="row row-wrap" style={{ alignItems: 'center', gap: 10 }}>
            <button
              className="btn btn-primary btn-lg"
              disabled={starting || running}
              onClick={startSearch}
            >
              {starting ? <span className="spinner" /> : <Icon name="sparkle" />}
              {hasOpportunities ? 'Search again' : 'Find my opportunities'}
              <HelpTip title="What this does">
                Reads your profile to work out what to search for, picks the sources that fit,
                collects from them, then ranks everything it found — including roles a company
                has not advertised. It runs in the background; you can leave this page.
              </HelpTip>
            </button>
            {running && (
              <button className="btn" onClick={stopSearch}>
                <Icon name="stop" />
                Stop
              </button>
            )}
          </div>
        )}

        {running && <RunProgress run={run} />}
        {run && run.status === 'failed' && (
          <div className="alert alert-danger" style={{ marginBottom: 0 }}>
            <Icon name="error" />
            <div>
              The search did not finish. {run.last_error || 'See the campaign for details.'}{' '}
              {run.campaign_id && <Link to={`/campaigns/${run.campaign_id}`}>Open the campaign</Link>}
            </div>
          </div>
        )}
        {run && run.status === 'done' && <RunReport run={run} />}
      </Step>

      {/* --- Step 2 ------------------------------------------------------- */}
      <Step
        n={2}
        title="Opportunities"
        icon="opportunities"
        phase="phase-3"
        state={hasOpportunities ? 'current' : 'locked'}
        summary={
          hasOpportunities
            ? `${counts.opportunities} opportunit${counts.opportunities === 1 ? 'y' : 'ies'} ranked${
                counts.speculative ? `, ${counts.speculative} speculative` : ''
              }.`
            : 'Waiting for your search to find something.'
        }
        action={
          hasOpportunities ? (
            <Link className="btn btn-primary" to="/opportunities">
              <Icon name="check" />
              Choose what to pursue
            </Link>
          ) : null
        }
      >
        {hasOpportunities && (
          <div className="grid grid-4" style={{ marginTop: 4 }}>
            <Stat icon="opportunities" value={counts.opportunities} label="Found" phase="phase-3" />
            <Stat icon="speculative" value={counts.speculative || 0} label="Speculative" phase="phase-3" />
            <Stat icon="target" value={counts.scored || 0} label="Ranked" phase="phase-3" />
            <Stat icon="contacts" value={counts.contacts || 0} label="With a contact" phase="phase-4" />
          </div>
        )}
      </Step>

      {/* --- Step 3 ------------------------------------------------------- */}
      <Step
        n={3}
        title="Apply"
        icon="send"
        phase="phase-4"
        state={hasPackages ? 'current' : hasOpportunities ? 'available' : 'locked'}
        summary={
          hasSent
            ? `${counts.sent} application${counts.sent === 1 ? '' : 's'} sent.`
            : hasPackages
              ? `${counts.packages} application package${counts.packages === 1 ? '' : 's'} prepared.`
              : 'Tick who you want to write to and Dream Job prepares the rest.'
        }
        action={
          hasPackages ? (
            <Link className="btn btn-primary" to="/applications">
              <Icon name="eye" />
              Review and send
            </Link>
          ) : hasOpportunities ? (
            <Link className="btn" to="/opportunities">
              <Icon name="check" />
              Pick opportunities first
            </Link>
          ) : null
        }
      >
        {hasPackages && (
          <div className="grid grid-3" style={{ marginTop: 4 }}>
            <Stat icon="document" value={counts.packages} label="Prepared" phase="phase-4" />
            <Stat icon="send" value={counts.sent || 0} label="Sent" phase="phase-4" />
            <Stat icon="responses" value={counts.replies || 0} label="Responses" phase="phase-5" />
          </div>
        )}
        {hasPackages && (
          <Caution title="Nothing is sent for you">
            Dream Job writes the CV, the briefing, the motivation document and the email, and
            waits. Every message is yours to read, edit and send — it never goes out on its own.
          </Caution>
        )}
      </Step>

      <p className="small muted" style={{ marginTop: 18 }}>
        Want the controls behind all of this — directives, campaigns, sources, contacts,
        mail setup? They are all still there under Advanced.{' '}
        <Link to="/overview">Open Where I am</Link> for the detailed journey map.
      </p>
    </div>
  )
}

/* --- One step -------------------------------------------------------------- */

function Step({ n, title, icon, phase, state, summary, action, children }) {
  const locked = state === 'locked'
  return (
    <section className={`step-card ${phase} step-${state}`}>
      <div className="step-head">
        <span className={`step-index ${locked ? '' : phase}`}>{n}</span>
        <span className="step-icon">
          <Icon name={icon} />
        </span>
        <div style={{ flex: 1 }}>
          <h3 className="step-title">
            {title}
            {locked && (
              <Badge>
                <Icon name="lock" size={11} /> waiting
              </Badge>
            )}
            {state === 'done' && (
              <Badge tone="ok">
                <Icon name="check" size={11} /> done
              </Badge>
            )}
          </h3>
          <p className="step-summary">{summary}</p>
        </div>
        {action}
      </div>
      {children && <div className="step-body">{children}</div>}
    </section>
  )
}

/* --- Autopilot progress ---------------------------------------------------- */

const STAGE_LABELS = {
  composite: 'Reading your profile',
  dream_job: 'Understanding what you want',
  directives: 'Deciding what to search for',
  campaign: 'Setting up the search',
  plan: 'Choosing which sources to use',
  collection: 'Collecting and ranking opportunities',
  profiling: 'Looking into the companies',
  notify: 'Finishing up',
}

function RunProgress({ run }) {
  const total = run.total_steps || 8
  const done = run.stage_index || 0
  const pct = Math.min(100, Math.round((done / total) * 100))
  const label = STAGE_LABELS[run.stage] || 'Working'

  return (
    <div className="card" style={{ marginBottom: 0 }}>
      <div className="row" style={{ marginBottom: 8 }}>
        <span className="spinner" />
        <strong>{label}</strong>
        <div className="spacer" />
        <span className="small muted">
          step {Math.min(done + 1, total)} of {total}
        </span>
      </div>
      <div className="progress-track">
        <div className="progress-fill" style={{ width: `${pct}%` }} />
      </div>
      <p className="small muted" style={{ margin: '8px 0 0' }}>
        This keeps running if you close the page. It finishes in a few minutes for a normal
        search; browser-assisted sources take longer.
      </p>
    </div>
  )
}

function RunReport({ run }) {
  const report = run.report || {}
  const counts = report.counts || {}
  const sources = report.plan?.sources
  const profiled = report.profiling?.profiles?.built

  const facts = [
    sources != null && `${sources} sources searched`,
    counts.opportunities != null && `${counts.opportunities} opportunities ranked`,
    counts.speculative ? `${counts.speculative} speculative` : null,
    profiled ? `${profiled} company profiles built` : null,
  ].filter(Boolean)

  if (!facts.length) return null

  return (
    <div className="alert alert-ok" style={{ marginBottom: 0 }}>
      <Icon name="success" />
      <div>
        <strong>Done.</strong> {facts.join(' · ')}.
      </div>
    </div>
  )
}
