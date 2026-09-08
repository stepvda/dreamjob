/**
 * The five-year financial analysis (FR-243, FR-244, FR-245, NFR-404).
 *
 * Three things, in the order they answer a reader's questions: what was filed,
 * what shape the numbers make, and what the two 0-100 scores conclude from
 * them. The scores are shown with the rationale that cites the figures,
 * because an unexplained 0-100 number invites more trust than it has earned
 * (NFR-305).
 *
 * The trend is drawn as inline SVG - no chart library, and two separate small
 * multiples rather than one chart with two y-axes, since money and people do
 * not share a scale. Every colour is a CSS variable, so it reads in both
 * themes.
 */

import { HelpTip } from '../../components/Help'
import { Badge, Meter, formatPercent } from '../../components/ui'

function compact(value, currency) {
  if (value == null) return '–'
  return new Intl.NumberFormat('en-BE', {
    style: currency ? 'currency' : 'decimal',
    currency: currency || undefined,
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(value)
}

function people(value) {
  if (value == null) return '–'
  return `${Math.round(value)} FTE`
}

/** NFR-404: whatever the filings slice recorded as not reconciling. */
function flagList(flags) {
  if (!flags) return []
  if (Array.isArray(flags)) {
    return flags.map((f) => (typeof f === 'string' ? f : f?.flag || f?.code || f?.message || ''))
  }
  if (typeof flags === 'object') {
    return Object.entries(flags).map(([k, v]) => (v === true ? k : `${k}: ${v}`))
  }
  return [String(flags)]
}

/**
 * One measure over the filed years. Single series, its own axis, its own
 * chart - never a second scale on the same plot.
 */
function TrendChart({ label, points, format, description }) {
  const valued = points.filter((p) => p.value != null)
  if (valued.length < 2) {
    return (
      <div className="fin-chart">
        <div className="fin-chart-title">{label}</div>
        <p className="small muted" style={{ margin: 0 }}>
          Fewer than two filed years carry this figure, so there is no trend to draw.
        </p>
      </div>
    )
  }

  const W = 320
  const H = 132
  const left = 8
  const right = 62
  const top = 14
  const bottom = 26
  const max = Math.max(...valued.map((p) => p.value)) || 1
  const span = points.length > 1 ? points.length - 1 : 1
  const x = (i) => left + ((W - left - right) * i) / span
  const y = (v) => top + (H - top - bottom) * (1 - v / (max * 1.12))

  const line = points
    .map((p, i) => (p.value == null ? null : `${x(i).toFixed(1)},${y(p.value).toFixed(1)}`))
    .filter(Boolean)
    .join(' ')
  const last = valued[valued.length - 1]
  const lastIndex = points.findIndex((p) => p === last)
  // The reference line is labelled only when the last point is not itself the
  // maximum - otherwise the two labels land on the same spot.
  const showMaxLabel = last.value < max * 0.98

  return (
    <div className="fin-chart">
      <div className="fin-chart-title">{label}</div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        height={H}
        role="img"
        aria-label={description}
        preserveAspectRatio="xMidYMid meet"
      >
        {/* Recessive axes: a baseline and one reference line, nothing more. */}
        <line x1={left} y1={y(0)} x2={W - right} y2={y(0)} stroke="var(--line)" strokeWidth="1" />
        <line
          x1={left}
          y1={y(max)}
          x2={W - right}
          y2={y(max)}
          stroke="var(--line)"
          strokeWidth="1"
          strokeDasharray="3 4"
        />
        {showMaxLabel && (
          <text x={W - right + 6} y={y(max) + 4} fontSize="10" fill="var(--ink-3)">
            {format(max)}
          </text>
        )}

        <polyline points={line} fill="none" stroke="var(--accent)" strokeWidth="2" />

        {points.map((p, i) =>
          p.value == null ? null : (
            <circle
              key={p.year}
              cx={x(i)}
              cy={y(p.value)}
              r="4"
              fill={p.estimated ? 'var(--surface)' : 'var(--accent)'}
              stroke={p.estimated ? 'var(--warn)' : 'var(--surface)'}
              strokeWidth="2"
            >
              <title>{`${p.year}: ${format(p.value)}${p.estimated ? ' (estimated)' : ''}`}</title>
            </circle>
          ),
        )}

        {/* One direct label, on the latest year, rather than a number per point. */}
        {last && (
          <text
            x={x(lastIndex) + 9}
            y={y(last.value) + 4}
            fontSize="11"
            fill="var(--ink-2)"
            fontWeight="600"
          >
            {format(last.value)}
          </text>
        )}

        {points.map((p, i) => (
          <text
            key={`t-${p.year}`}
            x={x(i)}
            y={H - 8}
            fontSize="10"
            fill="var(--ink-3)"
            textAnchor="middle"
          >
            {p.year}
          </text>
        ))}
      </svg>
    </div>
  )
}

/**
 * FR-244: a 0-100 score is only usable with the reasoning that produced it, so
 * the rationale is rendered as part of the score rather than behind a click.
 * Where the analysis carries none, the block says what the score is computed
 * from instead of leaving a bare number to be trusted.
 */
function ScoreBlock({ label, term, value, rationale, basis }) {
  return (
    <div className="fin-score">
      <div className="row">
        <strong>
          {label}
          <HelpTip term={term} />
        </strong>
        <div className="spacer" />
      </div>
      <Meter value={value} />
      {value == null ? (
        <p className="small muted" style={{ margin: '8px 0 0' }}>
          Not scored — the filed accounts for this company have not been analysed.
        </p>
      ) : rationale ? (
        <p className="small" style={{ margin: '8px 0 0', lineHeight: 1.55 }}>
          {rationale}
        </p>
      ) : (
        <p className="small muted" style={{ margin: '8px 0 0', lineHeight: 1.55 }}>
          No written reasoning came with this analysis. The score is computed from {basis} across
          the years above — read them before you lean on the number.
        </p>
      )}
    </div>
  )
}

export default function CompanyFinancials({ financials }) {
  const summary = financials || {}
  const years = [...(summary.years || [])].sort(
    (a, b) => (a.fiscal_year || 0) - (b.fiscal_year || 0),
  )
  const currency = years.find((y) => y.currency)?.currency || 'EUR'
  const estimated = years.filter((y) => y.is_estimated)
  const flagged = years.filter((y) => flagList(y.reconciliation_flags).length > 0)

  if (!summary.available || years.length === 0) {
    return (
      <div className="card">
        <div className="card-header">
          <h3>Five-year financial analysis</h3>
        </div>
        <p className="muted" style={{ margin: 0 }}>
          No filed accounts are on record for this company. Financial figures come from the
          registry and filings sources of a campaign; a company profiled from its website alone
          has none, and the ability-to-pay and investment-capacity scores stay unset rather than
          being guessed (FR-241, FR-245).
        </p>
      </div>
    )
  }

  return (
    <div className="stack">
      <div className="card">
        <div className="card-header">
          <h3>Filed years</h3>
          <div className="spacer" />
          {summary.trajectory && (
            <Badge tone={summary.trajectory === 'growing' ? 'ok' : 'info'}>
              {summary.trajectory}
              {summary.trajectory_confidence != null
                ? ` · ${Math.round(summary.trajectory_confidence * 100)}% confidence`
                : ''}
            </Badge>
          )}
        </div>

        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Year</th>
                <th className="num">Revenue</th>
                <th className="num">EBIT</th>
                <th className="num">Net result</th>
                <th className="num">Equity</th>
                <th className="num">Headcount</th>
                <th>
                  Basis
                  <HelpTip term="estimated_figure" />
                </th>
              </tr>
            </thead>
            <tbody>
              {[...years].reverse().map((y) => {
                const flags = flagList(y.reconciliation_flags)
                return (
                  <tr key={y.fiscal_year}>
                    <td>
                      <strong>{y.fiscal_year}</strong>
                    </td>
                    <td className="num">{compact(y.revenue, y.currency || currency)}</td>
                    <td className="num">{compact(y.ebit, y.currency || currency)}</td>
                    <td className="num">{compact(y.net_result, y.currency || currency)}</td>
                    <td className="num">{compact(y.equity, y.currency || currency)}</td>
                    <td className="num">{people(y.headcount_fte)}</td>
                    <td>
                      {/* FR-245: an estimate is never presented as a filed figure. */}
                      {y.is_estimated ? (
                        <Badge tone="warn">estimated</Badge>
                      ) : (
                        <span className="small muted">filed</span>
                      )}
                      {/* NFR-404: figures that did not reconcile say so. */}
                      {flags.map((f) => (
                        <span key={f} style={{ marginLeft: 4 }}>
                          <Badge tone="danger">{f}</Badge>
                        </span>
                      ))}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>

        <div className="row row-wrap" style={{ marginTop: 12 }}>
          <span className="small muted">
            Revenue CAGR <strong>{formatPercent(summary.revenue_cagr)}</strong>
          </span>
          <span className="small muted">
            Headcount CAGR <strong>{formatPercent(summary.headcount_cagr)}</strong>
          </span>
          {estimated.length > 0 && (
            <span className="small">
              <Badge tone="warn">
                {estimated.length} of {years.length} years estimated
              </Badge>
              <HelpTip term="estimated_figure" />
            </span>
          )}
          {flagged.length > 0 && (
            <span className="small">
              <Badge tone="danger">{flagged.length} years with reconciliation flags</Badge>
              <HelpTip term="reconciliation_flag" />
            </span>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Revenue and headcount</h3>
        </div>
        <div className="fin-charts">
          <TrendChart
            label={`Revenue (${currency})`}
            description={`Revenue by fiscal year, ${years[0].fiscal_year} to ${
              years[years.length - 1].fiscal_year
            }`}
            points={years.map((y) => ({
              year: y.fiscal_year,
              value: y.revenue,
              estimated: Boolean(y.is_estimated),
            }))}
            format={(v) => compact(v, currency)}
          />
          <TrendChart
            label="Headcount (FTE)"
            description={`Headcount in full-time equivalents by fiscal year, ${
              years[0].fiscal_year
            } to ${years[years.length - 1].fiscal_year}`}
            points={years.map((y) => ({
              year: y.fiscal_year,
              value: y.headcount_fte,
              estimated: Boolean(y.is_estimated),
            }))}
            format={(v) => `${Math.round(v)}`}
          />
        </div>
        <p className="tiny muted" style={{ margin: '4px 0 0' }}>
          A hollow point is an estimated year (FR-245). Money and people are drawn as two charts
          rather than two axes on one, so neither trend is distorted by the other's scale.
        </p>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>What the figures imply</h3>
        </div>
        <div className="grid grid-2">
          <ScoreBlock
            label="Ability to pay"
            term="ability_to_pay"
            value={summary.ability_to_pay}
            rationale={summary.ability_to_pay_rationale}
            basis="margins, equity, cash and personnel cost per employee"
          />
          <ScoreBlock
            label="Investment capacity"
            term="investment_capacity"
            value={summary.investment_capacity}
            rationale={summary.investment_capacity_rationale}
            basis="cash, debt load and recent capital expenditure"
          />
        </div>
      </div>
    </div>
  )
}
