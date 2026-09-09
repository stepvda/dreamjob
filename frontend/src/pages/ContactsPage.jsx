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

import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import Icon from '../components/Icon'
import { Caution, FirstRun, HelpTip, ScreenIntro } from '../components/Help'
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  Modal,
  Tabs,
  ValidationBadge,
  formatDate,
  useFetch,
} from '../components/ui'
import WorkflowMap from '../components/WorkflowMap'

// The company chooser asks for one page of the knowledge-base browser. 100 is
// the maximum GET /api/companies allows (FR-345); asking for more is answered
// 422 and leaves the chooser empty, so the ceiling is named rather than
// guessed, and a longer list is reported instead of silently truncated.
const COMPANY_CHOICES = 100

const TABS = [
  { key: 'contacts', label: 'Contacts' },
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
      {tab === 'introductions' && <IntroductionRoutes />}
      {tab === 'network' && <NetworkImport />}
      {tab === 'privacy' && <PrivacyPanel />}
    </div>
  )
}

/* --- Contacts -------------------------------------------------------------- */

function ContactList() {
  const [companyId, setCompanyId] = useState('')
  const companies = useFetch(() => api.get(`/companies?limit=${COMPANY_CHOICES}`), [])
  const contacts = useFetch(
    () => (companyId ? api.get(`/contacts/companies/${companyId}`) : Promise.resolve(null)),
    [companyId],
  )
  const [validating, setValidating] = useState(null)

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

  if (companies.loading) return <Loading />
  // A refused lookup is not an empty knowledge base: swallowing the failure
  // would show "no companies yet" to a job seeker whose companies are all
  // there, and no contact could ever be reached from this screen.
  if (companies.error) return <ErrorBox error={companies.error} onRetry={companies.reload} />
  if (!list.length) {
    return (
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
    )
  }

  return (
    <>
      <div className="card">
        <div className="row">
          <div className="field" style={{ flex: 1, marginBottom: 0 }}>
            <label>
              Company
              <HelpTip term="reachability" />
            </label>
            <select value={companyId} onChange={(e) => setCompanyId(e.target.value)}>
              <option value="">Choose a company…</option>
              {list.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
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
                    <td className="mono">{c.email || '–'}</td>
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
                        {validating === c.id ? <span className="spinner" /> : <Icon name="refresh" />}
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
  )
}

/* --- Introduction routes --------------------------------------------------- */

function IntroductionRoutes() {
  const opportunities = useFetch(
    () => api.get('/opportunities?limit=100').catch(() => []),
    [],
  )
  const [oppId, setOppId] = useState('')
  const routes = useFetch(
    () =>
      oppId
        ? api.get(`/contacts/opportunities/${oppId}/introductions`)
        : Promise.resolve(null),
    [oppId],
  )
  const [draft, setDraft] = useState(null)

  const opps = Array.isArray(opportunities.data)
    ? opportunities.data
    : opportunities.data?.items || opportunities.data?.opportunities || []

  async function generate(path) {
    const res = await api.post(`/contacts/introductions/${path.id}/message`, {})
    setDraft({ path, text: res.message_draft || res.draft || res.message || '' })
  }

  return (
    <>
      <p className="section-intro">
        A warm introduction outperforms a cold email, so routes are ranked by the strength
        of the relationship. The message here goes to the <em>intermediary</em>, asking
        them to introduce you — it is not the application itself, which lives on the
        Applications screen.
      </p>

      <div className="card">
        <div className="field" style={{ marginBottom: 0 }}>
          <label>Opportunity</label>
          <select value={oppId} onChange={(e) => setOppId(e.target.value)}>
            <option value="">Choose an opportunity…</option>
            {opps.map((o) => (
              <option key={o.id} value={o.id}>
                {o.title} — {o.company_name || 'unknown company'}
              </option>
            ))}
          </select>
        </div>
      </div>

      {oppId && routes.loading && <Loading rows={3} />}
      {routes.error && <ErrorBox error={routes.error} onRetry={routes.reload} />}

      {oppId && routes.data && (routes.data.paths || routes.data || []).length === 0 && (
        <Empty title="No introduction route found">
          Nobody in your imported network is connected to this company. You can still apply
          directly — or import more of your network on the previous tab.
        </Empty>
      )}

      <div className="grid grid-2">
        {(routes.data?.paths || (Array.isArray(routes.data) ? routes.data : []) || []).map(
          (p) => (
            <div className="card phase-edge phase-4" key={p.id}>
              <div className="row" style={{ marginBottom: 8 }}>
                <span className="icon-chip phase-chip">
                  <Icon name="networking" />
                </span>
                <div>
                  <strong>{p.intermediary_name || 'Contact'}</strong>
                  <div className="tiny muted">{p.intermediary_role}</div>
                </div>
                <div className="spacer" />
                <Badge tone="accent">{p.relationship?.replace(/_/g, ' ')}</Badge>
              </div>
              <div className="row small muted" style={{ marginBottom: 10 }}>
                <span>Degree {p.degree ?? '–'}</span>
                <span>· strength {Math.round((p.strength ?? 0) * 100)}%</span>
                <span>· {p.status}</span>
              </div>
              <button className="btn btn-sm" onClick={() => generate(p)}>
                <Icon name="edit" /> Draft the request
              </button>
            </div>
          ),
        )}
      </div>

      {draft && (
        <Modal
          title={`Introduction request to ${draft.path.intermediary_name || 'your contact'}`}
          onClose={() => setDraft(null)}
          actions={
            <>
              <button className="btn" onClick={() => navigator.clipboard?.writeText(draft.text)}>
                <Icon name="copy" /> Copy
              </button>
              <button className="btn btn-primary" onClick={() => setDraft(null)}>
                Done
              </button>
            </>
          }
        >
          <p className="small muted">
            Send this yourself, from wherever you normally speak to this person. Dream Job
            does not send introduction requests on your behalf.
          </p>
          <textarea
            rows={12}
            value={draft.text}
            onChange={(e) => setDraft({ ...draft, text: e.target.value })}
          />
        </Modal>
      )}
    </>
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
    await api.post('/contacts/retention/sweep', {})
    due.reload()
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
            <button className="btn btn-sm" onClick={sweep}>
              <Icon name="trash" /> Delete what is due
            </button>
          </div>
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
