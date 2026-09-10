/**
 * Formatting, tone maps and the one shared tile the campaign screens use.
 *
 * These live apart from the pages because the list and the detail screen have
 * to agree on what a status colour means and on how money is written - a
 * campaign costs cents, and the house formatMoney rounds those away to "EUR 0".
 */

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { formatMoney } from '../../components/ui'

export const STATUS_TONE = {
  draft: undefined,
  planned: 'info',
  running: 'accent',
  paused: 'warn',
  completed: 'ok',
  cancelled: undefined,
  failed: 'danger',
}

export const ITEM_TONE = {
  planned: undefined,
  running: 'accent',
  done: 'ok',
  failed: 'danger',
  skipped: 'info',
  // FR-185: a plan item ends on one of six answers, and only ``failed`` is a
  // defect. Declining a source on principle and finding a board gone are not
  // red, because colouring them red is how a dozen real failures went missing
  // among five hundred expected outcomes.
  blocked: 'info',
  gone: undefined,
  capped: undefined,
}

/**
 * FR-185: the measured outcome of a source, as a badge class. Same rule as
 * above - the failure states carry the danger hue and nothing else does, so
 * that scanning a long per-source list finds the failures.
 */
export const OUTCOME_BADGE = {
  succeeded: 'badge-ok',
  no_matches: '',
  no_work: '',
  skipped: '',
  blocked: 'badge-info',
  refused: 'badge-info',
  robots_disallowed: 'badge-info',
  tos_prohibited: 'badge-info',
  gone: '',
  not_found: '',
  retired: '',
  capped: '',
  failed: 'badge-danger',
  rejected: 'badge-danger',
  normalised_nothing: 'badge-danger',
  extracted_nothing: 'badge-danger',
  // FR-361: the activity feed names two more things a source can be doing, and
  // it reads its severity out of this map so that a line in the log and a badge
  // on the row can never disagree about how serious something is. Neither is a
  // verdict, so neither is coloured.
  started: '',
  cancelled: '',
}

export const ACTION_TONE = { collect: undefined, collect_partial: 'info', skip: 'ok' }

export const ACTION_LABEL = {
  collect: 'collect',
  collect_partial: 'collect the rest',
  skip: 'reuse, do not collect',
}

const EUR_CENTS = new Intl.NumberFormat('en-BE', {
  style: 'currency',
  currency: 'EUR',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

/** Collection costs are cents, and formatMoney rounds those away to "€0". */
export function cost(value) {
  if (value == null) return '–'
  return value >= 100 ? formatMoney(value) : EUR_CENTS.format(value)
}

export function num(value) {
  return (value ?? 0).toLocaleString('en-GB')
}

export function secondsBetween(startIso, endIso) {
  if (!startIso) return null
  const start = new Date(startIso).getTime()
  const end = endIso ? new Date(endIso).getTime() : Date.now()
  if (Number.isNaN(start) || Number.isNaN(end)) return null
  return Math.max(0, (end - start) / 1000)
}

/* --- A labelled number ---------------------------------------------------- */

/**
 * A headline number, built on the theme's `.stat` tile so a campaign figure
 * looks like every other headline number in the application: an icon in the
 * phase hue, the number, its label, and - where the number needs one - a
 * concept tip, a meter or a note underneath.
 */
export function Stat({ icon, label, value, note, tip, phase, children }) {
  return (
    // Not a .card: these sit in a grid, and stacked-card margins would skew it.
    <div className={`stat${phase ? ` ${phase}` : ''}`} style={{ alignItems: 'flex-start' }}>
      {icon && (
        <span className="icon-chip">
          <Icon name={icon} />
        </span>
      )}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="stat-value">{value ?? '\u2013'}</div>
        <div className="stat-label">
          {label}
          {tip && (tip.term ? <HelpTip term={tip.term} /> : <HelpTip title={tip.title}>{tip.body}</HelpTip>)}
        </div>
        {children}
        {note && <div className="cmp-stat-note" style={{ marginTop: 4 }}>{note}</div>}
      </div>
    </div>
  )
}
