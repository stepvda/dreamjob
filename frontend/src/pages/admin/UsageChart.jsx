/**
 * Token and cost consumption over time (FR-361, NFR-701).
 *
 * Two small multiples rather than one plot with two axes, for the reason the
 * company financials give: tokens and euros do not share a scale, and a shared
 * axis flatters whichever series happens to be larger. Inline SVG, no library.
 *
 * The series is built from the call log rather than from a dedicated endpoint,
 * because `llm_call.created_at` is the only per-day record the API exposes —
 * `/admin/overview` aggregates by task and loses the time axis entirely.
 */

import { dayKey, eur, tokens as fmtTokens } from './format'

const W = 460
const H = 150
const LEFT = 6
const RIGHT = 54
const TOP = 12
const BOTTOM = 24

/** @param calls  rows from GET /api/admin/llm-calls (newest first) */
export function buildSeries(calls, days = 14) {
  const byDay = new Map()
  for (const c of calls || []) {
    const key = dayKey(c.created_at)
    if (!key) continue
    const bucket = byDay.get(key) || { day: key, tokens: 0, cost: 0, calls: 0, failed: 0 }
    bucket.tokens += (c.input_tokens || 0) + (c.output_tokens || 0)
    bucket.cost += c.cost_eur || 0
    bucket.calls += 1
    if (c.status && c.status !== 'ok') bucket.failed += 1
    byDay.set(key, bucket)
  }
  if (!byDay.size) return []

  // Fill the gaps: a day with no calls is a real zero, and leaving it out
  // would draw a flat line across an outage as though nothing had happened.
  const keys = [...byDay.keys()].sort()
  const first = new Date(`${keys[0]}T00:00:00`)
  const last = new Date(`${keys[keys.length - 1]}T00:00:00`)
  const out = []
  for (let d = new Date(first); d <= last; d.setDate(d.getDate() + 1)) {
    const key = dayKey(d.toISOString())
    out.push(byDay.get(key) || { day: key, tokens: 0, cost: 0, calls: 0, failed: 0 })
  }
  return out.slice(-days)
}

export default function UsageChart({ series }) {
  if (!series.length) return null
  return (
    <div className="fin-charts">
      <Bars
        label="Tokens per day"
        series={series}
        pick={(d) => d.tokens}
        format={fmtTokens}
        description="Tokens consumed per day across every campaign on this installation."
      />
      <Bars
        label="Cost per day"
        series={series}
        pick={(d) => d.cost}
        format={eur}
        description="Estimated cost per day, computed from the configured token prices."
      />
    </div>
  )
}

function Bars({ label, series, pick, format, description }) {
  const max = Math.max(...series.map(pick), 0)
  if (max <= 0) {
    return (
      <div className="fin-chart">
        <div className="fin-chart-title">{label}</div>
        <p className="small muted" style={{ margin: 0 }}>
          Nothing recorded in this window.
        </p>
      </div>
    )
  }

  const plotW = W - LEFT - RIGHT
  const plotH = H - TOP - BOTTOM
  const slot = plotW / series.length
  const barW = Math.max(3, Math.min(26, slot * 0.62))
  const y = (v) => TOP + plotH * (1 - v / (max * 1.1))

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
        <line
          x1={LEFT}
          y1={TOP + plotH}
          x2={W - RIGHT}
          y2={TOP + plotH}
          stroke="var(--line)"
          strokeWidth="1"
        />
        <line
          x1={LEFT}
          y1={y(max)}
          x2={W - RIGHT}
          y2={y(max)}
          stroke="var(--line)"
          strokeWidth="1"
          strokeDasharray="3 4"
        />
        <text x={W - RIGHT + 6} y={y(max) + 4} fontSize="10" fill="var(--ink-3)">
          {format(max)}
        </text>

        {series.map((d, i) => {
          const v = pick(d)
          const h = Math.max(v > 0 ? 1.5 : 0, TOP + plotH - y(v))
          return (
            <rect
              key={d.day}
              x={LEFT + slot * i + (slot - barW) / 2}
              y={TOP + plotH - h}
              width={barW}
              height={h}
              rx="2"
              fill={d.failed ? 'var(--warn)' : 'var(--accent)'}
            >
              <title>
                {`${d.day}: ${format(v)} · ${d.calls} call${d.calls === 1 ? '' : 's'}${
                  d.failed ? `, ${d.failed} failed` : ''
                }`}
              </title>
            </rect>
          )
        })}

        {/* Two dates rather than a label per bar; the rest is in the tooltips. */}
        <text x={LEFT} y={H - 8} fontSize="10" fill="var(--ink-3)">
          {series[0].day.slice(5)}
        </text>
        <text
          x={W - RIGHT}
          y={H - 8}
          fontSize="10"
          fill="var(--ink-3)"
          textAnchor="end"
        >
          {series[series.length - 1].day.slice(5)}
        </text>
      </svg>
    </div>
  )
}
