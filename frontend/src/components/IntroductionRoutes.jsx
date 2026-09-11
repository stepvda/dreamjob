/**
 * FR-461: introduction routes into a target — one implementation, two feeds.
 *
 * This is the single canonical introduction-routes view. It is mounted by two
 * screens, which reach the same capability through two different APIs:
 *
 *   mode="company"      /networking  — GET/POST /networking/introductions…
 *                       Routes are read per *company* (one network around a
 *                       company, however many roles it has), ranked and
 *                       drafted per role, and their status is recorded.
 *   mode="opportunity"  /contacts    — GET /contacts/opportunities/:id/introductions
 *                       POST /contacts/introductions/:id/message
 *                       Routes are read per *opportunity* from the stored
 *                       paths, and one message to one intermediary is rewritten
 *                       on demand.
 *
 * Both feeds carry the same core shape (relationship, degree, strength,
 * relevance, target, message draft), so a single card renders either. Keeping
 * both calls alive matters: neither backend endpoint is dropped while the two
 * routes settle on one.
 *
 * The message drafted here goes to the *intermediary* — somebody in your own
 * network — asking them to introduce you. It is not the introduction email to
 * the hiring contact, which lives on the Applications screen.
 */

import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { HelpTip } from './Help'
import Icon from './Icon'
import { Badge, Empty, ErrorBox, Loading, Meter, Modal, useFetch } from './ui'

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

export default function IntroductionRoutes({ mode = 'company', campaignId, companies = [] }) {
  const isCompanyMode = mode === 'company'

  const [companyId, setCompanyId] = useState(companies[0]?.company_id || '')
  const [oppId, setOppId] = useState('')
  const [actionError, setActionError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [drafting, setDrafting] = useState(null)
  const [draftingPath, setDraftingPath] = useState(null)
  const [confirmDraft, setConfirmDraft] = useState(null)

  // The contacts feed is per opportunity, so it discovers the opportunities
  // itself; the networking feed is handed the companies it already loaded.
  const opportunities = useFetch(
    () =>
      isCompanyMode
        ? Promise.resolve(null)
        : api.get('/opportunities?limit=100').catch(() => []),
    [isCompanyMode],
  )
  const opps = Array.isArray(opportunities.data)
    ? opportunities.data
    : opportunities.data?.items || opportunities.data?.opportunities || []
  const opportunity = opps.find((o) => String(o.id) === String(oppId)) || null

  const company = companies.find((c) => c.company_id === companyId) || companies[0]

  const routes = useFetch(
    () => {
      if (isCompanyMode) {
        if (!companyId) return Promise.resolve(null)
        return (
          api
            .get(
              `/networking/introductions/company/${encodeURIComponent(companyId)}?limit=15${
                campaignId ? `&campaign_id=${encodeURIComponent(campaignId)}` : ''
              }`,
            )
            // A company with no opportunity in scope answers 404. That is an
            // empty result for this screen, not a failure.
            .catch((err) =>
              err.status === 404
                ? { routes: [], count: 0, missing: true }
                : Promise.reject(err),
            )
        )
      }
      if (!oppId) return Promise.resolve(null)
      return api.get(`/contacts/opportunities/${encodeURIComponent(oppId)}/introductions`)
    },
    [isCompanyMode, companyId, campaignId, oppId],
  )

  const data = routes.data
  const rows = useMemo(() => {
    if (!data) return []
    if (Array.isArray(data)) return data
    return data.routes || []
  }, [data])
  const count = Array.isArray(data) ? data.length : data?.count ?? rows.length
  const discretionMode = Array.isArray(data) ? false : data?.discretion_mode
  const note = Array.isArray(data) ? null : data?.note
  const missing = Array.isArray(data) ? false : data?.missing
  const selected = isCompanyMode
    ? company?.company_name
    : opportunity?.company_name || opportunity?.title

  /** Replace one route anywhere it sits in either feed's response shape. */
  function patchRoute(pathId, patch) {
    routes.setData((current) => {
      if (!current) return current
      if (Array.isArray(current)) {
        return current.map((r) =>
          (r.path_id || r.id) === pathId ? { ...r, ...patch } : r,
        )
      }
      return {
        ...current,
        routes: (current.routes || []).map((r) =>
          (r.path_id || r.id) === pathId ? { ...r, ...patch } : r,
        ),
      }
    })
  }

  /** Networking feed: rank every route for a role and draft each message. */
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

  /** Contacts feed: rewrite the message to a single intermediary (FR-461). */
  async function draftMessage(route) {
    const pathId = route.path_id || route.id
    if (!pathId) return
    setActionError(null)
    setNotice(null)
    setDraftingPath(pathId)
    try {
      const res = await api.post(
        `/contacts/introductions/${encodeURIComponent(pathId)}/message`,
        {},
      )
      patchRoute(pathId, {
        message_subject: res.message_subject,
        message_draft: res.message_draft || res.draft || res.message || '',
      })
      setNotice(
        'Message drafted. Nothing has been sent — read and edit it, then copy it into your own account.',
      )
    } catch (err) {
      setActionError(err)
    } finally {
      setDraftingPath(null)
    }
  }

  async function setStatus(pathId, status) {
    setActionError(null)
    try {
      if (isCompanyMode) {
        await api.post(`/networking/introductions/${pathId}/status`, { status })
      } else {
        await api.patch(`/contacts/introductions/${pathId}`, { status })
      }
      routes.reload()
    } catch (err) {
      setActionError(err)
    }
  }

  return (
    <>
      {!isCompanyMode && (
        <p className="section-intro">
          A warm introduction outperforms a cold email, so routes are ranked by the
          strength of the relationship. The message here goes to the <em>intermediary</em>,
          asking them to introduce you — it is not the application itself, which lives on
          the Applications screen.
        </p>
      )}

      {isCompanyMode ? (
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
      ) : (
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
      )}

      {actionError && <ErrorBox error={actionError} />}
      {notice && (
        <div className="alert alert-ok">
          <div style={{ flex: 1 }}>{notice}</div>
        </div>
      )}

      {isCompanyMode && drafting != null && (
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

      {!routes.loading && !routes.error && rows.length === 0 && (isCompanyMode || oppId) && (
        isCompanyMode ? (
          <Empty title="No introduction route into this company yet">
            {missing
              ? 'This company has no opportunity in the selected campaign, so there is nothing to build a route for. Switch campaign, or pick another company.'
              : 'Either your network has nobody who connects to this company, or the routes have not been built yet. Import your connections on the Contacts screen, then pick a role above and draft.'}
          </Empty>
        ) : (
          <Empty title="No introduction route found">
            Nobody in your imported network is connected to this company. You can still apply
            directly — or import more of your network on the previous tab.
          </Empty>
        )
      )}

      {rows.length > 0 && (
        <>
          <div className="card">
            <div className="row row-wrap small muted">
              <span>
                {count} route{count === 1 ? '' : 's'} into <strong>{selected}</strong>
              </span>
              {discretionMode && <Badge tone="warn">Discretion mode active</Badge>}
              <div className="spacer" />
              <span style={{ display: 'flex', alignItems: 'center' }}>
                Ranked by strength against relevance
                <HelpTip term="route_strength" align="right" />
              </span>
            </div>
            {note && (
              <p className="small muted" style={{ margin: '8px 0 0', lineHeight: 1.55 }}>
                {note}
              </p>
            )}
          </div>

          <div className="stack">
            {rows.map((route, index) => (
              <RouteCard
                key={
                  route.path_id ||
                  route.id ||
                  route.network_member_id ||
                  `${route.intermediary_name}-${index}`
                }
                rank={index + 1}
                route={route}
                onDraft={isCompanyMode ? undefined : draftMessage}
                draftingPath={draftingPath}
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

function RouteCard({ rank, route, onStatus, onDraft, draftingPath }) {
  const relationship = RELATIONSHIP[route.relationship] || {
    label: route.relationship || 'Connection',
    tone: undefined,
  }
  const pathId = route.path_id || route.id
  const [message, setMessage] = useState(route.message_draft || '')
  const [copied, setCopied] = useState(false)
  const edited = message !== (route.message_draft || '')

  // A freshly fetched draft (from either feed) replaces the local copy; an
  // unsaved edit survives a reload because the stored text has not changed.
  useEffect(() => {
    setMessage(route.message_draft || '')
  }, [route.message_draft])

  const strength = route.strength ?? 0
  const relevance = route.relevance ?? 0
  // The networking feed publishes the merged rank under "score"; the contacts
  // feed stores the two halves only, so the same 0.6/0.4 combination is
  // recomputed rather than showing an empty meter.
  const score = route.score ?? 0.6 * strength + 0.4 * relevance

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
            <Meter value={strength * 100} />
          </div>
          <div className="row small" style={{ marginBottom: 4 }}>
            <span className="muted" style={{ minWidth: 82 }}>
              Relevance
            </span>
            <Meter value={relevance * 100} />
          </div>
          <div className="row small">
            <span className="muted" style={{ minWidth: 82 }}>
              Rank score
            </span>
            <Meter value={score * 100} />
          </div>

          {(route.target_name || route.target_role) && (
            <div className="small muted" style={{ marginTop: 10, lineHeight: 1.5 }}>
              Would introduce you to <strong>{route.target_name || 'the hiring contact'}</strong>
              {route.target_role ? `, ${route.target_role}` : ''}.
            </div>
          )}

          {pathId && (
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
                onChange={(e) => onStatus(pathId, e.target.value)}
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

      {onDraft && (
        <div style={{ marginTop: 12 }}>
          <button
            className="btn btn-sm"
            disabled={draftingPath != null}
            onClick={() => onDraft(route)}
          >
            {draftingPath === pathId ? <span className="spinner" /> : <Icon name="edit" />} Draft
            the request
          </button>
        </div>
      )}

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
