/**
 * What the campaign is doing, newest first (FR-361).
 *
 * The progress bar above says how far a four-hour run has got. It cannot say
 * that Ashby's board is gone, that Jobat refused a bot, or that a source
 * collected eight vacancies thirty seconds ago, and those are the things a
 * person watching a run is actually watching for.
 *
 * Six lines are visible and the rest scroll, because this sits between the
 * progress bar and the outcomes ledger and must not push either off the screen.
 * Events are keyed by id and deduped: `since` is inclusive server-side, so the
 * boundary second is deliberately re-delivered rather than silently dropped.
 *
 * Accessibility: the region is labelled and reachable, and `aria-live` is off
 * on purpose. A live region that re-announces six lines every four seconds for
 * the length of a collection run is hostile to a screen reader; a reader who
 * wants the feed can enter it, and a reader who does not is left alone.
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import { SectionCard } from '../../components/ui'

/** FR-361: the same cadence the dashboard itself polls at. */
const POLL_MS = 4000
/** How many lines are kept in memory. Six are visible; the rest are history. */
const KEEP = 200
/** Server clamps this to 1..200; 60 covers a burst between two polls. */
const PAGE = 60
/**
 * The slowest this backs off to while the endpoint keeps failing.
 *
 * A failed poll is not an error state and must not stop the feed - the backend
 * restarts, a proxy hiccups, and the next tick is meant to recover. But a
 * *persistently* failing poll at full cadence is not free: `observeApi` logs a
 * 5xx as an error and `logger.js` flushes errors immediately, so every failed
 * poll costs a second request and a line in `logs/frontend.log`. Migration 134
 * is the concrete case - until the backend restarts and applies it, `/activity`
 * answers 500 to every poll - and at four seconds that is 1,800 failed requests
 * and 1,800 shipped error lines an hour, for a panel that is showing one line
 * of apology. Doubling up to a minute keeps the recovery automatic and makes
 * the standing cost about 1% of that.
 */
const MAX_BACKOFF_MS = 60000

/**
 * Severity as a rail down the left, and nothing else.
 *
 * This is the log half of `OUTCOME_BADGE` in shared.jsx and follows the same
 * restraint: only the states that mean something went wrong are coloured. There
 * is deliberately no green rail for `succeeded` — most lines succeed, and a
 * panel that is entirely coloured says nothing. `blocked` and `gone` get the
 * neutral rail because they are decisions and facts about the world, not
 * defects (FR-185).
 */
const LOG_TONE = {
  failed: 'log-error',
  rejected: 'log-error',
  normalised_nothing: 'log-error',
  extracted_nothing: 'log-error',
  blocked: 'log-info',
  gone: 'log-info',
  cancelled: 'log-warning',
  collection_cancelled: 'log-warning',
}

/** Local wall-clock time, eight characters, to sit in a tabular column. */
function clock(at) {
  if (!at) return '--:--:--'
  const d = new Date(at)
  if (Number.isNaN(d.getTime())) return '--:--:--'
  return d.toTimeString().slice(0, 8)
}

/**
 * Merge a poll's events into what is already on screen.
 *
 * `since` is inclusive, so the newest second arrives twice by design; the id
 * carries the timestamp, so the same event merges onto itself and a restamped
 * one arrives as a new line.
 */
function merge(previous, incoming) {
  const byId = new Map()
  for (const event of previous) byId.set(event.id, event)
  for (const event of incoming) {
    if (event && event.id) byId.set(event.id, event)
  }
  return [...byId.values()]
    .sort((a, b) => {
      if (a.at !== b.at) return a.at < b.at ? 1 : -1
      return a.id < b.id ? 1 : -1
    })
    .slice(0, KEEP)
}

export default function ActivityLog({ campaignId, running }) {
  const [events, setEvents] = useState([])
  // The backend half of this may not be deployed yet, and a campaign that ran
  // before the columns existed has nothing to report. Neither is an error.
  const [unavailable, setUnavailable] = useState(false)
  const cursor = useRef('')
  const boxRef = useRef(null)
  const prevHeight = useRef(0)

  // A different campaign is a different chronology.
  useEffect(() => {
    cursor.current = ''
    setEvents([])
    setUnavailable(false)
  }, [campaignId])

  useEffect(() => {
    if (!campaignId) return undefined
    let alive = true
    let timer = null
    // Full cadence while the endpoint answers; doubling while it does not.
    let delay = POLL_MS

    const stop = () => {
      if (timer) clearTimeout(timer)
      timer = null
    }

    // `setTimeout` rather than `setInterval` so the gap can grow, and so a slow
    // answer is never overlapped by the next request.
    const again = () => {
      if (!alive || !running) return
      stop()
      timer = setTimeout(poll, delay)
    }

    const poll = async () => {
      try {
        const query = `since=${encodeURIComponent(cursor.current)}&limit=${PAGE}`
        const data = await api.get(`/campaigns/${campaignId}/activity?${query}`)
        if (!alive) return
        // A 200 is not proof the endpoint exists. `main.py` serves the SPA for
        // every GET no router claimed, so a backend that has not been restarted
        // since this endpoint was added answers the poll with `index.html` and
        // a 200; `api/client.js` cannot parse that as JSON and hands back the
        // raw HTML *string*. Read through optional chaining that is a feed with
        // no events in it - which is why this panel sat on "nothing recorded
        // yet" while polling a route that was never going to answer. The body
        // has to be this endpoint's shape before it counts as an answer.
        if (!data || typeof data !== 'object' || !Array.isArray(data.events)) {
          setUnavailable(true)
          stop()
          return
        }
        if (typeof data.cursor === 'string') cursor.current = data.cursor
        if (data.events.length) setEvents((prev) => merge(prev, data.events))
        setUnavailable(false)
        delay = POLL_MS // it answered; back to the dashboard's own cadence
      } catch (err) {
        if (!alive) return
        // 404 is both "this endpoint is not deployed yet" and "not your
        // campaign" (FR-101). Either way there is nothing to poll for, so stop
        // rather than issue the same failing request every four seconds.
        if (err?.status === 404 || err?.status === 405) {
          setUnavailable(true)
          stop()
          return
        }
        // Anything else is a dropped poll, not an error state: the next tick
        // retries, and if the run has finished there is no next tick. It just
        // does not keep retrying at full speed - see MAX_BACKOFF_MS.
        delay = Math.min(delay * 2, MAX_BACKOFF_MS)
      }
      again()
    }

    // One fetch on every run of this effect, so that when `running` flips to
    // false the closing milestones still land - without leaving a timer behind.
    poll()

    return () => {
      alive = false
      stop()
    }
  }, [campaignId, running])

  // Newest is on top, so nothing auto-scrolls: the panel already opens where
  // the reader wants it. The one correction needed is for a reader who has
  // scrolled into history - prepending would shift the content under them, so
  // the height the prepend added is given back. An assignment rather than a
  // smooth scroll, so prefers-reduced-motion has nothing to suppress.
  useLayoutEffect(() => {
    const el = boxRef.current
    if (!el) return
    if (el.scrollTop > 0) el.scrollTop += el.scrollHeight - prevHeight.current
    prevHeight.current = el.scrollHeight
  }, [events])

  const lines = useMemo(
    () =>
      events.map((event) => ({
        ...event,
        tone: LOG_TONE[event.kind] || '',
        time: clock(event.at),
      })),
    [events],
  )

  return (
    <SectionCard
      icon="clock"
      title={
        <>
          Activity
          <HelpTip term="activity_log" />
        </>
      }
      phase="phase-2"
      actions={<span className="small muted">{running ? 'live' : 'stopped'}</span>}
    >
      {/* The region scrolls, so it has to be reachable from the keyboard
          (WCAG 2.1.1) - which also gives a screen reader somewhere to enter. */}
      <div
        className="log-view cmp-activity"
        role="log"
        aria-live="off"
        aria-label="Collection activity"
        tabIndex={0}
        ref={boxRef}
      >
        {lines.map((event) => (
          <div key={event.id} className={`log-line ${event.tone}`} title={event.detail || undefined}>
            <span className="log-ts">{event.time}</span>
            {event.source && (
              <span className="log-src" title={event.source}>
                {event.source}
              </span>
            )}
            <span className="log-msg">{event.text || String(event.kind || '').replace(/_/g, ' ')}</span>
          </div>
        ))}
        {!lines.length && (
          <div className="log-line">
            <span className="log-msg is-quiet">
              {unavailable
                ? 'No activity was recorded for this run.'
                : 'Nothing recorded for this run yet.'}
            </span>
          </div>
        )}
      </div>
    </SectionCard>
  )
}
