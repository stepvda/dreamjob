/**
 * FR-461: introduction routes into one target company.
 *
 * The API merges the routes across every open role at the company and keeps
 * the best rank each intermediary achieved, so what arrives here is one list
 * of people rather than one list per vacancy. Each row carries the two halves
 * of the rank separately - strength (would they help) and relevance (are they
 * close to the decision) - because a single number hides which of the two is
 * carrying it, and those call for very different messages.
 *
 * The drafted message is editable in place. It is *not* saved back: the
 * networking API has no endpoint that stores an edited draft, so the screen
 * says so rather than pretending an edit persisted.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, Empty, ErrorBox, Loading, Meter, Modal, useFetch } from '../../components/ui'

/** FR-461 names the relationship kinds the ranking recognises. */
const RELATIONSHIP = {
  former_colleague: { label: 'Former colleague', tone: 'ok' },
  first_degree: { label: 'First-degree connection', tone: 'ok' },
  alumni_employer: { label: 'Alumnus of the same employer', tone: 'accent' },
  alumni_school: { label: 'Alumnus of the same school', tone: 'accent' },
  community: { label: 'Shared community', tone: 'info' },
  second_degree: { label: 'Second-degree connection', tone: undefined },
}

/** Where a route has got to. Free text on the API; these are the useful ones. */
const STATUSES = ['proposed', 'asked', 'agreed', 'introduced', 'declined']

export default function IntroductionRoutes({ campaignId, companies }) {
  const [companyId, setCompanyId] = useState(companies[0]?.company_id || '')
  const [actionError, setActionError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [drafting, setDrafting] = useState(null)
  const [confirmDraft, setConfirmDraft] = useState(null)

  const company = companies.find((c) => c.company_id === companyId) || companies[0]

  const routes = useFetch(
    () =>
      companyId
        ? api
            .get(
              `/networking/introductions/company/${encodeURIComponent(companyId)}?limit=15${
                campaignId ? `&campaign_id=${encodeURIComponent(campaignId)}` : ''
              }`,
            )
            // A company with no opportunity in scope answers 404. That is an
            // empty result for this screen, not a failure.
            .catch((err) => (err.status === 404 ? { routes: [], count: 0, missing: true } : Promise.reject(err)))
        : Promise.resolve(null),
    [companyId, campaignId],
  )

  const data = routes.data
  const rows = data?.routes || []

  /** FR-461: rank the routes and draft the message asking for an introduction. */
  async function draft(opportunityId) {
    setConfirmDraft(null)
    setActionError(null)
    setNotice(null)
    setDrafting(opportunityId)
    try {
      const res = await api.post('/networking/introductions', {
        opportunity_id: opportunityId,
        use_llm: true,
        limit: 15,
      })
      setNotice(
        `${res.count} route${res.count === 1 ? '' : 's'} ranked and drafted. Nothing has been sent — each message is yours to read, edit and send.`,
      )
      routes.reload()
    } catch (err) {
      setActionError(err)
    } finally {
      setDrafting(null)
    }
  }

  async function setStatus(pathId, status) {
    setActionError(null)
    try {
      await api.post(`/networking/introductions/${pathId}/status`, { status })
      routes.reload()
    } catch (err) {
      setActionError(err)
    }
  }

  return (
    <>
      <div className="card">
        <div className="row row-wrap">
          <label className="small muted" style={{ display: 'flex', alignItems: 'center' }}>
            Target company
            <HelpTip term="introduction_route" />
          </label>
          <select
            value={companyId}
            onChange={(e) => setCompanyId(e.target.value)}
            style={{ maxWidth: 380 }}
          >
            {companies.map((c) => (
              <option key={c.company_id} value={c.company_id}>
                {c.company_name} ({c.roles.length} {c.roles.length === 1 ? 'role' : 'roles'})
              </option>
            ))}
          </select>
          {company && (
            <Link className="btn btn-sm" to={`/companies/${company.company_id}`}>
              <Icon name="companies" /> Company profile
            </Link>
          )}
        </div>

        {company && (
          <div className="row row-wrap" style={{ marginTop: 12 }}>
            <span className="small muted" style={{ display: 'flex', alignItems: 'center' }}>
              Draft for a role
              <HelpTip title="Why a role, and not just the company">
                The message has to name the job you would be introduced for, so the draft is
                built per role. The ranking itself is per company: the same person can be the
                best route into two of its vacancies.
              </HelpTip>
            </span>
            {company.roles.map((role) => (
              <button
                key={role.id}
                className="btn btn-sm"
                disabled={drafting != null}
                onClick={() => setConfirmDraft({ role, company })}
              >
                {drafting === role.id ? <span className="spinner" /> : <Icon name="sparkle" />}{' '}
                {role.title || 'Untitled role'}
              </button>
            ))}
          </div>
        )}
      </div>

      {actionError && <ErrorBox error={actionError} />}
      {notice && (
        <div className="alert alert-ok">
          <div style={{ flex: 1 }}>{notice}</div>
        </div>
      )}

      {drafting != null && (
        <div className="card">
          <div className="row small muted" style={{ marginBottom: 8 }}>
            <span className="spinner" />
            <span>
              Ranking your network against this role and writing one message per route. This
              takes a model call per intermediary, so give it a moment.
            </span>
          </div>
          <div className="progress-track">
            <div className="progress-fill indeterminate" />
          </div>
        </div>
      )}

      {routes.loading && <Loading rows={3} />}
      {routes.error && <ErrorBox error={routes.error} onRetry={routes.reload} />}

      {!routes.loading && !routes.error && rows.length === 0 && (
        <Empty title="No introduction route into this company yet">
          {data?.missing
            ? 'This company has no opportunity in the selected campaign, so there is nothing to build a route for. Switch campaign, or pick another company.'
            : 'Either your network has nobody who connects to this company, or the routes have not been built yet. Import your connections on the Contacts screen, then pick a role above and draft.'}
        </Empty>
      )}

      {rows.length > 0 && (
        <>
          <div className="card">
            <div className="row row-wrap small muted">
              <span>
                {data.count} route{data.count === 1 ? '' : 's'} into{' '}
                <strong>{company?.company_name}</strong>
              </span>
              {data.discretion_mode && <Badge tone="warn">Discretion mode active</Badge>}
              <div className="spacer" />
              <span style={{ display: 'flex', alignItems: 'center' }}>
                Ranked by strength against relevance
                <HelpTip term="route_strength" align="right" />
              </span>
            </div>
            {data.note && (
              <p className="small muted" style={{ margin: '8px 0 0', lineHeight: 1.55 }}>
                {data.note}
              </p>
            )}
          </div>

          <div className="stack">
            {rows.map((route, index) => (
              <RouteCard
                key={route.path_id || route.network_member_id || `${route.intermediary_name}-${index}`}
                rank={index + 1}
                route={route}
                onStatus={setStatus}
              />
            ))}
          </div>
        </>
      )}

      {confirmDraft && (
        <Modal
          title={`Draft introduction requests for ${confirmDraft.role.title || 'this role'}?`}
          onClose={() => setConfirmDraft(null)}
          actions={
            <>
              <button className="btn" onClick={() => setConfirmDraft(null)}>
                Cancel
              </button>
              <button
                className="btn btn-primary"
                onClick={() => draft(confirmDraft.role.id)}
              >
                Rank and draft
              </button>
            </>
          }
        >
          <p className="small" style={{ lineHeight: 1.6 }}>
            This ranks everyone in your imported network against{' '}
            <strong>{confirmDraft.company.company_name}</strong> and writes one message per
            route, asking that person to introduce you. It replaces any drafts already stored
            for this role.
          </p>
          <p className="small muted" style={{ lineHeight: 1.6 }}>
            Nobody is contacted. Each message stays here until you copy it and send it
            yourself.
          </p>
        </Modal>
      )}
    </>
  )
}

/* --- One route ------------------------------------------------------------ */

function RouteCard({ rank, route, onStatus }) {
  const relationship = RELATIONSHIP[route.relationship] || {
    label: route.relationship || 'Connection',
    tone: undefined,
  }
  const [message, setMessage] = useState(route.message_draft || '')
  const [copied, setCopied] = useState(false)
  const edited = message !== (route.message_draft || '')

  async function copy() {
    const text = route.message_subject ? `${route.message_subject}\n\n${message}` : message
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 2500)
    } catch {
      setCopied(false)
    }
  }

  return (
    <div className="card">
      <div className="card-header">
        <span className="badge">#{rank}</span>
        <h3>{route.intermediary_name}</h3>
        <Badge tone={relationship.tone}>{relationship.label}</Badge>
        {route.degree != null && <span className="tiny muted">degree {route.degree}</span>}
        <div className="spacer" />
        {route.intermediary_linkedin && (
          <a
            className="btn btn-sm btn-ghost"
            href={route.intermediary_linkedin}
            target="_blank"
            rel="noreferrer"
          >
            <Icon name="external" /> Profile
          </a>
        )}
      </div>

      <div className="grid grid-2">
        <div>
          <div className="small muted" style={{ marginBottom: 6 }}>
            {route.intermediary_role || 'Role not known'}
            {route.company_name ? ` · ${route.company_name}` : ''}
          </div>
          {route.shared && (
            <div className="small" style={{ marginBottom: 6 }}>
              Shared: <strong>{route.shared}</strong>
            </div>
          )}
          {route.mutual_name && (
            <div className="small" style={{ marginBottom: 6 }}>
              Knows <strong>{route.mutual_name}</strong> at the company
            </div>
          )}
          {route.rationale && (
            <p className="small muted" style={{ margin: '6px 0 0', lineHeight: 1.55 }}>
              {route.rationale}
            </p>
          )}
          {route.opportunity_title && (
            <div className="small muted" style={{ marginTop: 8 }}>
              Best route for{' '}
              <Link to={`/opportunities/${route.opportunity_id}`}>{route.opportunity_title}</Link>
            </div>
          )}
        </div>

        <div>
          <div className="row small" style={{ marginBottom: 4 }}>
            <span className="muted" style={{ minWidth: 82, display: 'flex', alignItems: 'center' }}>
              Strength
              <HelpTip term="route_strength" />
            </span>
            <Meter value={(route.strength ?? 0) * 100} />
          </div>
          <div className="row small" style={{ marginBottom: 4 }}>
            <span className="muted" style={{ minWidth: 82 }}>
              Relevance
            </span>
            <Meter value={(route.relevance ?? 0) * 100} />
          </div>
          <div className="row small">
            <span className="muted" style={{ minWidth: 82 }}>
              Rank score
            </span>
            <Meter value={(route.score ?? 0) * 100} />
          </div>

          {(route.target_name || route.target_role) && (
            <div className="small muted" style={{ marginTop: 10, lineHeight: 1.5 }}>
              Would introduce you to <strong>{route.target_name || 'the hiring contact'}</strong>
              {route.target_role ? `, ${route.target_role}` : ''}.
            </div>
          )}

          {route.path_id && (
            <div className="row" style={{ marginTop: 10 }}>
              <span
                className="small muted"
                style={{ display: 'flex', alignItems: 'center' }}
              >
                Status
                <HelpTip title="Where this route has got to">
                  Your own record of the conversation. It is not detected: mark a route
                  “asked” when you have sent the message, so the list stops looking untouched.
                </HelpTip>
              </span>
              <select
                style={{ width: 'auto', fontSize: 12, padding: '2px 6px' }}
                value={STATUSES.includes(route.status) ? route.status : 'proposed'}
                onChange={(e) => onStatus(route.path_id, e.target.value)}
              >
                {STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>
      </div>

      {(route.message_draft || route.message_subject) && (
        <div style={{ marginTop: 12 }}>
          <div className="row small" style={{ marginBottom: 6 }}>
            <strong style={{ display: 'flex', alignItems: 'center' }}>
              Message to {route.intermediary_name}
              <HelpTip term="intermediary" />
            </strong>
            <div className="spacer" />
            <button className="btn btn-sm" onClick={copy}>
              <Icon name="copy" /> {copied ? 'Copied' : 'Copy'}
            </button>
          </div>
          {route.message_subject && (
            <div className="small muted" style={{ marginBottom: 6 }}>
              Subject: {route.message_subject}
            </div>
          )}
          <textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            rows={8}
            aria-label={`Introduction request to ${route.intermediary_name}`}
          />
          <div className="tiny muted" style={{ marginTop: 4, lineHeight: 1.5 }}>
            {edited
              ? 'Edited here only — there is no endpoint that stores an edited draft, so copy it before you leave this screen. Re-drafting the role will overwrite the stored text.'
              : 'Edit it before you copy. This asks somebody you know for an introduction; it is not the email to the hiring contact.'}
          </div>
        </div>
      )}
    </div>
  )
}
