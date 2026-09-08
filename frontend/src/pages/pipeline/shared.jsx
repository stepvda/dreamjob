/**
 * Vocabulary shared by the pipeline board, its card detail, the rehearsal and
 * the negotiation brief.
 *
 * The five stages and the four outcomes are not a UI invention: they are the
 * vocabulary `dreamjob.postapp.board` validates against (FR-421), so they are
 * written once here rather than retyped in each view. Getting one of these
 * strings wrong is a 422 from the API, not a cosmetic problem.
 */

/** FR-421: the board's columns, in the order an application moves through them. */
export const STAGES = [
  {
    key: 'sent',
    label: 'Sent',
    hint: 'Applied, nothing back yet',
  },
  {
    key: 'replied',
    label: 'Replied',
    hint: 'Answered, but not yet a decision',
  },
  {
    key: 'interview',
    label: 'Interview',
    hint: 'Invited to talk',
  },
  {
    key: 'offer',
    label: 'Offer',
    hint: 'A number is on the table',
  },
  {
    key: 'closed',
    label: 'Closed',
    hint: 'Ended, one way or the other',
  },
]

export const STAGE_LABEL = Object.fromEntries(STAGES.map((s) => [s.key, s.label]))

/** FR-421: only a closed card carries an outcome. */
export const OUTCOMES = [
  { value: 'accepted', label: 'Accepted — I took the job' },
  { value: 'rejected', label: 'Rejected — they said no' },
  { value: 'withdrawn', label: 'Withdrawn — I pulled out' },
  { value: 'no_response', label: 'No response — it went silent' },
]

export const OUTCOME_TONE = {
  accepted: 'ok',
  rejected: 'danger',
  withdrawn: undefined,
  no_response: 'warn',
}

/** FR-444 applies from the interview stage onwards. */
export const NEGOTIABLE_STAGES = ['interview', 'offer']

/** Reply classes as `dreamjob.postapp.reply_classifier.CLASSES` spells them. */
export const CLASSIFICATION_LABEL = {
  interview_invitation: 'Interview invitation',
  interest: 'Interest',
  request_for_information: 'Request for information',
  rejection: 'Rejection',
  referral: 'Referral',
  automatic_reply: 'Automatic reply',
  other: 'Other',
}

export const CLASSIFICATION_TONE = {
  interview_invitation: 'ok',
  interest: 'ok',
  request_for_information: 'info',
  rejection: 'danger',
  referral: 'info',
  automatic_reply: undefined,
  other: undefined,
}

export const DRAFT_TONE = {
  draft: 'warn',
  edited: 'warn',
  approved: 'ok',
  sent: 'info',
  discarded: undefined,
}

/** What moved a card, in the words `pipeline_card_event.trigger` uses. */
export const TRIGGER_LABEL = {
  user: 'You moved it',
  reply_detection: 'A reply moved it',
  system: 'Opened by the system',
}

export function isOverdue(iso) {
  return Boolean(iso) && iso < new Date().toISOString()
}

/** An ISO stamp as `<input type="date">` wants it. */
export function toDateInput(iso) {
  return iso ? String(iso).slice(0, 10) : ''
}

/**
 * A date field back into the shape every TEXT date column in this database
 * holds — `datetime.now(UTC).isoformat(timespec='seconds')`. The comparisons
 * the board makes on `next_action_due` are string comparisons, so a due date
 * written in any other shape sorts wrongly rather than failing loudly.
 *
 * An emptied field sends `''`, not `null`: `board.update_card` skips any value
 * that arrives as `None`, so a null would silently leave the old date in
 * place, while the empty string is normalised to NULL and actually clears it.
 */
export function fromDateInput(value) {
  return value ? `${value}T09:00:00+00:00` : ''
}

/**
 * The negotiation brief arrives in two shapes: the stored row from
 * `GET /negotiation/{opportunity_id}` and the generator's own return value
 * from `POST`. They carry the same case under different keys, so both are
 * flattened here rather than at three call sites.
 */
export function normaliseBrief(raw) {
  if (!raw) return null
  const figures = raw.figures || raw.inputs || {}
  const caseBlock = raw.case || {}
  const market = raw.market_data || {
    min: figures.market_min,
    max: figures.market_max,
    median: figures.market_median,
    method: figures.market_method,
    confidence: figures.market_confidence,
    sample_size: figures.market_sample,
  }
  return {
    opportunityId: raw.opportunity_id,
    stage: raw.stage || null,
    currency: raw.currency || figures.currency || 'EUR',
    suggestedAsk: raw.suggested_ask || null,
    askMin: raw.ask_min ?? figures.ask_min ?? null,
    askMax: raw.ask_max ?? figures.ask_max ?? null,
    walkAway: raw.walk_away ?? figures.walk_away ?? null,
    confidence: raw.confidence ?? figures.confidence ?? null,
    abilityToPay: figures.ability_to_pay ?? null,
    abilityRationale: figures.ability_to_pay_rationale || '',
    personnelCostPerFte: figures.personnel_cost_per_fte ?? null,
    financialsEstimated: Boolean(figures.financials_estimated),
    anchorRationale: figures.anchor_rationale || '',
    gaps: figures.gaps || [],
    market,
    arguments: raw.arguments || caseBlock.arguments || [],
    fallbacks: raw.fallbacks || caseBlock.fallbacks || [],
    risks: caseBlock.risks || [],
    ifPushed: caseBlock.if_pushed || '',
    openingLine: caseBlock.opening_line || '',
    generatedBy: caseBlock.generated_by || null,
    advisory: raw.advisory || null,
    pdfPath: raw.pdf_path || null,
    updatedAt: raw.updated_at || raw.created_at || null,
  }
}
