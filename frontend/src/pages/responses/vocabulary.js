/**
 * The vocabulary of a response, kept in one place because the backend uses two
 * of them and they do not match.
 *
 * ``manual_response.stated_outcome`` takes the seven values in
 * ``response_intake.STATED_OUTCOMES``; the FR-422 classifier that reads a reply
 * emits its own labels for three of the same things. Mapping one onto the other
 * here is what stops the screen reporting a disagreement between "interview"
 * and "interview_invitation" - which is no disagreement at all.
 */

/** manual_response.channel — the values response_intake.CHANNELS accepts. */
export const CHANNELS = [
  { value: 'email', label: 'Email' },
  { value: 'phone', label: 'Phone' },
  { value: 'linkedin', label: 'LinkedIn' },
  { value: 'portal', label: 'ATS portal' },
  { value: 'in_person', label: 'In person' },
  { value: 'other', label: 'Other' },
]

/**
 * manual_response.stated_outcome — response_intake.STATED_OUTCOMES.
 * Ordered as they occur, not by how welcome they are: a rejection sits in the
 * middle of the row and looks like everything around it.
 */
export const OUTCOMES = [
  { value: 'interest', label: 'Interest' },
  { value: 'info_request', label: 'Request for information' },
  { value: 'interview', label: 'Interview invitation' },
  { value: 'rejection', label: 'Rejection' },
  { value: 'referral', label: 'Referral' },
  { value: 'auto_reply', label: 'Automatic reply' },
  { value: 'other', label: 'Something else' },
]

/** FR-422 classifier labels, mapped onto the intake vocabulary they mean. */
const CLASSIFIER_ALIASES = {
  interview_invitation: 'interview',
  request_for_information: 'info_request',
  automatic_reply: 'auto_reply',
}

/** segments.analyse_segments(silence_days=21): when silence counts as an answer. */
export const SILENCE_DAYS = 21
/** segments.MIN_SEGMENT_TO_ADVISE: below this the analysis says nothing at all. */
export const MIN_TO_ANALYSE = 6

const OUTCOME_LABELS = Object.fromEntries(OUTCOMES.map((o) => [o.value, o.label]))
const CHANNEL_LABELS = Object.fromEntries(CHANNELS.map((c) => [c.value, c.label]))

const own = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key)

/** A classifier label or a stated outcome, as one comparable value. */
export function normaliseOutcome(value) {
  if (!value) return null
  return own(CLASSIFIER_ALIASES, value) ? CLASSIFIER_ALIASES[value] : value
}

/** How a hand-entered response arrived. */
export function channelLabel(value) {
  if (!value) return 'recorded by hand'
  return own(CHANNEL_LABELS, value) ? CHANNEL_LABELS[value] : String(value)
}

/** What to show a person. An unknown label is shown, not swallowed. */
export function outcomeLabel(value) {
  const v = normaliseOutcome(value)
  if (!v) return null
  return own(OUTCOME_LABELS, v) ? OUTCOME_LABELS[v] : String(v).replace(/_/g, ' ')
}

export function daysSince(iso) {
  if (!iso) return null
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return null
  return Math.max(0, Math.floor((Date.now() - then) / 86_400_000))
}

export function todayISODate() {
  const now = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}
