/**
 * Hiring contacts and introduction routes (FR-301..306, FR-461, NFR-302, NFR-303).
 *
 * Two ways to reach a company, shown side by side because they are genuinely
 * alternatives rather than a fallback: a validated address for the right
 * person, or a warm introduction through someone who already knows them.
 *
 * The screen is deliberately blunt about data protection. Objections block an
 * address permanently and for everyone (NFR-302); contacts collected through
 * browser automation carry a retention deadline and are never shared
 * (NFR-303). Both are shown, not buried in settings.
 */

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import Icon from '../components/Icon'
import IntroductionRoutes from '../components/IntroductionRoutes'
import { Caution, FirstRun, HelpTip, ScreenIntro } from '../components/Help'
import {
  Badge,
  Empty,
  ErrorBox,
  JobProgress,
  Loading,
  Tabs,
  ValidationBadge,
  formatDate,
  useFetch,
} from '../components/ui'
import WorkflowMap from '../components/WorkflowMap'
import BrowseContacts from './contact/BrowseContacts'
import EmailCertaintyBadge from './contact/EmailCertaintyBadge'

// The company chooser asks for one page of the knowledge-base browser. 100 is
// the maximum GET /api/companies allows (FR-345); asking for more is answered
// 422 and leaves the chooser empty, so the ceiling is named rather than
// guessed, and a longer list is reported instead of silently truncated.
const COMPANY_CHOICES = 100

const TABS = [
  { key: 'contacts', label: 'Contacts' },
  { key: 'browse', label: 'Browse all' },
  { key: 'introductions', label: 'Introduction routes' },
  { key: 'network', label: 'My network' },
  { key: 'privacy', label: 'Objections & retention' },
]

export default function ContactsPage() {
  const [tab, setTab] = useState('contacts')
  const journey = useFetch(() => api.get('/overview/journey').catch(() => null), [])

  return (
    <div className="content-wide">
      {journey.data?.journey && (
        <WorkflowMap compact journey={journey.data.journey} current="contacts" />
      )}

      <ScreenIntro pathname="/contacts" />
      <Tabs tabs={TABS} active={tab} onChange={setTab} />

      {tab === 'contacts' && <ContactList />}
      {tab === 'browse' && <BrowseContacts />}
      {tab === 'introductions' && <IntroductionRoutes mode="opportunity" />}
      {tab === 'network' && <NetworkImport />}
      {tab === 'privacy' && <PrivacyPanel />}
    </div>
  )
}

/* --- Contacts -------------------------------------------------------------- */

/** How often the screen asks the server how a discovery pass is getting on. */
const DISCOVERY_POLL_MS = 4000
/** The default coverage target, in vacancies, for the whole-shortlist pass. */
const DISCOVERY_LIMIT = 500

function ContactList() {
  const [companyId, setCompanyId] = useState('')
  const companies = useFetch(() => api.get(`/companies?limit=${COMPANY_CHOICES}`), [])
  const contacts = useFetch(
    () => (companyId ? api.get(`/contacts/companies/${companyId}`) : Promise.resolve(null)),
    [companyId],
  )
  const coverage = useFetch(() => api.get('/contacts/coverage').catch(() => null), [])
  const [validating, setValidating] = useState(null)
  const [scraping, setScraping] = useState(false)
  const [scrapeResult, setScrapeResult] = useState(null)
  const [scrapeError, setScrapeError] = useState(null)
  const [batch, setBatch] = useState(null)
  const [batchError, setBatchError] = useState(null)
  const [target, setTarget] = useState(DISCOVERY_LIMIT)
  const [scope, setScope] = useState('shortlist')

  const list = Array.isArray(companies.data)
    ? companies.data
    : companies.data?.items || companies.data?.companies || []
  const total = Number.isFinite(companies.data?.total) ? companies.data.total : list.length

  async function validate(contact) {
    setValidating(contact.id)
    try {
      await api.post('/contacts/validate', { email: contact.email })
      contacts.reload()
    } finally {
      setValidating(null)
    }
  }

  /** FR-301 for one company: walk the ladder and show what it concluded. */
  async function findForCompany() {
    if (!companyId) return
    setScraping(true)
    setScrapeResult(null)
    setScrapeError(null)
    try {
      const result = await api.post(`/contacts/companies/${companyId}/discover`, {})
      setScrapeResult(result)
      contacts.reload()
      coverage.reload()
    } catch (error) {
      setScrapeError(error)
    } finally {
      setScraping(false)
    }
  }

  /**
   * FR-301 as a resumable background job (FR-185).
   *
   * `scope` decides what `limit` counts. On the shortlist the pass stops once
   * that many *vacancies* have somebody to write to; across the whole database
   * it instead visits that many *companies*. The distinction is carried into
   * the job so the progress and the closing report use the right unit.
   */
  async function findContacts() {
    setBatchError(null)
    const parsed = Math.round(Number(target))
    const limit = Number.isFinite(parsed)
      ? Math.max(1, Math.min(5000, parsed))
      : DISCOVERY_LIMIT
    setTarget(limit)
    try {
      const started = await api.post('/contacts/discover', { limit, scope })
      setBatch({
        job_id: started.job_id,
        job: null,
        done: false,
        reused: started.reused === true,
        startedAt: Date.now(),
        scope,
        limit,
      })
    } catch (error) {
      setBatchError(error)
    }
  }

  // A pass over many companies is a job, not a request. Poll it while it runs
  // and reload the list the moment it finishes (NFR-502).
  useEffect(() => {
    if (!batch?.job_id || batch.done) return undefined
    const timer = setInterval(async () => {
      try {
        const job = await api.get(`/contacts/discover/${batch.job_id}`)
        const finished = ['done', 'failed', 'cancelled'].includes(job.status)
        setBatch((current) => (current ? { ...current, job, done: finished } : current))
        if (finished) {
          contacts.reload()
          companies.reload()
          coverage.reload()
        }
      } catch (error) {
        setBatchError(error)
      }
    }, DISCOVERY_POLL_MS)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batch?.job_id, batch?.done])

  if (companies.loading) return <Loading />
  // A refused lookup is not an empty knowledge base: swallowing the failure
  // would show "no companies yet" to a job seeker whose companies are all
  // there, and no contact could ever be reached from this screen.
  if (companies.error) return <ErrorBox error={companies.error} onRetry={companies.reload} />

  const running = batch && !batch.done
  const covered = coverage.data?.coverage

  return (
    <>
      <div className="card">
        <div className="card-header">
          <Icon name="search" />
          <h3>Find contacts</h3>
          <div className="spacer" />
          {covered && (
            <span className="small muted">
              {covered.vacancies_with_contact} of {covered.vacancies} vacancies reachable
            </span>
          )}
        </div>
        <p className="small muted">
          Scraping walks each company&apos;s own website and press pages for a named hiring
          manager or a published careers mailbox, then validates every address it finds
          (FR-301, FR-303, FR-304). It is rate-limited per domain, so it runs as a
          background job you can leave.
        </p>
        <div className="row row-wrap" style={{ gap: 12, alignItems: 'flex-end' }}>
          <div className="field" style={{ marginBottom: 0, width: 170 }}>
            <label>
              How many to find
              <HelpTip title="How many to find">
                {scope === 'shortlist'
                  ? 'The pass stops once this many vacancies on your shortlist have somebody to write to.'
                  : 'The pass visits this many companies across the whole database, best-covered vacancies first.'}
              </HelpTip>
            </label>
            <input
              type="number"
              min={1}
              max={5000}
              value={target}
              disabled={running}
              onChange={(e) => setTarget(e.target.value)}
            />
          </div>
          <div className="field" style={{ marginBottom: 0, width: 240 }}>
            <label>Scope</label>
            <select
              value={scope}
              disabled={running}
              onChange={(e) => setScope(e.target.value)}
            >
              <option value="shortlist">My shortlist</option>
              <option value="all">All companies in the database</option>
            </select>
          </div>
          <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 240 }}>
            <label>&nbsp;</label>
            <div className="row" style={{ gap: 8 }}>
              <button className="btn btn-primary" disabled={running} onClick={findContacts}>
                {running ? <span className="spinner" /> : <Icon name="search" />}
                {scope === 'shortlist'
                  ? 'Find contacts for my shortlist'
                  : 'Find contacts for all companies'}
              </button>
              <HelpTip term="reachability" />
            </div>
          </div>
        </div>
        <p className="small muted" style={{ margin: '8px 0 0' }}>
          {scope === 'shortlist'
            ? `Looking for up to ${target || DISCOVERY_LIMIT} vacancies with a reachable contact on your shortlist.`
            : `Visiting up to ${target || DISCOVERY_LIMIT} companies across the whole database, best-covered vacancies first.`}
        </p>
        {batchError && <ErrorBox error={batchError} onRetry={() => setBatchError(null)} />}
        {batch && (
          <div style={{ marginTop: 12 }}>
            {batch.reused && !batch.done && (
              <p className="small muted" style={{ margin: '0 0 8px' }}>
                Continuing the run already in progress.
              </p>
            )}
            <JobProgress
              report={batch.job?.checkpoint?.report}
              job={
                batch.job
                  ? { ...batch.job, kind: 'Finding hiring contacts' }
                  : {
                      kind: 'Finding hiring contacts',
                      status: 'running',
                      progress_done: 0,
                      progress_total: 1,
                    }
              }
            />
            {batch.job?.checkpoint?.report && (
              <p className="small muted" style={{ margin: '8px 0 0' }}>
                {batch.scope === 'all' ? (
                  <>
                    {batch.job.checkpoint.report.companies_reachable || 0} companies reachable ·{' '}
                    {batch.job.checkpoint.report.companies_unreachable || 0} companies with
                    nothing found · {batch.job.checkpoint.report.vacancies_covered || 0}{' '}
                    vacancies newly covered
                  </>
                ) : (
                  <>
                    {batch.job.checkpoint.report.companies_reachable || 0} reachable ·{' '}
                    {batch.job.checkpoint.report.companies_unreachable || 0} nothing found ·{' '}
                    {batch.job.checkpoint.report.vacancies_covered || 0} vacancies newly covered
                  </>
                )}
              </p>
            )}
          </div>
        )}
      </div>

      {!list.length ? (
        <FirstRun
          pathname="/contacts"
          title="No companies to look through yet"
          action={
            <Link className="btn btn-primary" to="/campaigns">
              <Icon name="campaign" /> Run a campaign
            </Link>
          }
        >
          Contacts are found per company, so a campaign has to have collected some first.
        </FirstRun>
      ) : (
        <>
          <div className="card">
            <div className="row row-wrap">
              <div className="field" style={{ flex: 1, minWidth: 240, marginBottom: 0 }}>
                <label>
                  Company
                  <HelpTip term="reachability" />
                </label>
                <select
                  value={companyId}
                  onChange={(e) => {
                    setCompanyId(e.target.value)
                    setScrapeResult(null)
                    setScrapeError(null)
                  }}
                >
                  <option value="">Choose a company…</option>
                  {list.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>&nbsp;</label>
                <button className="btn" disabled={!companyId || scraping} onClick={findForCompany}>
                  {scraping ? <span className="spinner" /> : <Icon name="search" />} Find contacts
                  for this company
                </button>
              </div>
            </div>
            {total > list.length && (
              <p className="small muted" style={{ marginBottom: 0 }}>
                Showing the {list.length} most recently collected of {total} companies. Search
                the full knowledge base on <Link to="/companies">Companies</Link> if the one you
                want is not here.
              </p>
            )}
          </div>

          {scrapeError && <ErrorBox error={scrapeError} onRetry={() => setScrapeError(null)} />}
          {scrapeResult && <ScrapeOutcome result={scrapeResult} />}

          {!companyId && (
            <Empty title="Pick a company">
              Contacts are ranked per company: the hiring manager of the relevant department
              where one can be identified, then talent acquisition, then a generic careers
              mailbox.
            </Empty>
          )}

          {companyId && contacts.loading && <Loading rows={4} />}
          {contacts.error && <ErrorBox error={contacts.error} onRetry={contacts.reload} />}

          {companyId && contacts.data && (
            <div className="card">
              <div className="card-header">
                <Icon name="contacts" />
                <h3>Ranked contacts</h3>
                <div className="spacer" />
                <span className="small muted">
                  {(contacts.data.contacts || contacts.data || []).length} found
                </span>
              </div>

              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Role</th>
                      <th>
                        Email
                        <HelpTip term="catch_all" />
                      </th>
                      <th>
                        Validation
                        <HelpTip title="Validation result">
                          Syntax, MX lookup, disposable and role-address detection, an SMTP
                          probe where the server allows it, and catch-all detection. Addresses
                          that come back invalid are never used.
                        </HelpTip>
                      </th>
                      <th>
                        Source
                        <HelpTip title="How the address was found">
                          From the company website, a press page, inferred from the pattern of
                          other addresses on the same domain, or a lookup service. The method
                          is recorded because an inferred address deserves less confidence.
                        </HelpTip>
                      </th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {(contacts.data.contacts || contacts.data || []).map((c) => (
                      <tr key={c.id}>
                        <td>
                          <strong>{c.full_name || <span className="muted">Unnamed</span>}</strong>
                          {c.is_generic_mailbox === 1 && (
                            <>
                              {' '}
                              <Badge>
                                <Icon name="companies" /> role address
                              </Badge>
                              <HelpTip term="role_address" />
                            </>
                          )}
                          {c.objected === 1 && (
                            <>
                              {' '}
                              <Badge tone="danger">
                                <Icon name="lock" /> blocked
                              </Badge>
                            </>
                          )}
                        </td>
                        <td>
                          {c.role_title || '–'}
                          {c.department && <div className="tiny muted">{c.department}</div>}
                        </td>
                        <td className="mono">
                          {c.email || '–'}
                          <EmailCertaintyBadge contact={c} />
                        </td>
                        <td>
                          <ValidationBadge result={c.email_validation} />
                          {c.email_validated_at && (
                            <div className="tiny muted">{formatDate(c.email_validated_at)}</div>
                          )}
                        </td>
                        <td className="small muted">
                          {c.email_source_method || c.source || '–'}
                          {c.access_method === 'browser' && (
                            <div className="tiny">
                              <Badge tone="warn">
                                <Icon name="browser" /> browser
                              </Badge>
                            </div>
                          )}
                        </td>
                        <td>
                          <button
                            className="btn btn-sm"
                            disabled={!c.email || validating === c.id || c.objected === 1}
                            onClick={() => validate(c)}
                          >
                            {validating === c.id ? (
                              <span className="spinner" />
                            ) : (
                              <Icon name="refresh" />
                            )}
                            Re-check
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </>
  )
}

/* --- Company scrape outcome ----------------------------------------------- */

function ScrapeOutcome({ result }) {
  const outcome = result?.outcome || {}
  const reachable = outcome.status === 'reachable'
  return (
    <div className={`card phase-edge ${reachable ? 'phase-4' : 'phase-2'}`}>
      <div className="card-header">
        <Icon name={reachable ? 'check' : 'search'} />
        <h3>{result?.company_name || 'Contact search'}</h3>
        <div className="spacer" />
        <Badge tone={reachable ? 'ok' : 'warn'}>{outcome.status || 'unknown'}</Badge>
      </div>
      {reachable ? (
        <p className="small" style={{ marginBottom: 4 }}>
          <span className="mono">{outcome.email}</span>
          {outcome.is_generic_mailbox ? ' (role address)' : ''} via{' '}
          {outcome.email_source_method || 'the knowledge base'}
          {outcome.validation ? ` · ${outcome.validation}` : ''}
        </p>
      ) : (
        <p className="small muted" style={{ marginBottom: 4 }}>
          {outcome.reason || 'No contact could be found for this company.'}
        </p>
      )}
      {outcome.domain && (
        <p className="tiny muted" style={{ marginBottom: 0 }}>
          Domain: {outcome.domain}
          {outcome.domain_source ? ` (${outcome.domain_source})` : ''}
        </p>
      )}
    </div>
  )
}

/* --- Network --------------------------------------------------------------- */

function NetworkImport() {
  const network = useFetch(() => api.get('/contacts/network').catch(() => []), [])
  const members = Array.isArray(network.data) ? network.data : network.data?.members || []

  return (
    <>
      <p className="section-intro">
        Your own network is what makes a warm introduction possible. Import it once and it
        is matched against every target company from then on.
      </p>

      <Caution title="Your network stays yours.">
        Imported connections are private to your account and are never added to the shared
        knowledge base or offered to another job seeker.
      </Caution>

      <div className="card">
        <div className="card-header">
          <Icon name="contacts" />
          <h3>Imported connections</h3>
          <div className="spacer" />
          <span className="badge">{members.length}</span>
        </div>

        {network.loading && <Loading rows={3} />}
        {!network.loading && !members.length && (
          <Empty title="Nothing imported yet">
            LinkedIn lets you export your connections as CSV from Settings → Data privacy →
            Get a copy of your data. Upload that file and Dream Job will match it against
            target companies.
          </Empty>
        )}

        {members.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Company</th>
                  <th>Role</th>
                  <th>Relationship</th>
                </tr>
              </thead>
              <tbody>
                {members.slice(0, 100).map((m) => (
                  <tr key={m.id}>
                    <td>{m.full_name}</td>
                    <td>{m.company_name || '–'}</td>
                    <td className="small muted">{m.role_title || '–'}</td>
                    <td>
                      <Badge>{(m.relationship || 'connection').replace(/_/g, ' ')}</Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  )
}

/* --- Objections and retention ---------------------------------------------- */

function PrivacyPanel() {
  const objections = useFetch(() => api.get('/contacts/objections').catch(() => []), [])
  const due = useFetch(() => api.get('/contacts/retention/due').catch(() => []), [])
  const [email, setEmail] = useState('')
  const [busy, setBusy] = useState(false)
  const [sweepError, setSweepError] = useState(null)

  const list = Array.isArray(objections.data) ? objections.data : objections.data?.items || []
  const expiring = Array.isArray(due.data) ? due.data : due.data?.items || []

  async function block(e) {
    e.preventDefault()
    setBusy(true)
    try {
      await api.post('/contacts/objections', { email, reason: 'Entered by the job seeker' })
      setEmail('')
      objections.reload()
    } finally {
      setBusy(false)
    }
  }

  async function sweep() {
    // Irreversible deletion: ask first, and say so when it fails rather than
    // leaving a stray click looking like it worked.
    const confirmed = window.confirm(
      'Permanently delete every browser-collected contact whose retention date has ' +
        'passed? This cannot be undone.',
    )
    if (!confirmed) return
    setBusy(true)
    setSweepError(null)
    try {
      await api.post('/contacts/retention/sweep', {})
      due.reload()
    } catch (e) {
      setSweepError(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Caution title="An objection is permanent and applies to everyone.">
        A blocked address is never used again by any campaign or any job seeker on this
        installation. That is what makes the legitimate-interest basis for contacting
        someone defensible: they can say no, once, and it holds.
      </Caution>

      <div className="grid grid-2">
        <div className="card phase-edge phase-5">
          <div className="card-header">
            <Icon name="lock" />
            <h3>Blocked addresses</h3>
          </div>
          <form onSubmit={block} className="row" style={{ marginBottom: 14 }}>
            <input
              type="email"
              value={email}
              placeholder="someone@company.com"
              onChange={(e) => setEmail(e.target.value)}
              required
            />
            <button className="btn btn-danger" disabled={busy || !email}>
              <Icon name="x" /> Block
            </button>
          </form>

          {objections.loading && <Loading rows={2} />}
          {!objections.loading && !list.length && (
            <p className="small muted">No objections recorded.</p>
          )}
          {list.map((o) => (
            <div className="row small" key={o.id || o.email}>
              <Icon name="lock" />
              <span className="mono">{o.email}</span>
              <div className="spacer" />
              <span className="muted tiny">{formatDate(o.objected_at || o.created_at)}</span>
            </div>
          ))}
        </div>

        <div className="card phase-edge phase-4">
          <div className="card-header">
            <Icon name="clock" />
            <h3>Retention</h3>
            <div className="spacer" />
            <button className="btn btn-sm" onClick={sweep} disabled={busy}>
              <Icon name="trash" /> Delete what is due
            </button>
          </div>
          {sweepError && <ErrorBox error={sweepError} onRetry={() => setSweepError(null)} />}
          <p className="small muted">
            Contacts collected through browser automation are kept only for the campaign
            that collected them, plus a grace period, and are never shared between job
            seekers.
            <HelpTip title="Why these expire">
              LinkedIn's terms and the data-minimisation principle both point the same way:
              people data gathered through an authenticated session is scoped to the
              purpose that justified collecting it, and then removed.
            </HelpTip>
          </p>

          {due.loading && <Loading rows={2} />}
          {!due.loading && !expiring.length && (
            <p className="small muted">Nothing is due for deletion.</p>
          )}
          {expiring.map((c) => (
            <div className="row small" key={c.id}>
              <Icon name="clock" />
              <span>{c.full_name || c.email}</span>
              <div className="spacer" />
              <span className="muted tiny">due {formatDate(c.retention_until)}</span>
            </div>
          ))}
        </div>
      </div>
    </>
  )
}
