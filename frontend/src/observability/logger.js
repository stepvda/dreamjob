/**
 * Client-side structured logging (NFR-701).
 *
 * The backend writes a structured line for every request it serves; without a
 * matching stream from the browser, half of what a job seeker actually
 * experiences - a screen that never rendered, a call that took eleven seconds,
 * a promise nobody caught - leaves no trace anywhere. This module is that
 * second half: it buffers entries in memory and ships them to
 * `POST /api/logs/client`, which appends them to `logs/frontend.log`.
 *
 * Three properties matter more than the feature list:
 *
 *  - It is silent. A logging failure never surfaces to the person using the
 *    app, and never becomes an entry of its own, because a log entry about a
 *    failed log shipment is the first turn of an infinite loop.
 *  - It is correlated. Every entry carries the correlation id echoed by the
 *    last API response, so a stack trace in `frontend.log` and the request
 *    line in the backend log can be put side by side.
 *  - It records success, not only failure. Confirming that the application
 *    works is as much a part of NFR-701 as spotting that it does not, so
 *    navigations and completed calls are logged too - at a level low enough
 *    that they stay out of the way in production.
 */

// The backend endpoint is owned by the backend-logging work. If it is not
// mounted yet every shipment simply 404s and is discarded, which is the
// correct failure mode for logging: the app is unaffected.
const ENDPOINT = '/api/logs/client'

const FLUSH_MS = 5000 // the timer flush; an error does not wait for it
const MAX_BUFFER = 200 // a broken loop must not eat the tab's memory
const SLOW_API_MS = 1500 // above this a call is worth a line of its own
const MAX_STRING = 500 // per context string
const MAX_STACK = 4000
const MAX_DEPTH = 3

const LEVELS = { debug: 10, info: 20, warn: 30, error: 40 }

// In development every completed call is interesting; in production the debug
// stream would bury the two lines that matter, so it is dropped before it is
// ever buffered.
const MIN_LEVEL = import.meta.env?.DEV ? LEVELS.debug : LEVELS.info

/* --- Identity ------------------------------------------------------------- */

const CLIENT_ID_KEY = 'dreamjob.client_id'

function randomId() {
  try {
    return crypto.randomUUID().replace(/-/g, '').slice(0, 16)
  } catch {
    return Math.random().toString(16).slice(2, 10) + Math.random().toString(16).slice(2, 10)
  }
}

/**
 * An id that is stable for as long as this browser tab lives.
 *
 * It groups a person's entries into one readable sequence without identifying
 * them: it is a random value in session storage, never the seeker id and never
 * anything derived from the session cookie. Storage can throw (private
 * browsing, blocked site data), in which case the in-memory value still holds
 * for the life of the page.
 */
function readClientId() {
  const fresh = randomId()
  try {
    const existing = sessionStorage.getItem(CLIENT_ID_KEY)
    if (existing) return existing
    sessionStorage.setItem(CLIENT_ID_KEY, fresh)
  } catch {
    /* storage unavailable - the in-memory id is good enough */
  }
  return fresh
}

const clientId = readClientId()

/* --- Correlation ---------------------------------------------------------- */

let lastCorrelationId = null

// Header name is read defensively: whichever of the two the backend middleware
// settles on, the id is picked up without a second change here.
const CORRELATION_HEADERS = ['x-request-id', 'x-correlation-id']

function captureCorrelation(res) {
  try {
    for (const name of CORRELATION_HEADERS) {
      const value = res.headers?.get(name)
      if (value) {
        lastCorrelationId = String(value).slice(0, 64)
        return
      }
    }
  } catch {
    /* an opaque response has no readable headers */
  }
}

/** The id to quote when reporting a problem - see ErrorBoundary. */
export function correlationId() {
  return lastCorrelationId
}

/* --- Redaction ------------------------------------------------------------ */

// WHAT MUST NEVER REACH THE LOG FILE.
//
// A log that contains a password is worse than no log at all: it turns a
// debugging aid into a credential store that outlives the session and gets
// copied around in support threads. So this module never reads a form control,
// never reads `document.cookie`, and drops any context key that names a
// secret. The session cookie travels with a shipment as an opaque browser
// credential (so the backend can attribute the entry to a seeker) and is never
// itself part of a payload.
const SECRET_KEY = /pass|secret|token|api[-_ ]?key|cookie|auth|credential|otp|pin|card|iban/i

/**
 * Reduce an arbitrary value to something safe and small.
 *
 * DOM nodes and events are the dangerous case - an input element carries
 * `.value`, which is whatever the person typed, and a password field carries
 * it in clear. They are therefore reduced to a tag name and nothing else,
 * here, before anything is buffered.
 */
function sanitize(value, depth = 0) {
  if (value == null) return value
  const type = typeof value

  if (type === 'string') {
    return value.length > MAX_STRING ? `${value.slice(0, MAX_STRING)}…` : value
  }
  if (type === 'number' || type === 'boolean') return value
  if (type === 'function' || type === 'symbol' || type === 'bigint') return `[${type}]`

  if (value instanceof Error) {
    return {
      name: value.name,
      message: sanitize(value.message, depth + 1),
      stack: value.stack ? String(value.stack).slice(0, MAX_STACK) : undefined,
    }
  }

  // An element or an event: the tag, never the contents of a field.
  if (typeof Element !== 'undefined' && value instanceof Element) {
    return `<${value.tagName.toLowerCase()}>`
  }
  if (typeof Event !== 'undefined' && value instanceof Event) {
    return `[${value.type} event]`
  }

  if (depth >= MAX_DEPTH) return '[…]'

  if (Array.isArray(value)) {
    return value.slice(0, 20).map((v) => sanitize(v, depth + 1))
  }

  if (type === 'object') {
    const out = {}
    for (const [key, v] of Object.entries(value).slice(0, 30)) {
      out[key] = SECRET_KEY.test(key) ? '[redacted]' : sanitize(v, depth + 1)
    }
    return out
  }

  return String(value).slice(0, MAX_STRING)
}

/* --- Buffer and transport ------------------------------------------------- */

let buffer = []
let dropped = 0

function push(level, message, context) {
  if (LEVELS[level] < MIN_LEVEL) return
  let entry
  try {
    entry = {
      ts: new Date().toISOString(),
      level,
      // Only the path. A query string can carry what somebody typed into a
      // search box, and the route is what a log reader actually needs.
      route: location.pathname,
      message: String(message ?? '').slice(0, MAX_STRING),
      client_id: clientId,
      correlation_id: lastCorrelationId,
      user_agent: navigator.userAgent,
      context: context === undefined ? undefined : sanitize(context),
    }
  } catch {
    return // a value that cannot even be described is not worth breaking for
  }

  buffer.push(entry)
  if (buffer.length > MAX_BUFFER) {
    buffer.shift()
    dropped += 1
  }
  // An error may be the last thing that happens before the tab is closed or
  // reloaded, so it does not wait for the timer.
  if (level === 'error') flush()
}

function ship(batch) {
  let payload
  try {
    payload = JSON.stringify({ entries: batch })
  } catch {
    return // unserialisable batch: drop it rather than retry forever
  }

  try {
    // A beacon survives a page that is already being torn down, which is
    // exactly the flush that matters most (visibilitychange, pagehide). It
    // carries same-origin cookies, so the backend can attribute the entries.
    if (navigator.sendBeacon) {
      const blob = new Blob([payload], { type: 'application/json' })
      if (navigator.sendBeacon(ENDPOINT, blob)) return
    }
    fetch(ENDPOINT, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: payload,
      keepalive: true,
      // Swallowed deliberately. A failed shipment must not be logged - that
      // entry would be shipped, fail, be logged... - and must not surface as
      // an unhandled rejection, which the global handler below would catch.
    }).catch(() => {})
  } catch {
    /* silent by design */
  }
}

/** Send whatever is buffered. Safe to call at any time. */
export function flush() {
  if (!buffer.length) return
  const batch = buffer
  buffer = []
  if (dropped) {
    // Say so rather than pretend the sequence is complete.
    batch.unshift({
      ts: new Date().toISOString(),
      level: 'warn',
      route: location.pathname,
      message: 'client log buffer overflowed',
      client_id: clientId,
      correlation_id: lastCorrelationId,
      user_agent: navigator.userAgent,
      context: { dropped },
    })
    dropped = 0
  }
  ship(batch)
}

/* --- Public logging surface ----------------------------------------------- */

export const log = {
  debug: (message, context) => push('debug', message, context),
  info: (message, context) => push('info', message, context),
  warn: (message, context) => push('warn', message, context),
  error: (message, context) => push('error', message, context),
}

/* --- Automatic capture ---------------------------------------------------- */

/**
 * Everything the API client sees, in one call.
 *
 * `client.js` funnels every request in the SPA through a single function, so
 * this is the only hook needed to notice a failed call, a slow one, or the
 * correlation id that ties this session to the backend's own log lines.
 */
export function observeApi({ method = 'GET', path = '', status = 0, ms = 0, res, error }) {
  // The log endpoint never reports on itself: that is the loop.
  if (path.startsWith('/logs/')) return

  if (res) captureCorrelation(res)

  // Path only - a query string may hold a typed search term, and the request
  // body (which may hold a password on sign-in) is never touched at all.
  const route = String(path).split('?')[0]
  const duration = Math.round(ms)
  const context = { method, path: route, status, ms: duration }

  if (error) {
    log.error('api request failed', { ...context, error })
    return
  }
  if (status === 401) {
    // Expected: an anonymous visitor's /auth/me answers 401 by design. Worth a
    // line for the sequence, not worth a warning.
    log.info('api unauthenticated', context)
    return
  }
  if (status >= 400) {
    log.error(status >= 500 ? 'api server error' : 'api request rejected', context)
    return
  }
  if (duration >= SLOW_API_MS) {
    log.warn('slow api call', context)
    return
  }
  log.debug('api ok', context)
}

let installed = false

/**
 * Install global capture. Idempotent: a second call (StrictMode, HMR) is a
 * no-op rather than a second set of handlers on every event.
 */
export function installGlobalHandlers() {
  if (installed) return
  installed = true

  // Uncaught errors. `addEventListener` sees what `window.onerror` sees and
  // does not clobber a handler anything else may have set. The capture phase
  // is required, not decoration: a script or stylesheet that fails to load
  // fires an `error` that does not bubble, so a listener without it would
  // never hear about the half of the failures that are not exceptions.
  addEventListener('error', (event) => {
    const target = event.target
    if (target && target !== window && target.tagName) {
      // A stylesheet, script or image that did not load: not a crash, but the
      // reason a screen looks wrong.
      log.warn('resource failed to load', {
        tag: target.tagName.toLowerCase(),
        src: String(target.src || target.href || '').slice(0, MAX_STRING),
      })
      return
    }
    log.error('uncaught error', {
      error: event.error ?? event.message,
      source: event.filename,
      line: event.lineno,
      column: event.colno,
    })
  }, true)

  addEventListener('unhandledrejection', (event) => {
    log.error('unhandled promise rejection', { reason: event.reason })
  })

  installNavigationLog()

  // A hidden tab may never come back. Flushing here, and again on pagehide, is
  // what makes the last few entries before a close reach the file at all.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flush()
  })
  addEventListener('pagehide', flush)

  setInterval(flush, FLUSH_MS)

  log.info('app loaded', {
    // Neither of these identifies anybody; both explain a rendering report.
    viewport: `${window.innerWidth}x${window.innerHeight}`,
    language: navigator.language,
  })
}

/**
 * Log navigation between screens.
 *
 * React Router moves through the History API, so patching it sees every route
 * change without this module reaching into the router - or the router's owner
 * having to know that logging exists.
 */
function installNavigationLog() {
  let current = location.pathname

  const record = (kind) => {
    const to = location.pathname
    if (to === current) return
    const from = current
    current = to
    log.info('navigation', { from, to, kind })
  }

  for (const name of ['pushState', 'replaceState']) {
    const original = history[name]
    if (typeof original !== 'function') continue
    history[name] = function patched(...args) {
      const result = original.apply(this, args)
      record(name === 'pushState' ? 'push' : 'replace')
      return result
    }
  }

  addEventListener('popstate', () => record('back'))
}
