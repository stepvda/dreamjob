/**
 * Browse every stored contact (FR-301, FR-303).
 *
 * The Contacts tab is organised around a company: you pick one and read its
 * ranked ladder. That is the working view, but it cannot answer "what is in
 * the store at all?", "how many inferred addresses are there?" or "show me
 * everything found by a search provider". This view can, by reading the flat
 * `/contacts` list with server-side filters and paging.
 *
 * The certainty of each address is shown here as strictly as it is elsewhere:
 * an inferred address carries the EmailCertaintyBadge and is never presented as
 * valid.
 */

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../../api/client'
import Icon from '../../components/Icon'
import { HelpTip } from '../../components/Help'
import {
  Badge,
  Empty,
  ErrorBox,
  JobProgress,
  Loading,
  ValidationBadge,
  formatDate,
  formatPercent,
  useFetch,
} from '../../components/ui'
import EmailCertaintyBadge from './EmailCertaintyBadge'

const PAGE_SIZE = 50

/** How often the missing-address pass is polled while it runs. */
const BACKFILL_POLL_MS = 4000
/** The default number of stored contacts one pass will consider. */
const BACKFILL_LIMIT = 500

const VALIDATIONS = ['valid', 'risky', 'unknown', 'invalid']

/** The FR-303 methods a stored address can name. */
const METHODS = [
  'website',
  'press',
  'pattern_inference',
  'lookup_service',
  'manual',
  'vacancy',
  'search',
  'json_ld',
  'security_txt',
  'sitemap',
  'ats_board',
  'stored_document',
]

function browseQuery({ q, validation, method, uncertain, offset }) {
  const params = new URLSearchParams()
  if (q) params.set('q', q)
  if (validation) params.set('validation', validation)
  if (method) params.set('method', method)
  if (uncertain) params.set('uncertain', '1')
  params.set('limit', String(PAGE_SIZE))
  params.set('offset', String(offset))
  return params.toString()
}

function confidenceLabel(value) {
  if (value == null || value === '') return '–'
  const n = Number(value)
  if (Number.isNaN(n)) return '–'
  return formatPercent(n > 1 ? n / 100 : n)
}

/**
 * Ask the backend to find addresses for stored contacts that have none.
 *
 * The count is a live read of `/contacts/emails/missing`; the pass itself is a
 * background job, so it is polled exactly like the discovery pass on the parent
 * screen and rendered with the shared JobProgress component. Nothing here
 * touches the browse table's own filters or paging.
 */
function EmailBackfillPanel({ onContactsChanged }) {
  const me = useFetch(() => api.get('/auth/me').catch(() => null), [])
  const isAdmin = Boolean(me.data?.is_admin)

  const [scope, setScope] = useState('mine')
  const missing = useFetch(
    () => api.get(`/contacts/emails/missing?scope=${scope}`),
    [scope],
  )

  const [limit, setLimit] = useState(BACKFILL_LIMIT)
  const [crawlSite, setCrawlSite] = useState(true)
  const [allowSmtp, setAllowSmtp] = useState(false)
  const [backupMethods, setBackupMethods] = useState(false)
  const [batch, setBatch] = useState(null)
  const [error, setError] = useState(null)

  const count = missing.data?.count
  const forbidden = error?.status === 403 || missing.error?.status === 403
  const running = Boolean(batch && !batch.done)
  const report = batch?.job?.report || batch?.job?.checkpoint?.report

  async function start() {
    setError(null)
    const parsed = Math.round(Number(limit))
    const n = Number.isFinite(parsed) ? Math.max(1, Math.min(5000, parsed)) : BACKFILL_LIMIT
    setLimit(n)
    try {
      const started = await api.post('/contacts/emails/backfill', {
        limit: n,
        scope,
        max_companies: null,
        allow_smtp: allowSmtp,
        crawl_site: crawlSite,
        use_lookup_service: false,
        backup_methods: backupMethods,
      })
      setBatch({
        job_id: started.job_id,
        job: null,
        done: false,
        reused: started.reused === true,
      })
    } catch (e) {
      setError(e)
    }
  }

  // Same polling style as the discovery pass: ask every few seconds, reload the
  // count and the table the moment the job reaches a terminal state.
  useEffect(() => {
    if (!batch?.job_id || batch.done) return undefined
    const timer = setInterval(async () => {
      try {
        const job = await api.get(`/contacts/emails/backfill/${batch.job_id}`)
        const finished = ['done', 'failed', 'cancelled'].includes(job.status)
        setBatch((current) => (current ? { ...current, job, done: finished } : current))
        if (finished) {
          missing.reload()
          onContactsChanged?.()
        }
      } catch (e) {
        setError(e)
      }
    }, BACKFILL_POLL_MS)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batch?.job_id, batch?.done])

  return (
    <div className="card">
      <div className="card-header">
        <Icon name="mailsetup" />
        <h3>Find missing e-mail addresses</h3>
        <div className="spacer" />
        {missing.loading && <span className="small muted">checking…</span>}
        {!missing.loading && count != null && <span className="badge">{count}</span>}
      </div>

      {forbidden ? (
        <div className="alert alert-danger">
          <div>
            <strong>Administrator access required.</strong> Searching every stored contact
            (&ldquo;All contacts&rdquo;) is restricted to administrators. Choose &ldquo;My
            contacts&rdquo;, or ask an administrator to run the installation-wide pass.
          </div>
        </div>
      ) : count === 0 ? (
        <p className="small muted" style={{ marginBottom: 0 }}>
          Every stored contact has an address.
        </p>
      ) : (
        <>
          <p className="small muted">
            {count == null
              ? 'Checking how many stored contacts have no address…'
              : `${count} stored ${count === 1 ? 'contact has' : 'contacts have'} no e-mail address.`}{' '}
            The pass reads each company&apos;s own site and published pages first; anything it
            composes from a domain pattern is stored as <em>unverified</em> and never shown as
            valid.
          </p>

          <div className="row row-wrap" style={{ gap: 12, alignItems: 'flex-end' }}>
            <div className="field" style={{ marginBottom: 0, width: 170 }}>
              <label>How many to process</label>
              <input
                type="number"
                min={1}
                max={5000}
                value={limit}
                disabled={running}
                onChange={(e) => setLimit(e.target.value)}
              />
            </div>

            {isAdmin && (
              <div className="field" style={{ marginBottom: 0, width: 200 }}>
                <label>
                  Scope
                  <HelpTip title="Which contacts to search">
                    &ldquo;My contacts&rdquo; covers the people attached to your own companies
                    and applications. &ldquo;All contacts&rdquo; searches the whole installation
                    and is available to administrators only.
                  </HelpTip>
                </label>
                <select value={scope} disabled={running} onChange={(e) => setScope(e.target.value)}>
                  <option value="mine">My contacts</option>
                  <option value="all">All contacts</option>
                </select>
              </div>
            )}

            <div className="field" style={{ marginBottom: 0 }}>
              <label>&nbsp;</label>
              <label className="row small" style={{ gap: 6, cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={crawlSite}
                  disabled={running}
                  onChange={(e) => setCrawlSite(e.target.checked)}
                />
                Crawl company sites
              </label>
            </div>

            <div className="field" style={{ marginBottom: 0 }}>
              <label>&nbsp;</label>
              <label className="row small" style={{ gap: 6, cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={allowSmtp}
                  disabled={running}
                  onChange={(e) => setAllowSmtp(e.target.checked)}
                />
                SMTP probe
                <HelpTip title="SMTP probe">
                  Opens a probe connection to the mail server to confirm a mailbox exists.
                  It is much slower and less polite than reading published pages, so it is
                  off by default and should be used sparingly.
                </HelpTip>
              </label>
            </div>

            <div className="field" style={{ marginBottom: 0 }}>
              <label>&nbsp;</label>
              <label className="row small" style={{ gap: 6, cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={backupMethods}
                  disabled={running}
                  onChange={(e) => setBackupMethods(e.target.checked)}
                />
                Backup methods
                <HelpTip title="Backup methods">
                  Also try the company&apos;s stored postings, its ATS board and its
                  legal/imprint pages when the normal search finds nothing.
                </HelpTip>
              </label>
            </div>

            <button
              className="btn btn-primary"
              disabled={running || missing.loading || !count}
              onClick={start}
            >
              {running ? <span className="spinner" /> : <Icon name="mailsetup" />}
              Find missing e-mail addresses
            </button>
          </div>
          {allowSmtp && (
            <p className="small muted" style={{ margin: '8px 0 0' }}>
              The SMTP probe is slower and less polite: it connects to each mail server to
              check a mailbox exists. Leave it off unless the count will not come down
              otherwise.
            </p>
          )}
        </>
      )}

      {error && !forbidden && <ErrorBox error={error} onRetry={() => setError(null)} />}

      {batch && (
        <div style={{ marginTop: 12 }}>
          {batch.reused && !batch.done && (
            <p className="small muted" style={{ margin: '0 0 8px' }}>
              Continuing the run already in progress.
            </p>
          )}
          <JobProgress
            report={batch.job?.report}
            job={
              batch.job
                ? { ...batch.job, kind: 'Finding missing e-mail addresses' }
                : {
                    kind: 'Finding missing e-mail addresses',
                    status: 'running',
                    progress_done: 0,
                    progress_total: limit,
                  }
            }
          />
          {report && <BackfillSummary report={report} />}
          {batch.done && batch.job?.status === 'failed' && (
            <p className="small muted" style={{ margin: '8px 0 0' }}>
              {batch.job.last_error || 'The pass failed before it could finish.'}
            </p>
          )}
        </div>
      )}
    </div>
  )
}

function BackfillSummary({ report }) {
  const n = (value) => Number(value) || 0
  return (
    <p className="small muted" style={{ margin: '8px 0 0' }}>
      <strong>{n(report.updated)}</strong> addresses found
      {n(report.uncertain) > 0 && (
        <>
          {' '}
          · <strong>{n(report.uncertain)}</strong> left uncertain
        </>
      )}{' '}
      · skipped {n(report.skipped_no_domain)} with no domain, {n(report.skipped_unresolved)}{' '}
      unresolved, {n(report.skipped_invalid)} invalid.
      {n(report.companies_visited) > 0 && (
        <>
          {' '}
          Visited {n(report.companies_visited)}{' '}
          {n(report.companies_visited) === 1 ? 'company' : 'companies'}.
        </>
      )}{' '}
      Addresses composed from a domain pattern are marked <em>unverified</em>.
    </p>
  )
}

export default function BrowseContacts() {
  // The text box is debounced; the selects and the checkbox apply at once.
  // Either way every change resets paging, because page 3 of a new search is
  // almost never where the user wants to land.
  const [draft, setDraft] = useState('')
  const [q, setQ] = useState('')
  const [validation, setValidation] = useState('')
  const [method, setMethod] = useState('')
  const [uncertain, setUncertain] = useState(false)
  const [offset, setOffset] = useState(0)
  const [deletingId, setDeletingId] = useState(null)
  const [deleteNotice, setDeleteNotice] = useState(null)
  const [deleteError, setDeleteError] = useState(null)

  useEffect(() => {
    const timer = setTimeout(() => {
      setQ(draft.trim())
      setOffset(0)
    }, 400)
    return () => clearTimeout(timer)
  }, [draft])

  const { data, error, loading, reload } = useFetch(
    () => api.get(`/contacts?${browseQuery({ q, validation, method, uncertain, offset })}`),
    [q, validation, method, uncertain, offset],
  )

  const items = data?.items ?? []
  const total = Number.isFinite(data?.total) ? data.total : items.length
  const facets = data?.facets ?? null
  const filtered = Boolean(q || validation || method || uncertain)

  function submit(e) {
    e.preventDefault()
    setQ(draft.trim())
    setOffset(0)
  }

  function clearAll() {
    setDraft('')
    setQ('')
    setValidation('')
    setMethod('')
    setUncertain(false)
    setOffset(0)
  }

  /**
   * NFR-303: a campaign-scoped row can be deleted; a shared one is refused by
   * the API (409) and points at the objection route. The button is disabled for
   * shared rows, so an unexpected 409 is surfaced rather than swallowed.
   */
  async function removeContact(contact) {
    const label = contact.full_name || contact.email || 'this contact'
    const confirmed = window.confirm(
      `Delete ${label}? This removes the campaign-scoped record permanently. The person ` +
        'is not blocked, so an objection is the way to stop contact for good (NFR-302).',
    )
    if (!confirmed) return
    setDeletingId(contact.id)
    setDeleteNotice(null)
    setDeleteError(null)
    try {
      await api.del(`/contacts/${contact.id}`)
      setDeleteNotice(`Deleted ${label}.`)
      reload()
    } catch (e) {
      setDeleteError(e)
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <>
      <div className="card">
        <div className="card-header">
          <Icon name="contacts" />
          <h3>Browse all contacts</h3>
          <div className="spacer" />
          <span className="small muted">
            {total} {total === 1 ? 'contact' : 'contacts'} stored
          </span>
        </div>
        <p className="small muted">
          Every address the installation holds, across every company, searchable on its
          own. Validation and source method are filters rather than labels so you can ask
          for exactly the slice you mean — for example every address inferred from a
          domain pattern that has not been confirmed.
        </p>

        <form className="row row-wrap" onSubmit={submit} style={{ alignItems: 'flex-end' }}>
          <div className="field" style={{ marginBottom: 0, minWidth: 240, flex: 1 }}>
            <label>Search</label>
            <input
              type="text"
              value={draft}
              placeholder="Name, e-mail, role, company…"
              onChange={(e) => setDraft(e.target.value)}
            />
          </div>

          <div className="field" style={{ marginBottom: 0, width: 150 }}>
            <label>Validation</label>
            <select
              value={validation}
              onChange={(e) => {
                setValidation(e.target.value)
                setOffset(0)
              }}
            >
              <option value="">Any validation</option>
              {VALIDATIONS.map((v) => (
                <option key={v} value={v}>
                  {v}
                  {facets?.by_validation?.[v] != null ? ` (${facets.by_validation[v]})` : ''}
                </option>
              ))}
            </select>
          </div>

          <div className="field" style={{ marginBottom: 0, width: 190 }}>
            <label>
              Source method
              <HelpTip title="How the address was found">
                From the company website, a press page, a stored posting or its ATS board,
                a legal/imprint page, inferred from the pattern of other addresses on the
                same domain, a lookup service, a vacancy, a search provider or structured
                data. An inferred address deserves less confidence than one published on
                the company&apos;s own site.
              </HelpTip>
            </label>
            <select
              value={method}
              onChange={(e) => {
                setMethod(e.target.value)
                setOffset(0)
              }}
            >
              <option value="">Any method</option>
              {METHODS.map((m) => (
                <option key={m} value={m}>
                  {m.replace(/_/g, ' ')}
                  {facets?.by_method?.[m] != null ? ` (${facets.by_method[m]})` : ''}
                </option>
              ))}
            </select>
          </div>

          <div className="field" style={{ marginBottom: 0 }}>
            <label>&nbsp;</label>
            <label className="row small" style={{ gap: 6, cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={uncertain}
                onChange={(e) => {
                  setUncertain(e.target.checked)
                  setOffset(0)
                }}
              />
              Only unverified
              <HelpTip title="Only unverified">
                Show only addresses that were inferred from the domain&apos;s pattern and
                could not be confirmed. These are guesses: treat them as worth checking,
                never as usable addresses. {facets?.uncertain ? `${facets.uncertain} match.` : ''}
              </HelpTip>
            </label>
          </div>

          <button className="btn btn-primary">Search</button>
          {filtered && (
            <button type="button" className="btn btn-ghost" onClick={clearAll}>
              Clear
            </button>
          )}
        </form>
      </div>

      <EmailBackfillPanel onContactsChanged={reload} />

      {loading && <Loading rows={6} />}
      {error && <ErrorBox error={error} onRetry={reload} />}

      {!loading && !error && !items.length && !filtered && (
        <Empty title="No contacts on record">
          Contacts arrive when a discovery pass runs: pick a company on the Contacts tab,
          or find contacts for your shortlist. Nothing has been collected yet.
        </Empty>
      )}

      {!loading && !error && !items.length && filtered && (
        <Empty
          title="No contact matches these filters"
          action={
            <button className="btn" onClick={clearAll}>
              Clear filters
            </button>
          }
        >
          Nothing stored matches this search. Clear a filter or broaden it.
        </Empty>
      )}

      {deleteNotice && (
        <div className="alert alert-ok">
          <div style={{ flex: 1 }}>{deleteNotice}</div>
          <button className="btn btn-sm btn-ghost" onClick={() => setDeleteNotice(null)}>
            ✕
          </button>
        </div>
      )}
      {deleteError && <ErrorBox error={deleteError} onRetry={() => setDeleteError(null)} />}

      {!loading && !error && items.length > 0 && (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Company</th>
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
                  <th>Certainty</th>
                  <th>Source</th>
                  <th>Confidence</th>
                  <th>Collected</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {items.map((c) => (
                  <tr key={c.id}>
                    <td>
                      {c.company_id ? (
                        <Link to={`/companies/${c.company_id}`}>{c.company_name || '–'}</Link>
                      ) : (
                        <span className="muted">{c.company_name || '–'}</span>
                      )}
                    </td>
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
                    <td>
                      <EmailCertaintyBadge contact={c} />
                    </td>
                    <td className="small muted">
                      {c.email_source_method || '–'}
                    </td>
                    <td className="small">{confidenceLabel(c.confidence)}</td>
                    <td className="small nowrap">
                      {formatDate(c.collected_at)}
                      {c.retention_until && (
                        <div className="tiny muted">until {formatDate(c.retention_until)}</div>
                      )}
                    </td>
                    <td className="nowrap">
                      {c.shareable === 0 ? (
                        <button
                          className="btn btn-sm btn-danger"
                          disabled={deletingId === c.id}
                          onClick={() => removeContact(c)}
                        >
                          {deletingId === c.id ? (
                            <span className="spinner" />
                          ) : (
                            <Icon name="trash" />
                          )}
                          Delete
                        </button>
                      ) : (
                        <span
                          title="Shared contact — use Object so it is never contacted"
                          style={{ display: 'inline-flex' }}
                        >
                          <button className="btn btn-sm btn-ghost" disabled>
                            <Icon name="trash" /> Delete
                          </button>
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="row" style={{ marginTop: 12 }}>
            <span className="small muted">
              {offset + 1}–{offset + items.length} of {total}
            </span>
            <div className="spacer" />
            <button
              className="btn btn-sm"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              Previous
            </button>
            <button
              className="btn btn-sm"
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Next
            </button>
          </div>
        </>
      )}
    </>
  )
}
