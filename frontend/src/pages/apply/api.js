/**
 * The Apply Browser's calls, in one place.
 *
 * Every one of them goes through `api/client.js`, so the session cookie, the
 * 401 handling and the NFR-701 observability apply here as they do everywhere
 * else. What this module adds is the query-string building and the two URL
 * shapes the screen needs that are not JSON: the document endpoint, which is
 * used both as an `<iframe>` source for the preview and as a download.
 *
 * The document routes are same-origin (the dev server proxies `/api`), so the
 * browser sends the session cookie with an `<iframe>` request by itself. That
 * is why a PDF can be previewed without holding a blob in memory per tab.
 */

import { api } from '../../api/client'

function qs(params) {
  const search = new URLSearchParams()
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return
    // The list endpoint takes `filters` as a repeated parameter, and the
    // filters are ANDed on the server. Flattening an array here is what keeps
    // that a query rather than something this screen has to re-implement.
    if (Array.isArray(value)) value.forEach((v) => search.append(key, String(v)))
    else search.append(key, String(value))
  })
  const text = search.toString()
  return text ? `?${text}` : ''
}

export const applyApi = {
  /** One page of the browser list (FR-284, NFR-502). */
  browse: (params) => api.get(`/apply/browser${qs(params)}`),

  /** Everything about one application: email, documents, checks, send check. */
  detail: (opportunityId) => api.get(`/apply/${opportunityId}`),

  /** The send guard, read before either send control is pressed (RK-05). */
  sendStatus: () => api.get('/apply/send-status'),

  /** FR-284: what the pipeline works on. */
  select: (opportunityIds, selected) =>
    api.post('/apply/select', { opportunity_ids: opportunityIds, selected }),

  /** FR-301: find who to write to, for the selection. */
  discoverContacts: (body) => api.post('/apply/contacts/discover', body),

  /** FR-321: the four artefacts, as a resumable job. */
  generate: (body) => api.post('/apply/packages/generate', body),

  /** FR-324: the job seeker's own edits to the email. */
  editEmail: (opportunityId, body) => api.put(`/apply/${opportunityId}/email`, body),

  /** FR-324: regenerate with an instruction, keeping the same package. */
  regenerate: (opportunityId, body) => api.post(`/apply/${opportunityId}/regenerate`, body),

  /** One application. Runs the whole path; the guard decides what happens. */
  sendOne: (opportunityId, body) => api.post(`/apply/${opportunityId}/send`, body || {}),

  /** FR-324: the approved selection, behind the confirmation modal. */
  sendAll: (body) => api.post('/apply/send-all', body || {}),

  /** The CV templates and the two seeker-only kinds (FR-321, FR-331). */
  templates: () => api.get('/applications/templates'),

  /**
   * FR-322: the same facts in another layout. This is a re-render, not a
   * rewrite — no model call, no new wording, and nothing for the consistency
   * check to reconsider — so it is deliberately not the regenerate route.
   */
  retemplate: (packageId, template) =>
    api.post(`/applications/${packageId}/cv-template`, { template }),

  /** FR-322 / NFR-206: re-read the current text and re-run both checks. */
  recheck: (packageId) => api.post(`/applications/${packageId}/consistency`),

  /**
   * FR-324: authorise dispatch. It sends nothing by itself — approval and
   * dispatch are two decisions, and keeping them apart is what lets a person
   * approve twenty in the morning and send them when the window opens.
   */
  approve: (packageId, body) => api.post(`/applications/${packageId}/approve`, body || {}),

  /** FR-324: withdraw one from the run, with a reason kept on the record. */
  discard: (packageId, reason) =>
    api.post(`/applications/${packageId}/discard`, { reason }),

  /** FR-106: whether the photograph may be used in a generated CV. */
  profile: () => api.get('/profile/'),

  /** A viewable URL for a generated document — same origin, cookie-carried. */
  documentUrl: (opportunityId, kind) => `/api/apply/${opportunityId}/document/${kind}`,

  /** FR-331: the file itself, saved to disk. */
  download: (opportunityId, kind, filename) =>
    api.download(`/apply/${opportunityId}/document/${kind}`, filename),
}

export default applyApi
