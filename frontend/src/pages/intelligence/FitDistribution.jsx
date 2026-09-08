/**
 * How much of what the market offers actually clears the threshold.
 *
 * The ranked list answers "which is best"; this answers "is any of it what I
 * want" - the question FR-382 turns on. It is one distribution of the
 * dream-job fit sub-score across the campaign's own opportunities, drawn as
 * inline SVG, with the threshold as a line rather than a sentence.
 *
 * Two honesty rules, both visible rather than described:
 *  - opportunities with no dream-fit score yet are counted separately and are
 *    never folded into the zero bin, because "not scored" and "scored zero"
 *    are different facts;
 *  - the cleared/scored counts come from the backend's own assessment over the
 *    whole campaign, while the bars are drawn from the page you loaded. Where
 *    the two differ, the caption says so.
 */

import { Link } from 'react-router-dom'

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Empty } from '../../components/ui'

const BIN = 10
const BINS = 100 / BIN

const W = 760
const H = 210
const PAD = { left: 38, right: 14, top: 18, bottom: 34 }
const PLOT_W = W - PAD.left - PAD.right
const PLOT_H = H - PAD.top - PAD.bottom

const x = (score) => PAD.left + (Math.max(0, Math.min(100, score)) / 100) * PLOT_W

export default function FitDistribution({ items, threshold, scored, unscored, cleared, total }) {
  const values = (items || [])
    .map((o) => (o.score_dream_fit == null ? null : Number(o.score_dream_fit)))
    .filter((v) => v != null && !Number.isNaN(v))

  const counts = Array.from({ length: BINS }, () => 0)
  for (const v of values) {
    const index = Math.min(BINS - 1, Math.floor(Math.max(0, Math.min(100, v)) / BIN))
    counts[index] += 1
  }
  const max = Math.max(1, ...counts)
  const limit = threshold == null ? null : Number(threshold)
  const truncated = total != null && items != null && total > items.length

  if (!values.length) {
    return (
      <Empty title="Nothing scored for dream-job fit yet">
        The distribution is drawn from the dream-job fit sub-score on this campaign’s
        opportunities. Recalculate the ranking on the{' '}
        <Link to="/opportunities">opportunities screen</Link> and the shape of the market appears
        here.
      </Empty>
    )
  }

  return (
    <div className="card">
      <div className="card-header">
        <Icon name="chart" />
        <h3>
          What the market is offering, against what you want
          <HelpTip term="dream_job_fit" />
        </h3>
      </div>

      <div className="grid grid-4" style={{ marginBottom: 12 }}>
        <div className="fin-score">
          <div className="fin-chart-title">Clears the threshold</div>
          <div className="stat-value">{cleared ?? 0}</div>
          <div className="tiny muted">at {limit == null ? '–' : Math.round(limit)} or better</div>
        </div>
        <div className="fin-score">
          <div className="fin-chart-title">Scored</div>
          <div className="stat-value">{scored ?? values.length}</div>
          <div className="tiny muted">have a dream-job fit score</div>
        </div>
        <div className="fin-score">
          <div className="fin-chart-title">Not scored</div>
          <div className="stat-value">{unscored ?? 0}</div>
          <div className="tiny muted">no score yet — not the same as a low one</div>
        </div>
        <div className="fin-score">
          <div className="fin-chart-title">Share clearing</div>
          <div className="stat-value">
            {scored ? `${Math.round(((cleared ?? 0) / scored) * 100)}%` : '–'}
          </div>
          <div className="tiny muted">of everything scored</div>
        </div>
      </div>

      <svg
        className="int-chart"
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`Distribution of dream-job fit across ${values.length} scored opportunities, in bands of ${BIN} points. Threshold ${limit == null ? 'not set' : Math.round(limit)}. ${counts
          .map((c, i) => `${i * BIN} to ${(i + 1) * BIN}: ${c}`)
          .join('; ')}.`}
      >
        {/* Colour lives in app.css: var() inside an SVG presentation attribute
            is not reliably honoured, so fills and strokes here are classes. */}
        {[0, 0.5, 1].map((f) => {
          const y = PAD.top + PLOT_H - f * PLOT_H
          return (
            <g key={f}>
              <line className="int-chart-grid" x1={PAD.left} y1={y} x2={W - PAD.right} y2={y} />
              <text className="int-chart-label" x={PAD.left - 6} y={y + 3} textAnchor="end">
                {Math.round(f * max)}
              </text>
            </g>
          )
        })}

        {counts.map((count, i) => {
          const lo = i * BIN
          const barX = x(lo) + 2
          const barW = Math.max(2, x(lo + BIN) - x(lo) - 4)
          const barH = (count / max) * PLOT_H
          const y = PAD.top + PLOT_H - barH
          const clears = limit != null && lo >= limit
          return (
            <g key={i}>
              <rect
                className={`int-bar${clears ? ' is-clear' : ''}`}
                x={barX}
                y={y}
                width={barW}
                height={Math.max(count ? 2 : 0, barH)}
                rx="2"
              />
              {count > 0 && (
                <text className="int-chart-count" x={barX + barW / 2} y={y - 4} textAnchor="middle">
                  {count}
                </text>
              )}
            </g>
          )
        })}

        {limit != null && (
          <g>
            <line
              className="int-threshold"
              x1={x(limit)}
              y1={PAD.top - 6}
              x2={x(limit)}
              y2={PAD.top + PLOT_H}
            />
            <text
              className="int-threshold-label"
              x={Math.min(x(limit) + 5, W - PAD.right - 60)}
              y={PAD.top - 8}
            >
              threshold {Math.round(limit)}
            </text>
          </g>
        )}

        <line
          className="int-chart-axis"
          x1={PAD.left}
          y1={PAD.top + PLOT_H}
          x2={W - PAD.right}
          y2={PAD.top + PLOT_H}
        />
        {[0, 20, 40, 60, 80, 100].map((tick) => (
          <text
            key={tick}
            className="int-chart-label"
            x={x(tick)}
            y={PAD.top + PLOT_H + 15}
            textAnchor="middle"
          >
            {tick}
          </text>
        ))}
        <text
          className="int-chart-label"
          x={PAD.left + PLOT_W / 2}
          y={H - 4}
          textAnchor="middle"
        >
          dream-job fit (0–100)
        </text>
      </svg>

      <p className="tiny muted" style={{ margin: '8px 0 0' }}>
        Bars are counts of opportunities per ten-point band; the band the threshold falls inside
        is shown as below it, since only part of that band clears.{' '}
        {truncated
          ? `Drawn from the ${items.length} highest-fit of ${total} opportunities — everything clearing the threshold is included, the tail is not.`
          : `Drawn from all ${values.length} scored opportunities in this campaign.`}
      </p>
    </div>
  )
}
