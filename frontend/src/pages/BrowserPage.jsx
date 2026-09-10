/**
 * Browser session — the control surface for automation that drives a browser
 * window you opened and logged into yourself (FR-201..208, CR-401).
 *
 * The screen is ordered the way the procedure is: read the terms warning and
 * decide (CR-401), get a browser attached and signed in (FR-201, FR-202),
 * see exactly which pages will be visited and how long it will take, and
 * confirm that (FR-204, FR-205), then watch it and keep control of it
 * (FR-206) — stopping the moment the site puts a challenge page in front of
 * us (FR-203).
 *
 * Nothing here asks for a password. The system attaches to a session you
 * created; it never holds a LinkedIn or Glassdoor credential (NFR-203).
 */

import { useEffect, useState } from 'react'

import { api } from '../api/client'
import { Caution, FirstRun, HelpTip, ScreenIntro } from '../components/Help'
import WorkflowMap from '../components/WorkflowMap'
import {
  Badge,
  ErrorBox,
  JobProgress,
  Loading,
  Modal,
  formatDate,
  formatDuration,
  useFetch,
} from '../components/ui'

const OS_LABELS = { macos: 'macOS', windows: 'Windows', linux: 'Linux' }
const LIVE = ['pending', 'running', 'paused']

/**
 * Several browser routes answer a conflict with a structured `detail` object
 * ({error, message, instructions}) rather than a string, so ApiError.message
 * degrades to "Request failed (409)". Read the object when there is one.
 */
function messageOf(err) {
  if (!err) return ''
  const d = err.detail
  if (d && typeof d === 'object') return d.message || d.error || err.message
  return err.message
}

function parsePayload(value) {
  if (!value) return null
  if (typeof value === 'object') return value
  try {
    return JSON.parse(value)
  } catch {
    return null
  }
}

/* --- Connection (FR-201, FR-202) ------------------------------------------ */

function ConnectionCard({ query, site, sites, onCheckLogin, login, loginError, loginBusy }) {
  const { data, error, loading, reload } = query
  return (
    <div className="card">
      <div className="card-header">
        <h3>
          Connection
          <HelpTip term="cdp" />
        </h3>
        <div className="spacer" />
        <button className="btn btn-sm" onClick={reload} disabled={loading}>
          {loading ? <span className="spinner" /> : 'Check connection'}
        </button>
      </div>

      {loading && <Loading rows={2} />}
      {error && <ErrorBox error={error} onRetry={reload} />}

      {!loading && !error && data && (
        <>
          <div className="row row-wrap" style={{ marginBottom: 10 }}>
            <Badge tone={data.connected ? 'ok' : 'danger'}>
              {data.connected ? 'Browser attached' : 'No browser attached'}
            </Badge>
            {data.browser && <span className="small muted">{data.browser}</span>}
            {data.connected && (
              <span className="small muted">
                {data.open_tabs} open tab{data.open_tabs === 1 ? '' : 's'}
              </span>
            )}
          </div>

          <p className="small muted" style={{ marginTop: 0 }}>
            {data.detail}
          </p>

          <div className="row row-wrap small" style={{ marginTop: 10 }}>
            <span className="muted">Debugging endpoint</span>
            <span className="mono">{data.cdp_url}</span>
            {data.protocol_version && (
              <span className="muted">· protocol {data.protocol_version}</span>
            )}
          </div>

          {/* FR-201: which sites this window already has open, by hostname only —
              the probe never reads a tab's contents or its cookies (NFR-203). */}
          {data.connected && (
            <div className="row row-wrap" style={{ marginTop: 10 }}>
              {sites.map((s) => (
                <Badge key={s.key} tone={data.sites?.[s.key] ? 'info' : undefined}>
                  {s.display_name}: {data.sites?.[s.key] ? 'tab open' : 'no tab'}
                </Badge>
              ))}
            </div>
          )}

          {/* FR-202: the sign-in check reads the rendered page in that window.
              It is a button, not a poll, because it navigates the user's tab. */}
          <div className="row row-wrap" style={{ marginTop: 14 }}>
            <button className="btn btn-sm" onClick={onCheckLogin} disabled={loginBusy}>
              {loginBusy ? <span className="spinner" /> : `Check sign-in on ${siteName(sites, site)}`}
            </button>
            {login && (
              <Badge tone={login.logged_in === true ? 'ok' : login.logged_in === false ? 'warn' : undefined}>
                {login.logged_in === true
                  ? 'Signed in'
                  : login.logged_in === false
                    ? 'Not signed in'
                    : 'Could not tell'}
              </Badge>
            )}
            {login?.detail && <span className="small muted">{login.detail}</span>}
          </div>
          {loginError && (
            <div className="alert alert-warn" style={{ marginTop: 10 }}>
              <div>{messageOf(loginError)}</div>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function siteName(sites, key) {
  return sites.find((s) => s.key === key)?.display_name || key
}

/* --- Launch instructions (FR-201) ----------------------------------------- */

function LaunchInstructions({ query, os, onOs }) {
  const { data, error, loading, reload } = query
  const [copied, setCopied] = useState(null)

  async function copy(key, text) {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(key)
      setTimeout(() => setCopied(null), 1800)
    } catch {
      setCopied('failed')
    }
  }

  const selected = os || data?.os

  return (
    <div className="card">
      <div className="card-header">
        <h3>Launch the browser</h3>
        <div className="spacer" />
        <div className="chips">
          {Object.entries(OS_LABELS).map(([key, label]) => (
            <span
              key={key}
              className={`chip clickable${selected === key ? ' on' : ''}`}
              onClick={() => onOs(key)}
            >
              {label}
            </span>
          ))}
        </div>
      </div>

      {loading && <Loading rows={4} />}
      {error && <ErrorBox error={error} onRetry={reload} />}

      {!loading && !error && data && (
        <>
          <div className="alert alert-info">
            <div>
              <strong>The short way.</strong> Run <span className="mono">scripts/browser.sh</span>{' '}
              from the project root. It finds your browser, opens the dedicated profile and the
              debugging port for you. The steps below are the same thing done by hand.
            </div>
          </div>

          <ol className="help-steps">
            {(data.steps || []).map((step) => (
              <li key={step.n}>
                <strong>{step.title}.</strong> {step.detail}
                {step.commands?.length > 0 && (
                  <div className="col" style={{ gap: 10, marginTop: 10 }}>
                    {/* The one per-machine part of every command below, and the
                        part that used to run off the right-hand edge of the
                        box. Shown first and separately copyable, so nothing a
                        person cannot retype from memory depends on the wrap. */}
                    <div>
                      <div className="cmd-caption">
                        <strong>Profile directory</strong>
                        <HelpTip title="Dedicated profile">
                          Automation uses its own profile directory, so your everyday browser, its
                          history and its logins are untouched. It also makes the automation window
                          visibly separate from the one you browse with.
                        </HelpTip>
                        <span>appears in every command below</span>
                      </div>
                      <div className="cmd cmd-profile">
                        <code>{data.profile_dir}</code>
                        {/* Every Copy button on this card reads the same word.
                            The visible label stays short; the accessible name
                            says which one, because a screen reader announces the
                            button without the caption above it. */}
                        <button
                          className="btn btn-sm"
                          aria-label="Copy the profile directory"
                          onClick={() => copy('profile', data.profile_dir)}
                        >
                          {copied === 'profile' ? 'Copied' : 'Copy'}
                        </button>
                      </div>
                    </div>

                    {step.commands.map((c) => (
                      <div key={c.key}>
                        <div className="cmd-caption">
                          <strong>{c.display_name}</strong>
                          <Badge tone={c.family === 'firefox' ? 'accent' : 'info'}>{c.family}</Badge>
                        </div>
                        <div className="cmd">
                          <code>{c.command}</code>
                          <button
                            className="btn btn-sm"
                            aria-label={`Copy the ${c.display_name} command`}
                            onClick={() => copy(c.key, c.command)}
                          >
                            {copied === c.key ? 'Copied' : 'Copy'}
                          </button>
                        </div>
                        {c.note && (
                          <p className="small muted" style={{ margin: '5px 0 0' }}>
                            {c.note}
                          </p>
                        )}
                      </div>
                    ))}

                    <p className="small muted" style={{ margin: 0 }}>
                      Run the line whole. A Chromium browser started without its{' '}
                      <span className="mono">--user-data-dir</span> refuses the debugging port and
                      opens an ordinary window instead — and the only sign of that here is
                      &lsquo;No browser attached&rsquo;.
                    </p>

                    {copied === 'failed' && (
                      <span className="small muted">
                        The clipboard is not available here — select the command and copy it by
                        hand.
                      </span>
                    )}
                  </div>
                )}
              </li>
            ))}
          </ol>
        </>
      )}
    </div>
  )
}

/* --- The run planner (FR-204, FR-205, FR-208) ----------------------------- */

function TargetTable({ targets, skipped = [], currentUrl, onSkip, skipping }) {
  if (!targets?.length) return null
  const skippedSet = new Set(skipped)
  return (
    <div className="table-wrap" style={{ marginTop: 12 }}>
      <table>
        <thead>
          <tr>
            <th>Page</th>
            <th>Kind</th>
            <th>Label</th>
            {onSkip && <th />}
          </tr>
        </thead>
        <tbody>
          {targets.map((t) => (
            <tr key={t.url}>
              <td className="mono" style={{ maxWidth: 420, wordBreak: 'break-all' }}>
                {t.url}
                {currentUrl === t.url && <Badge tone="accent">open now</Badge>}
              </td>
              <td>{t.kind}</td>
              <td className="muted">{t.label || '–'}</td>
              {onSkip && (
                <td className="nowrap">
                  {skippedSet.has(t.url) ? (
                    <Badge tone="warn">skipped</Badge>
                  ) : (
                    <button
                      className="btn btn-sm"
                      disabled={skipping}
                      onClick={() => onSkip(t.url)}
                    >
                      Skip
                    </button>
                  )}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function EstimateBlock({ estimate }) {
  if (!estimate) return null
  return (
    <div className="grid grid-4" style={{ marginTop: 12 }}>
      <div>
        <div className="small muted">
          Estimated duration
          <HelpTip term="duration_estimate" />
        </div>
        <div style={{ fontSize: 18, fontWeight: 650 }}>{estimate.human}</div>
        <div className="small muted">
          range {formatDuration(estimate.low_seconds)} – {formatDuration(estimate.high_seconds)}
        </div>
      </div>
      <div>
        <div className="small muted">Pages to visit</div>
        <div style={{ fontSize: 18, fontWeight: 650 }}>{estimate.targets}</div>
        <div className="small muted">{formatDuration(estimate.per_target_seconds)} each</div>
      </div>
      <div>
        <div className="small muted">
          Pace
          <HelpTip term="human_pace" />
        </div>
        <div style={{ fontSize: 18, fontWeight: 650 }}>
          {formatDuration(estimate.page_load_seconds)}
        </div>
        <div className="small muted">average page load</div>
      </div>
      <div>
        <div className="small muted">Based on</div>
        <div style={{ fontSize: 18, fontWeight: 650 }}>{estimate.basis}</div>
        <div className="small muted">
          {estimate.samples ? `${estimate.samples} measured pages` : 'no measurements yet'}
        </div>
      </div>
    </div>
  )
}

/* --- Challenge notice (FR-203) -------------------------------------------- */

function ChallengeNotice({ challenge, notifications }) {
  const rows = (notifications || []).map((n) => ({
    id: n.id,
    title: n.title,
    body: n.body,
    payload: parsePayload(n.payload),
    at: n.created_at,
  }))
  if (!challenge && !rows.length) return null

  return (
    <div className="alert alert-danger" style={{ marginBottom: 14 }}>
      <div>
        <strong>The site showed a verification page, so the run stopped.</strong>
        <p style={{ margin: '6px 0 0' }}>
          This is the intended behaviour, not a fault (FR-203). Nothing was retried and nothing was
          worked around. Open the automation window, complete whatever the site asks for by hand,
          and start a smaller run later.
        </p>
        {challenge && (
          <p className="small" style={{ margin: '8px 0 0' }}>
            {challenge.kind} · {challenge.evidence}
            {challenge.url ? ` · ${challenge.url}` : ''}
          </p>
        )}
        {rows.map((r) => (
          <p key={r.id} className="small" style={{ margin: '8px 0 0' }}>
            <strong>{r.title}</strong> {r.body}
            {r.payload?.kind ? ` (${r.payload.kind})` : ''} · {formatDate(r.at)}
          </p>
        ))}
      </div>
    </div>
  )
}

/* --- Page ----------------------------------------------------------------- */

export default function BrowserPage() {
  const [site, setSite] = useState('linkedin')
  const [os, setOs] = useState(null)
  const [driverKey, setDriverKey] = useState('cdp|chromium')
  const [campaignId, setCampaignId] = useState('')

  const sitesQ = useFetch(() => api.get('/browser/sites'), [])
  const statusQ = useFetch(() => api.get(`/browser/status?site=${site}`), [site])
  const ackQ = useFetch(() => api.get('/browser/acknowledgement'), [])
  const instrQ = useFetch(
    () => api.get(`/browser/instructions?site=${site}${os ? `&os=${os}` : ''}`),
    [site, os],
  )
  const driversQ = useFetch(() => api.get('/browser/drivers'), [])
  const pacingQ = useFetch(() => api.get('/browser/pacing'), [])
  const campaignsQ = useFetch(() => api.get('/campaigns'), [])
  const runsQ = useFetch(() => api.get('/browser/runs'), [])
  const journeyQ = useFetch(() => api.get('/overview/journey'), [])

  const [login, setLogin] = useState(null)
  const [loginError, setLoginError] = useState(null)
  const [loginBusy, setLoginBusy] = useState(false)

  const [ackBusy, setAckBusy] = useState(false)
  const [ackError, setAckError] = useState(null)

  const [plan, setPlan] = useState(null)
  const [planError, setPlanError] = useState(null)
  const [estimating, setEstimating] = useState(false)

  const [confirmOpen, setConfirmOpen] = useState(false)
  const [confirmChecked, setConfirmChecked] = useState(false)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState(null)

  const [activeRunId, setActiveRunId] = useState(null)
  const [run, setRun] = useState(null)
  const [runError, setRunError] = useState(null)
  const [challenges, setChallenges] = useState([])
  const [targetsByRun, setTargetsByRun] = useState({})
  const [controlNote, setControlNote] = useState(null)
  const [skipping, setSkipping] = useState(false)
  const [cancelOpen, setCancelOpen] = useState(false)

  const sites = sitesQ.data || []
  const siteInfo = sites.find((s) => s.key === site)
  const acknowledged = Boolean(ackQ.data?.acknowledged)
  // CR-401 is enforced by the backend for whichever site declares a consent
  // kind; the screen mirrors that rather than guessing.
  const needsAck = Boolean(siteInfo?.consent_kind)
  const [driver, family] = driverKey.split('|')

  /* A campaign preselects itself: most users have exactly one. */
  useEffect(() => {
    if (!campaignId && campaignsQ.data?.length) setCampaignId(campaignsQ.data[0].id)
  }, [campaignsQ.data, campaignId])

  /* An estimate belongs to one campaign and one site; changing either
     invalidates the figure the user would otherwise confirm (FR-204). */
  useEffect(() => {
    setPlan(null)
    setPlanError(null)
    setStartError(null)
  }, [campaignId, site])

  /* Adopt a run that is already in flight, so a page reload does not lose it. */
  useEffect(() => {
    if (activeRunId || !runsQ.data) return
    const live = runsQ.data.find((r) => LIVE.includes(r.status))
    if (live) setActiveRunId(live.id)
  }, [runsQ.data, activeRunId])

  /* NFR-502: a long-running operation is watched, not guessed at. Polling
     stops the moment the run leaves a live state. */
  useEffect(() => {
    if (!activeRunId) {
      setRun(null)
      return undefined
    }
    let live = true
    let timer = null
    const reloadRuns = runsQ.reload

    async function tick() {
      try {
        const r = await api.get(`/browser/runs/${activeRunId}`)
        if (!live) return
        setRun(r)
        setRunError(null)
        api
          .get(`/browser/runs/${activeRunId}/challenges`)
          .then((c) => live && setChallenges(Array.isArray(c) ? c : []))
          .catch(() => {})
        if (LIVE.includes(r.status)) timer = setTimeout(tick, 3000)
        else reloadRuns()
      } catch (e) {
        if (live) setRunError(e)
      }
    }
    tick()
    return () => {
      live = false
      if (timer) clearTimeout(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRunId])

  /* FR-205: the target list is closed. Recover it for a run adopted after a
     reload so "skip this one" still has something to point at. */
  useEffect(() => {
    if (!run?.job_id || !run.campaign_id || targetsByRun[run.job_id]) return undefined
    let live = true
    api
      .get(`/browser/campaigns/${run.campaign_id}/targets?site=${run.site}`)
      .then((d) => live && setTargetsByRun((m) => ({ ...m, [run.job_id]: d.targets || [] })))
      .catch(() => {})
    return () => {
      live = false
    }
  }, [run?.job_id, run?.campaign_id, run?.site, targetsByRun])

  async function checkLogin() {
    setLoginBusy(true)
    setLoginError(null)
    try {
      setLogin(await api.get(`/browser/login?site=${site}`))
    } catch (e) {
      setLogin(null)
      setLoginError(e)
    } finally {
      setLoginBusy(false)
    }
  }

  async function setAcknowledgement(granted) {
    setAckBusy(true)
    setAckError(null)
    try {
      const res = await api.post('/browser/acknowledgement', { granted })
      ackQ.setData(res)
    } catch (e) {
      setAckError(e)
    } finally {
      setAckBusy(false)
    }
  }

  async function estimate() {
    setEstimating(true)
    setPlanError(null)
    setStartError(null)
    try {
      setPlan(await api.post('/browser/runs/estimate', { campaign_id: campaignId, site }))
    } catch (e) {
      setPlan(null)
      setPlanError(e)
    } finally {
      setEstimating(false)
    }
  }

  async function startRun() {
    setStarting(true)
    setStartError(null)
    try {
      // FR-204: `confirmed` is the user's own confirmation of the announced
      // duration; the backend refuses the run without it.
      const res = await api.post('/browser/runs', {
        campaign_id: campaignId,
        site,
        driver,
        browser_family: family,
        confirmed: true,
      })
      setTargetsByRun((m) => ({ ...m, [res.job_id]: res.targets || [] }))
      setChallenges([])
      setControlNote(null)
      setActiveRunId(res.job_id)
      setConfirmOpen(false)
      setConfirmChecked(false)
      runsQ.reload()
    } catch (e) {
      setStartError(e)
    } finally {
      setStarting(false)
    }
  }

  async function control(action) {
    setControlNote(null)
    try {
      const res = await api.post(`/browser/runs/${activeRunId}/${action}`)
      setControlNote(res.detail || `${res.action}: ${res.applied ? 'applied' : 'not applied'}`)
      setRun(await api.get(`/browser/runs/${activeRunId}`))
    } catch (e) {
      setControlNote(messageOf(e))
    }
  }

  async function skipTarget(url) {
    setSkipping(true)
    setControlNote(null)
    try {
      const res = await api.post(`/browser/runs/${activeRunId}/skip`, { url })
      setControlNote(res.detail || (res.applied ? 'Skipped' : 'Not applied'))
      setRun(await api.get(`/browser/runs/${activeRunId}`))
    } catch (e) {
      setControlNote(messageOf(e))
    } finally {
      setSkipping(false)
    }
  }

  /* Everything standing between the user and a start, said plainly. */
  const blockers = []
  if (!campaignId) blockers.push('Choose a campaign.')
  if (needsAck && !acknowledged)
    blockers.push(`Acknowledge ${siteInfo?.display_name || 'the site'}'s terms first (CR-401).`)
  if (!plan) blockers.push('Ask for the duration estimate first (FR-204).')
  else if (!plan.target_count)
    blockers.push('This campaign plan holds no targets for this site, so there is nothing to run.')
  if (driver === 'cdp' && statusQ.data && !statusQ.data.connected)
    blockers.push('No browser is attached — follow the launch instructions above.')
  if (run && LIVE.includes(run.status)) blockers.push('A run is already in progress.')

  const runTargets = run ? targetsByRun[run.job_id] || [] : []
  const runIsLive = Boolean(run && LIVE.includes(run.status))
  const jobView = run && {
    // FR-206 controls live on JobProgress; run_status names its fields
    // slightly differently, so the mapping is explicit.
    kind: `Browser run · ${siteName(sites, run.site)}`,
    adapter_key: run.site,
    status: run.status,
    progress_done: run.progress_done,
    progress_total: run.progress_total,
    error_count: run.error_count,
    last_error: run.last_error,
    estimated_seconds: run.estimate?.remaining_seconds ?? run.estimate?.seconds,
  }

  return (
    <div className="col" style={{ gap: 14 }}>
      <ScreenIntro pathname="/browser" />

      {journeyQ.data?.journey && (
        <WorkflowMap journey={journeyQ.data.journey} compact current="collection" />
      )}

      {/* CR-401 — first thing on the screen, and the gate on everything below.
          The wording is the specification's own, served by the API. */}
      {ackQ.loading && <Loading rows={2} />}
      {ackQ.error && <ErrorBox error={ackQ.error} onRetry={ackQ.reload} />}
      {ackQ.data && (
        <div>
          <Caution
            title="LinkedIn prohibits automated access — this is your decision to make"
            acknowledge={ackBusy ? 'Recording…' : 'I understand the risk and accept it'}
            acknowledged={acknowledged}
            onAcknowledge={() => setAcknowledgement(true)}
          >
            <p style={{ margin: 0 }}>{ackQ.data.warning}</p>
            <p style={{ margin: '8px 0 0' }}>
              What is recorded: {ackQ.data.consent_text}
            </p>
            <p style={{ margin: '8px 0 0' }}>
              No browser run starts until you accept this, and you can withdraw it at any time.
            </p>
          </Caution>
          {acknowledged && (
            <div className="row small muted" style={{ marginTop: 6 }}>
              <span>Acknowledged {formatDate(ackQ.data.decided_at)}.</span>
              <button
                className="btn btn-sm btn-ghost"
                disabled={ackBusy || runIsLive}
                onClick={() => setAcknowledgement(false)}
              >
                Withdraw acknowledgement
              </button>
            </div>
          )}
          {ackError && <ErrorBox error={ackError} />}
        </div>
      )}

      {/* FR-203: a challenge stops everything, and says so calmly. */}
      <ChallengeNotice challenge={run?.report?.challenge} notifications={challenges} />

      <div className="grid grid-2">
        <ConnectionCard
          query={statusQ}
          site={site}
          sites={sites}
          login={login}
          loginError={loginError}
          loginBusy={loginBusy}
          onCheckLogin={checkLogin}
        />
        <LaunchInstructions query={instrQ} os={os} onOs={setOs} />
      </div>

      {/* --- Plan and confirm (FR-204, FR-205, FR-208) --- */}
      <div className="card">
        <div className="card-header">
          <h3>Plan a run</h3>
        </div>

        {campaignsQ.loading && <Loading rows={2} />}
        {campaignsQ.error && <ErrorBox error={campaignsQ.error} onRetry={campaignsQ.reload} />}

        {!campaignsQ.loading && !campaignsQ.error && !campaignsQ.data?.length && (
          <div className="alert alert-info">
            <div>
              A browser run walks a closed list of pages that a campaign plan produced. Create a
              campaign first — nothing here crawls on its own.
            </div>
          </div>
        )}

        {campaignsQ.data?.length > 0 && (
          <>
            <div className="grid grid-3">
              <div className="field">
                <label>Campaign</label>
                <select value={campaignId} onChange={(e) => setCampaignId(e.target.value)}>
                  {campaignsQ.data.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name} ({c.status})
                    </option>
                  ))}
                </select>
                <span className="hint">The plan decides which pages are allowed.</span>
              </div>

              <div className="field">
                <label>Site</label>
                <select value={site} onChange={(e) => setSite(e.target.value)}>
                  {sites.map((s) => (
                    <option key={s.key} value={s.key}>
                      {s.display_name}
                    </option>
                  ))}
                </select>
                <span className="hint">{siteInfo?.home_url}</span>
              </div>

              <div className="field">
                <label>
                  Driver
                  <HelpTip title="Driver (FR-208)">
                    Chromium-family browsers are driven over the DevTools Protocol on a window you
                    launched. Firefox has no such port, so Dream Job re-opens the dedicated profile
                    itself — sign in once in that profile first.
                  </HelpTip>
                </label>
                <select value={driverKey} onChange={(e) => setDriverKey(e.target.value)}>
                  {(driversQ.data?.options || []).map((o) => (
                    <option
                      key={`${o.driver}|${o.browser_family}`}
                      value={`${o.driver}|${o.browser_family}`}
                    >
                      {o.display_name}
                    </option>
                  ))}
                </select>
                <span className="hint">
                  {driver === 'cdp'
                    ? 'Attaches to the window you launched.'
                    : 'Dream Job opens the dedicated profile itself.'}
                </span>
              </div>
            </div>

            {siteInfo && !siteInfo.consent_kind && siteInfo.terms_warning && (
              <div className="alert alert-warn" style={{ marginBottom: 12 }}>
                <div>
                  <strong>{siteInfo.display_name}.</strong> {siteInfo.terms_warning}
                </div>
              </div>
            )}

            <div className="row row-wrap">
              <button className="btn" onClick={estimate} disabled={estimating || !campaignId}>
                {estimating ? <span className="spinner" /> : 'Estimate duration'}
              </button>
              <button
                className="btn btn-primary"
                disabled={blockers.length > 0}
                onClick={() => {
                  setConfirmChecked(false)
                  setConfirmOpen(true)
                }}
              >
                Start run…
              </button>
              {pacingQ.data && (
                <span className="small muted">
                  {pacingQ.data.min_delay_ms}–{pacingQ.data.max_delay_ms} ms between actions,{' '}
                  {pacingQ.data.waits_per_target} waits per page
                  <HelpTip term="human_pace" align="right" />
                </span>
              )}
            </div>

            {blockers.length > 0 && (
              <ul className="help-tips" style={{ marginTop: 10 }}>
                {blockers.map((b) => (
                  <li key={b}>{b}</li>
                ))}
              </ul>
            )}

            {planError && <ErrorBox error={planError} onRetry={estimate} />}
            {startError && (
              <div className="alert alert-danger" style={{ marginTop: 10 }}>
                <div>{messageOf(startError)}</div>
              </div>
            )}

            {plan && (
              <>
                <EstimateBlock estimate={plan.estimate} />
                <div className="row row-wrap small muted" style={{ marginTop: 12 }}>
                  <span>
                    {plan.target_count} page{plan.target_count === 1 ? '' : 's'} on the list
                    <HelpTip term="target_allowlist" />
                  </span>
                  {plan.caps && (
                    <span>
                      · caps: {plan.caps.max_profiles} profiles, {plan.caps.max_companies}{' '}
                      companies
                    </span>
                  )}
                  {plan.advisory_only && <Badge tone="warn">advisory data only</Badge>}
                </div>
                <TargetTable targets={plan.targets} />
              </>
            )}
          </>
        )}
      </div>

      {/* --- Live run (FR-206, FR-203, NFR-502) --- */}
      {run && (
        <div className="card">
          <div className="card-header">
            <h3>This run</h3>
            <div className="spacer" />
            {runIsLive && (
              <button className="btn btn-sm btn-ghost" onClick={() => setActiveRunId(null)}>
                Stop watching
              </button>
            )}
          </div>

          {runError && <ErrorBox error={runError} />}

          <JobProgress
            job={jobView}
            onPause={() => control('pause')}
            onResume={() => control('resume')}
            onCancel={() => setCancelOpen(true)}
          />

          <div className="row row-wrap small muted" style={{ marginTop: 10 }}>
            {run.estimate && (
              <span>
                Refined estimate
                <HelpTip term="duration_estimate" />: {formatDuration(run.estimate.remaining_seconds)}{' '}
                remaining of {formatDuration(run.estimate.seconds)} · {run.estimate.basis}
                {run.estimate.samples ? ` from ${run.estimate.samples} measured pages` : ''}
              </span>
            )}
          </div>

          {run.current_url && (
            <div className="row row-wrap small" style={{ marginTop: 8 }}>
              <span className="muted">Open now</span>
              <span className="mono" style={{ wordBreak: 'break-all' }}>
                {run.current_url}
              </span>
            </div>
          )}

          {run.report?.stopped_reason && (
            <div className="alert alert-warn" style={{ marginTop: 10 }}>
              <div>
                The run stopped early: {run.report.stopped_reason}
                <HelpTip term="challenge_page" />
              </div>
            </div>
          )}

          {controlNote && (
            <p className="small muted" style={{ marginTop: 8 }}>
              {controlNote}
            </p>
          )}

          {run.tally && (
            <div className="row row-wrap small muted" style={{ marginTop: 8 }}>
              <span>{run.tally.people} people</span>
              <span>· {run.tally.companies} companies</span>
              <span>· {run.tally.vacancies} vacancies</span>
              {run.tally.skipped_by_cap > 0 && (
                <span>· {run.tally.skipped_by_cap} left out by the campaign caps</span>
              )}
            </div>
          )}

          {/* FR-206: skip one target without stopping the run. */}
          <TargetTable
            targets={runTargets}
            skipped={run.skipped}
            currentUrl={run.current_url}
            onSkip={runIsLive ? skipTarget : undefined}
            skipping={skipping}
          />

          {run.outcomes?.length > 0 && (
            <div className="table-wrap" style={{ marginTop: 12 }}>
              <table>
                <thead>
                  <tr>
                    <th>Visited</th>
                    <th>Result</th>
                    <th className="num">Records</th>
                    <th className="num">Seconds</th>
                  </tr>
                </thead>
                <tbody>
                  {run.outcomes.map((o) => (
                    <tr key={o.url}>
                      <td className="mono" style={{ maxWidth: 420, wordBreak: 'break-all' }}>
                        {o.url}
                      </td>
                      <td>
                        <Badge
                          tone={
                            o.state === 'done'
                              ? 'ok'
                              : o.state === 'failed'
                                ? 'danger'
                                : o.state === 'blocked'
                                  ? 'danger'
                                  : 'warn'
                          }
                        >
                          {o.state}
                        </Badge>
                        {o.error && <span className="small muted"> {o.error}</span>}
                      </td>
                      <td className="num">{o.records}</td>
                      <td className="num">{o.seconds}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* --- History, and the first-run guidance that replaces it --- */}
      <div className="card">
        <div className="card-header">
          <h3>Earlier runs</h3>
          <div className="spacer" />
          <button className="btn btn-sm" onClick={runsQ.reload} disabled={runsQ.loading}>
            Refresh
          </button>
        </div>

        {runsQ.loading && <Loading rows={3} />}
        {runsQ.error && <ErrorBox error={runsQ.error} onRetry={runsQ.reload} />}

        {!runsQ.loading && !runsQ.error && !runsQ.data?.length && (
          <FirstRun
            pathname="/browser"
            action={
              <button className="btn btn-primary" onClick={statusQ.reload}>
                Check the browser connection
              </button>
            }
          />
        )}

        {!runsQ.loading && !runsQ.error && runsQ.data?.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Started</th>
                  <th>Site</th>
                  <th>Status</th>
                  <th className="num">Pages</th>
                  <th className="num">Errors</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {runsQ.data.map((r) => (
                  <tr key={r.id}>
                    <td>{formatDate(r.started_at || r.created_at)}</td>
                    <td>{siteName(sites, r.adapter_key)}</td>
                    <td>
                      <Badge
                        tone={
                          r.status === 'done'
                            ? 'ok'
                            : r.status === 'failed'
                              ? 'danger'
                              : LIVE.includes(r.status)
                                ? 'accent'
                                : undefined
                        }
                      >
                        {r.status}
                      </Badge>
                    </td>
                    <td className="num">
                      {r.progress_done}
                      {r.progress_total ? ` / ${r.progress_total}` : ''}
                    </td>
                    <td className="num">{r.error_count}</td>
                    <td className="nowrap">
                      <button className="btn btn-sm" onClick={() => setActiveRunId(r.id)}>
                        {activeRunId === r.id ? 'Watching' : 'Open'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* FR-204: nothing opens until the announced duration is confirmed. */}
      {confirmOpen && plan && (
        <Modal
          title="Confirm before anything opens"
          onClose={() => setConfirmOpen(false)}
          actions={
            <>
              <button className="btn" onClick={() => setConfirmOpen(false)}>
                Not now
              </button>
              <button
                className="btn btn-primary"
                disabled={!confirmChecked || starting}
                onClick={startRun}
              >
                {starting ? <span className="spinner" /> : 'Start the run'}
              </button>
            </>
          }
        >
          <p>
            Dream Job will open <strong>{plan.target_count}</strong> page
            {plan.target_count === 1 ? '' : 's'} on {siteName(sites, site)} in your browser window,
            one at a time, at human pace. It expects to take{' '}
            <strong>{plan.estimate?.human}</strong> (between{' '}
            {formatDuration(plan.estimate?.low_seconds)} and{' '}
            {formatDuration(plan.estimate?.high_seconds)}).
          </p>
          <p className="small muted">
            It visits only the pages on this list and derives no new ones from what it reads
            (FR-205). You can pause it, skip a page or cancel it at any moment, and it stops by
            itself at the first challenge or rate-limit page (FR-203, FR-206).
          </p>
          <label className="checkline" style={{ marginTop: 12 }}>
            <input
              type="checkbox"
              checked={confirmChecked}
              onChange={(e) => setConfirmChecked(e.target.checked)}
            />
            I have read the estimate and I confirm this run.
          </label>
          {startError && (
            <div className="alert alert-danger" style={{ marginTop: 12 }}>
              <div>{messageOf(startError)}</div>
            </div>
          )}
        </Modal>
      )}

      {/* House style: an outward-facing or destructive action confirms first. */}
      {cancelOpen && (
        <Modal
          title="Cancel this run?"
          onClose={() => setCancelOpen(false)}
          actions={
            <>
              <button className="btn" onClick={() => setCancelOpen(false)}>
                Keep running
              </button>
              <button
                className="btn btn-danger"
                onClick={() => {
                  setCancelOpen(false)
                  control('cancel')
                }}
              >
                Cancel the run
              </button>
            </>
          }
        >
          <p>
            The run stops before the next page. Everything already collected is kept, and you can
            start a new run over the remaining pages later.
          </p>
        </Modal>
      )}
    </div>
  )
}
