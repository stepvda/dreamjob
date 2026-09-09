/**
 * Single API client for the whole SPA.
 *
 * Every request goes through `request()`, which is what makes three things
 * consistent across sixteen screens: session cookies are always sent, a 401
 * always lands the user on the sign-in screen, and an error always arrives as
 * an `ApiError` carrying the backend's own `detail` message rather than a
 * generic "request failed".
 */

import { observeApi } from '../observability/logger'

const BASE = '/api'

export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

let onUnauthorized = () => {}
export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn
}

async function request(path, { method = 'GET', body, headers = {}, raw = false } = {}) {
  const opts = {
    method,
    credentials: 'include',
    headers: { ...headers },
  }

  if (body instanceof FormData) {
    opts.body = body
  } else if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }

  // NFR-701: this is the one funnel every screen's traffic passes through, so
  // it is the only place a failed or slow call has to be noticed. The logger
  // sees the path, the status and the duration - never the body.
  const started = performance.now()
  let res
  try {
    res = await fetch(`${BASE}${path}`, opts)
  } catch (err) {
    observeApi({ method, path, ms: performance.now() - started, error: err })
    throw err
  }
  observeApi({ method, path, status: res.status, ms: performance.now() - started, res })

  if (res.status === 401) {
    onUnauthorized()
    throw new ApiError('Not signed in', 401)
  }

  if (raw) {
    if (!res.ok) throw new ApiError(`Request failed (${res.status})`, res.status)
    return res
  }

  const text = await res.text()
  const data = text ? safeParse(text) : null

  if (!res.ok) {
    const detail = data?.detail ?? data?.message ?? text
    throw new ApiError(
      extractMessage(detail, res.status),
      res.status,
      detail,
    )
  }
  return data
}

function safeParse(text) {
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

/**
 * Turn a backend `detail` into a human-readable message.
 *
 * FastAPI validation errors return an *array* of `{ loc, msg, type }` objects,
 * which would otherwise collapse into a useless "Request failed (422)". A plain
 * string detail (most other errors) is used as-is.
 */
function extractMessage(detail, status) {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail.length > 0) {
    // FastAPI puts the failing field in loc, e.g. ["body","password"].
    const first = detail.find((e) => e && typeof e.msg === 'string') || detail[0]
    const field = first?.loc?.filter((p) => p !== 'body').join(' ')
    const msg = first?.msg || 'validation error'
    return field ? `${field}: ${msg}` : msg
  }
  if (detail && typeof detail === 'object') {
    const text = detail.message || detail.error
    if (typeof text === 'string') return text
  }
  return `Request failed (${status})`
}

export const api = {
  get: (p) => request(p),
  post: (p, body) => request(p, { method: 'POST', body }),
  put: (p, body) => request(p, { method: 'PUT', body }),
  patch: (p, body) => request(p, { method: 'PATCH', body }),
  del: (p) => request(p, { method: 'DELETE' }),
  upload: (p, formData) => request(p, { method: 'POST', body: formData }),
  /** Trigger a browser download of a generated document (FR-331, FR-463). */
  download: async (p, filename) => {
    const res = await request(p, { raw: true })
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename || p.split('/').pop()
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  },
}
