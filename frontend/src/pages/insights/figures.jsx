/**
 * The figures behind "what works" (FR-425, CR-408).
 *
 * One table per dimension of `GET /api/learning/patterns`. Two design rules
 * come straight out of `postapp/segments.py`, which is written to keep saying
 * that nine applications and one interview is not a pattern:
 *
 *   1. The sample size is given the same visual weight as the percentage. A
 *      50% rate from 2 applications and one from 40 are the same number and
 *      completely different evidence, so `n` is set in the same size as the
 *      rate and carries a bar showing how it compares with the other rows.
 *   2. The plausible range is drawn, not just printed. The band behind each
 *      rate is the Wilson interval: a thin segment gets a band spanning half
 *      the scale, which is the honest picture of what it can support.
 *
 * Segments the backend labelled "thin" (fewer than MIN_SEGMENT_TO_ADVISE
 * resolved applications) are shown for completeness and visibly dimmed - they
 * are excluded from advice, so they must not read like evidence.
 */

import { HelpTip } from '../../components/Help'
import { Badge } from '../../components/ui'

/**
 * Fallback labels. The cached branch of GET /api/learning/patterns returns the
 * stored run only - no `dimension_labels` key - so the screen cannot rely on
 * the server sending them. These mirror segments.DIMENSIONS exactly.
 */
export const DIMENSION_LABELS = {
  function_family: 'kind of work',
  seniority: 'seniority level',
  size_band: 'company size',
  company_stage: 'company stage',
  sector: 'sector',
  work_arrangement: 'work arrangement',
  country: 'country',
  opportunity_kind: 'advertised vs speculative',
  language: 'language of the application',
}

/** The order the brief asks for, rather than whatever order the dict arrives in. */
export const DIMENSION_ORDER = [
  'function_family',
  'seniority',
  'size_band',
  'company_stage',
  'sector',
  'work_arrangement',
  'country',
  'opportunity_kind',
  'language',
]

/** Where a dimension has a glossary entry of its own, tie it to the heading. */
const DIMENSION_TERMS = {
  size_band: 'size_band',
  opportunity_kind: 'speculative_opening',
  function_family: 'role_family',
}

const EVIDENCE_TONE = { suggestive: 'accent', indicative: 'info', thin: undefined }

const clamp01 = (v) => Math.max(0, Math.min(1, Number(v) || 0))

/** A proportion as a CSS length, without floating-point noise in the markup. */
const posPct = (v) => `${(clamp01(v) * 100).toFixed(2)}%`

/** Rates arrive as proportions (0.42). Whole percentages: nothing here supports a decimal. */
export function pct(v) {
  return `${Math.round(clamp01(v) * 100)}%`
}

/** `lift` is a difference of proportions, reported in percentage points. */
export function points(v) {
  const n = Math.round((Number(v) || 0) * 100)
  return `${n > 0 ? '+' : n < 0 ? '−' : ''}${Math.abs(n)} pts`
}

export function orderedDimensions(segments) {
  const present = Object.keys(segments || {}).filter((d) => (segments[d] || []).length)
  const known = DIMENSION_ORDER.filter((d) => present.includes(d))
  return [...known, ...present.filter((d) => !DIMENSION_ORDER.includes(d))]
}

/* --- The interval, drawn ---------------------------------------------------- */

/**
 * The rate on a 0-100% scale: the Wilson interval as a band, the observed rate
 * as a mark inside it, and your overall rate as a dashed line to compare with.
 */
function RateScale({ segment, baseline }) {
  const [low, high] = segment.interval || [0, 0]
  const lo = clamp01(low)
  const hi = Math.max(lo, clamp01(high))
  const tone = segment.lift >= 0.02 ? 'up' : segment.lift <= -0.02 ? 'down' : ''

  return (
    <div className="seg-scale-wrap">
      <div
        className="seg-scale"
        role="img"
        aria-label={`${segment.successes} of ${segment.n} reached the outcome, ${pct(
          segment.rate,
        )}; plausible range ${pct(lo)} to ${pct(hi)}; overall rate ${pct(baseline)}`}
      >
        <span
          className={`seg-band ${tone}`}
          style={{ left: posPct(lo), width: posPct(Math.max(hi - lo, 0.005)) }}
        />
        <span className="seg-baseline" style={{ left: posPct(baseline) }} />
        <span className={`seg-mark ${tone}`} style={{ left: posPct(segment.rate) }} />
      </div>
      <div className="seg-scale-caption">
        <span className="seg-rate">{pct(segment.rate)}</span>
        <span className="muted">
          {pct(lo)}–{pct(hi)}
        </span>
      </div>
    </div>
  )
}

/* --- One dimension ---------------------------------------------------------- */

function DimensionTable({ dimension, label, rows, baseline }) {
  const maxN = rows.reduce((m, s) => Math.max(m, s.n || 0), 1)
  const term = DIMENSION_TERMS[dimension]

  return (
    <section className="seg-dim">
      <div className="seg-dim-head">
        <h4>{label}</h4>
        {term && <HelpTip term={term} />}
        <span className="muted small">
          {rows.length} {rows.length === 1 ? 'value' : 'values'} with at least 3 resolved
          applications
        </span>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Value</th>
              <th className="num">
                Resolved (n)
                <HelpTip term="sample_size" />
              </th>
              <th className="num">Reached</th>
              <th style={{ minWidth: 210 }}>
                Rate and plausible range
                <HelpTip term="plausible_range" />
              </th>
              <th className="num">
                vs overall
                <HelpTip term="baseline_rate" align="right" />
              </th>
              <th>
                Evidence
                <HelpTip term="evidence_label" align="right" />
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((s) => (
              <tr key={`${dimension}:${s.value}`} className={s.evidence === 'thin' ? 'seg-thin' : ''}>
                <td>
                  <span className="seg-value">{s.value}</span>
                  {(s.rejections > 0 || s.no_response > 0) && (
                    <div className="tiny muted" style={{ marginTop: 2 }}>
                      {s.rejections > 0 && `${s.rejections} rejected`}
                      {s.rejections > 0 && s.no_response > 0 && ' · '}
                      {s.no_response > 0 && `${s.no_response} no answer`}
                    </div>
                  )}
                </td>
                <td className="num">
                  {/* The sample size is the point, so it is set as large as the rate. */}
                  <div className="seg-n">{s.n}</div>
                  <div className="seg-nbar" aria-hidden>
                    <span style={{ width: posPct((s.n || 0) / maxN) }} />
                  </div>
                </td>
                <td className="num seg-reached">{s.successes}</td>
                <td>
                  <RateScale segment={s} baseline={baseline} />
                </td>
                <td className={`num ${s.lift > 0 ? 'seg-up' : s.lift < 0 ? 'seg-down' : ''}`}>
                  {points(s.lift)}
                </td>
                <td>
                  <Badge tone={EVIDENCE_TONE[s.evidence]}>{s.evidence}</Badge>
                  {s.evidence === 'thin' && (
                    <div className="tiny muted" style={{ marginTop: 3 }}>
                      excluded from advice
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

/* --- The whole set of figures ---------------------------------------------- */

export default function SegmentFigures({ analysis }) {
  const segments = analysis?.segments || {}
  const labels = { ...DIMENSION_LABELS, ...(analysis?.dimension_labels || {}) }
  const dims = orderedDimensions(segments)
  const baseline = Number(analysis?.baseline_rate) || 0

  return (
    <>
      <div className="seg-legend small muted">
        <span className="seg-legend-scale" aria-hidden>
          <span className="seg-band" style={{ left: '22%', width: '38%' }} />
          <span className="seg-baseline" style={{ left: '50%' }} />
          <span className="seg-mark" style={{ left: '40%' }} />
        </span>
        <span>
          Each row is drawn on a 0–100% scale. The shaded band is the plausible range for
          that rate — wide means few applications. The solid mark is the observed rate; the
          dashed line is your overall {pct(baseline)}.
        </span>
      </div>

      {dims.map((d) => (
        <DimensionTable
          key={d}
          dimension={d}
          label={labels[d] || d}
          rows={segments[d]}
          baseline={baseline}
        />
      ))}
    </>
  )
}
