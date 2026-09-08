/**
 * FR-382 - stepping-stone paths, drawn as a horizontal progression.
 *
 * The requirement fires on a condition rather than a button: when nothing in
 * the ranked list clears the configured dream-job fit threshold, the useful
 * answer is a route rather than a better sort. The screen therefore leads with
 * the condition - what the threshold is, what the best score against it was -
 * and only then shows the routes.
 *
 * The progression is one inline SVG per path (no chart library): a rail, a
 * numbered node per step and the horizon above it. The reasoning does not fit
 * on a rail, so each step's rationale, companies and linked opportunities sit
 * in a card underneath the node it belongs to.
 */

import { Link } from 'react-router-dom'

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, Empty, Meter } from '../../components/ui'

const KIND_LABEL = { now: 'Reachable now', intermediate: 'Next move', destination: 'Dream job' }
const KIND_TONE = { now: 'ok', intermediate: 'info', destination: 'accent' }

/** "in 2 years" reads better than "24 months" on a rail. */
function horizon(months) {
  if (months == null) return ''
  if (months <= 0) return 'now'
  if (months < 12) return `${months} mo`
  const years = months / 12
  return `${Number.isInteger(years) ? years : years.toFixed(1)} yr`
}

/** SVG has no text wrapping, so the label is broken by hand. */
function wrap(text, perLine = 26, maxLines = 2) {
  const words = String(text || '').split(/\s+/).filter(Boolean)
  const lines = []
  let line = ''
  for (const word of words) {
    if (!line) line = word
    else if (line.length + 1 + word.length <= perLine) line += ` ${word}`
    else {
      lines.push(line)
      line = word
      if (lines.length === maxLines) break
    }
  }
  if (line && lines.length < maxLines) lines.push(line)
  if (lines.length === maxLines && words.join(' ').length > lines.join(' ').length) {
    lines[maxLines - 1] = `${lines[maxLines - 1].slice(0, perLine - 1)}…`
  }
  return lines
}

/**
 * The path as a drawing. Colours come from the phase tokens so the rail agrees
 * with the rest of the screen in both themes.
 */
function PathRail({ steps }) {
  const n = Math.max(steps.length, 1)
  const W = 900
  const H = 108
  const railY = 40
  const cx = (i) => ((i + 0.5) * W) / n

  return (
    <svg
      className="int-rail"
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label={`Path: ${steps.map((s, i) => `step ${i + 1}, ${s.role}, ${horizon(s.horizon_months)}`).join('; ')}`}
    >
      {/* Colour lives in app.css: var() inside an SVG presentation attribute is
          not reliably honoured, so every fill and stroke here is a class. */}
      <line className="int-rail-line" x1={cx(0)} y1={railY} x2={cx(n - 1)} y2={railY} />
      {steps.slice(0, -1).map((_, i) => {
        const midX = (cx(i) + cx(i + 1)) / 2
        return (
          <path
            key={`a${i}`}
            className="int-rail-arrow"
            d={`M ${midX - 5} ${railY - 5} L ${midX + 5} ${railY} L ${midX - 5} ${railY + 5} Z`}
          />
        )
      })}
      {steps.map((step, i) => {
        const x = cx(i)
        const end = i === steps.length - 1 ? ' is-end' : ''
        return (
          <g key={step.position ?? i}>
            <text className="int-rail-horizon" x={x} y={16} textAnchor="middle">
              {horizon(step.horizon_months)}
            </text>
            <circle className={`int-rail-node${end}`} cx={x} cy={railY} r="13" />
            <text className={`int-rail-num${end}`} x={x} y={railY + 4} textAnchor="middle">
              {i + 1}
            </text>
            {wrap(step.role).map((line, li) => (
              <text
                key={li}
                className="int-rail-role"
                x={x}
                y={railY + 26 + li * 14}
                textAnchor="middle"
              >
                {line}
              </text>
            ))}
          </g>
        )
      })}
    </svg>
  )
}

function PathCard({ path, gapLabels }) {
  const steps = [...(path.steps || [])].sort((a, b) => (a.position || 0) - (b.position || 0))
  const basis = path.basis || {}
  /* Stored rows keep `grounded_in` inside `basis`; the compute response has it
     at the top level. Read both rather than showing "undefined". */
  const grounded = path.grounded_in || basis.grounded_in

  return (
    <div className="card int-path">
      <div className="card-header">
        <Icon name="target" />
        <h3>{path.name}</h3>
        <div className="spacer" />
        {grounded === 'dream_job_model' ? (
          <Badge tone="warn">Archetype, not a posting</Badge>
        ) : (
          <Badge tone="info">Built from collected postings</Badge>
        )}
      </div>

      <p className="advice-rationale">{path.rationale}</p>

      <PathRail steps={steps} />

      <div className="int-steps">
        {steps.map((step) => (
          <div key={step.position} className="int-step">
            <div className="row" style={{ marginBottom: 6 }}>
              <Badge tone={KIND_TONE[step.kind]}>{KIND_LABEL[step.kind] || step.kind}</Badge>
            </div>
            <div className="int-step-role">{step.role}</div>
            <p className="tiny muted" style={{ margin: '5px 0 0', lineHeight: 1.5 }}>
              {step.rationale}
            </p>

            {(step.companies || []).length > 0 && (
              <div className="chips" style={{ marginTop: 8 }}>
                {step.companies.map((c, i) => (
                  <span className="chip" key={`${c}-${i}`}>
                    {c}
                  </span>
                ))}
              </div>
            )}

            {(step.opportunity_ids || []).length > 0 && (
              <div className="tiny" style={{ marginTop: 8 }}>
                <span className="muted">Openings behind this step: </span>
                {step.opportunity_ids.map((id, i) => (
                  <span key={id}>
                    {i > 0 && ', '}
                    <Link to={`/opportunities/${id}`}>#{i + 1}</Link>
                  </span>
                ))}
              </div>
            )}

            {(step.closes_gaps || []).length > 0 && (
              <div className="tiny muted" style={{ marginTop: 8 }}>
                Closes: {step.closes_gaps.map((g) => gapLabels[g] || g).join('; ')}
              </div>
            )}
          </div>
        ))}
      </div>

      {basis.best_profile_fit != null && (
        <div className="row row-wrap tiny muted" style={{ marginTop: 10 }}>
          <span>Best profile fit on the first step: {basis.best_profile_fit}</span>
          {basis.candidates != null && <span>· {basis.candidates} candidate roles considered</span>}
        </div>
      )}
    </div>
  )
}

export default function SteppingStones({
  stones,
  runResult,
  gapLabels,
  onRun,
  onSaveThreshold,
  onTag,
  running,
  savingThreshold,
  tagResult,
  thresholdDraft,
  setThresholdDraft,
  hasCampaign,
}) {
  const paths = stones?.paths || []
  const destinations = stones?.destinations || []
  const threshold = stones?.threshold
  const best = stones?.best_dream_fit

  return (
    <>
      <div className="card">
        <div className="card-header">
          <Icon name="dream" />
          <h3>
            Does anything on the market clear the bar?
            <HelpTip term="dream_fit_threshold" />
          </h3>
          <div className="spacer" />
          <button className="btn btn-sm" disabled={running || !hasCampaign} onClick={() => onRun(false)}>
            {running ? <span className="spinner" /> : 'Propose paths'}
          </button>
          <button
            className="btn btn-sm btn-ghost"
            disabled={running || !hasCampaign}
            onClick={() => onRun(true)}
            title="Build the paths even when something already clears the threshold"
          >
            Show the longer route anyway
          </button>
        </div>

        <div className="grid grid-3" style={{ marginBottom: 12 }}>
          <div className="fin-score">
            <div className="fin-chart-title">
              Best dream-job fit
              <HelpTip term="dream_job_fit" />
            </div>
            <Meter value={best} />
          </div>
          <div className="fin-score">
            <div className="fin-chart-title">Your threshold</div>
            <div className="row">
              <input
                type="number"
                min="0"
                max="100"
                step="1"
                value={thresholdDraft}
                onChange={(e) => setThresholdDraft(e.target.value)}
                style={{ width: 84 }}
                aria-label="Dream-job fit threshold"
              />
              <button
                className="btn btn-sm"
                disabled={savingThreshold || thresholdDraft === '' || Number(thresholdDraft) === threshold}
                onClick={() => onSaveThreshold(Number(thresholdDraft))}
              >
                {savingThreshold ? <span className="spinner" /> : 'Save'}
              </button>
            </div>
            <div className="tiny muted" style={{ marginTop: 4 }}>
              FR-382 makes this configurable, and it is yours alone.
            </div>
          </div>
          <div className="fin-score">
            <div className="fin-chart-title">Clearing it now</div>
            <div className="stat-value">{destinations.length}</div>
            <div className="tiny muted">
              of {stones?.scored ?? 0} scored opportunit
              {(stones?.scored ?? 0) === 1 ? 'y' : 'ies'}
              {stones?.unscored ? ` · ${stones.unscored} not yet scored` : ''}
            </div>
          </div>
        </div>

        {stones?.triggered ? (
          <div className="alert alert-warn">
            <div>
              <strong>Nothing on the market clears your threshold.</strong> That is what these
              paths are for: routes that are reachable now and plausibly lead to the job you
              described.
            </div>
          </div>
        ) : destinations.length > 0 ? (
          <div className="alert alert-ok">
            <div>
              <strong>
                {destinations.length} opportunit{destinations.length === 1 ? 'y' : 'ies'} already
                clear{destinations.length === 1 ? 's' : ''} the bar.
              </strong>{' '}
              Stepping stones are not needed — but you can still see the longer route.
              <div className="chips" style={{ marginTop: 8 }}>
                {destinations.slice(0, 8).map((d) => (
                  <Link
                    className="chip clickable"
                    key={d.opportunity_id}
                    to={`/opportunities/${d.opportunity_id}`}
                  >
                    {d.title || 'Opportunity'}
                    {d.company_name ? ` · ${d.company_name}` : ''} ·{' '}
                    {d.dream_fit == null ? '–' : Math.round(d.dream_fit)}
                  </Link>
                ))}
              </div>
            </div>
          </div>
        ) : null}

        {(runResult?.warnings || []).map((w, i) => (
          <div className="alert alert-info" key={i}>
            <div>{w}</div>
          </div>
        ))}

        {/* FR-284: the tags belong to the job seeker, so nothing writes them
            without an explicit request. */}
        {paths.length > 0 && (
          <div className="row row-wrap" style={{ marginTop: 12 }}>
            <button className="btn btn-sm" onClick={onTag}>
              <Icon name="pin" /> Tag these on the ranked list
            </button>
            <span className="tiny muted">
              Adds “destination” and “stepping_stone” to the opportunities behind these paths, and
              leaves every other tag of yours in place.
            </span>
          </div>
        )}

        {tagResult && (
          <div className="alert alert-ok" style={{ marginTop: 10 }}>
            <div>
              Tagged {tagResult.tagged?.destination ?? 0} destination and{' '}
              {tagResult.tagged?.stepping_stone ?? 0} stepping-stone opportunit
              {(tagResult.tagged?.stepping_stone ?? 0) === 1 ? 'y' : 'ies'}.{' '}
              <Link to="/opportunities">Open the ranked list</Link>
            </div>
          </div>
        )}
      </div>

      {paths.length === 0 ? (
        <Empty title="No paths proposed yet">
          {hasCampaign
            ? 'Paths are drawn from this campaign’s own opportunities and from the computed gap analysis, so run the gap analysis first if you have not. A path only counts as a route when the postings behind it describe the gaps that stand in the way.'
            : 'A path is built from a campaign’s collected opportunities. Create and run a campaign first.'}
        </Empty>
      ) : (
        <div className="stack" style={{ marginTop: 14 }}>
          <p className="section-intro" style={{ marginTop: 0 }}>
            Each route is a sequence of roles you could plausibly get next, not a prediction.{' '}
            <HelpTip term="stepping_stone" />
          </p>
          {paths.map((path, i) => (
            <PathCard key={path.id || i} path={path} gapLabels={gapLabels} />
          ))}
        </div>
      )}
    </>
  )
}
