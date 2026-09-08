/**
 * Vocabulary the four monitoring panels share (FR-401, FR-402, FR-403).
 *
 * Every constant here mirrors a value the backend actually produces, and says
 * where: a label invented on the client is a label that silently stops
 * matching the data the day someone adds a signal type.
 */

import { Badge } from '../../components/ui'

/**
 * `timing_flag`, from `signals.FLAG_THRESHOLDS` and `signals.DEFAULT_FLAG`.
 * "wait" is set when the window has not opened yet, which is why it reads as a
 * caution rather than as bad news.
 */
export const TIMING = {
  apply_now: { label: 'Apply now', tone: 'ok' },
  favourable: { label: 'Favourable', tone: 'ok' },
  watch: { label: 'Worth watching', tone: 'info' },
  wait: { label: 'Too early', tone: 'warn' },
  neutral: { label: 'No timing signal', tone: undefined },
}

/** FR-402: only apply_now and favourable are moments worth acting on. */
export const FAVOURABLE = ['apply_now', 'favourable']

export function TimingBadge({ flag }) {
  const entry = TIMING[flag] || TIMING.neutral
  return <Badge tone={entry.tone}>{entry.label}</Badge>
}

/** `notification.severity` — info|action|urgent (migration 052_monitoring). */
export const SEVERITY_TONE = { urgent: 'danger', action: 'warn', info: undefined }

/** `notification.kind` — the five values the notification table documents. */
export const NOTIFICATION_KINDS = [
  { key: 'new_vacancy', label: 'New vacancies' },
  { key: 'signal', label: 'Hiring signals' },
  { key: 'follow_up_due', label: 'Follow-ups due' },
  { key: 'reply', label: 'Replies' },
  { key: 'digest', label: 'Digests' },
]

export const KIND_LABEL = Object.fromEntries(
  NOTIFICATION_KINDS.map((k) => [k.key, k.label]),
)

/** `watchlist.CHANNELS`, with what each one actually reads. */
export const CHANNEL_LABEL = {
  careers: 'Careers page',
  ats: 'ATS board',
  news: 'Newsroom',
  signals: 'Hiring signals',
  filings: 'Filed accounts',
}

export const CHANNEL_HINT = {
  careers: 'The company’s own careers page, and the ATS hiding behind it.',
  ats: 'The applicant-tracking board itself — Greenhouse, Lever, Workday and the rest.',
  news: 'The newsroom or RSS feed, read for events that precede hiring.',
  signals: 'The hiring signals those events produce (FR-225).',
  filings: 'Newly filed annual accounts. Re-read at most every 90 days.',
}

/** `hiring_signal.signal_type`, as `signals.TIMING_MODEL` keys them. */
export function signalLabel(type) {
  if (!type) return 'signal'
  return String(type).replace(/_/g, ' ')
}

/** Whole days between now and an ISO timestamp, in words. */
export function relativeDays(iso) {
  if (!iso) return 'never'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return String(iso)
  const diff = (Date.now() - then) / 86_400_000
  const days = Math.round(Math.abs(diff))
  if (days === 0) return 'today'
  const unit = days === 1 ? 'day' : 'days'
  return diff > 0 ? `${days} ${unit} ago` : `in ${days} ${unit}`
}

/** A count with its label, for the digest's headline figures. */
export function Stat({ label, value, tip }) {
  return (
    <div className="mon-stat">
      <div className="mon-stat-value">{value ?? 0}</div>
      <div className="mon-stat-label">
        {label}
        {tip}
      </div>
    </div>
  )
}
