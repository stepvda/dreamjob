/**
 * The figures card: the headline counts, the caveats, and the segment tables
 * (FR-425, CR-408).
 *
 * The ordering is the argument. Sample size first, then what the numbers
 * cannot support, then the tables — so nobody reaches a percentage before
 * knowing how much is behind it. `analysis.caveats` is rendered above the
 * tables rather than beneath them for exactly that reason.
 */

import { Link } from 'react-router-dom'

import { FirstRun, HelpTip } from '../../components/Help'
import { ErrorBox, Loading, formatDate } from '../../components/ui'
import SegmentFigures, { orderedDimensions, pct } from './figures'

export default function FiguresCard({ patterns, noun, minToAdvise, outstanding, onRecompute }) {
  const analysis = patterns.data || null
  const resolved = analysis?.resolved_size ?? 0
  const sent = analysis?.sample_size ?? 0
  const dims = orderedDimensions(analysis?.segments || {})

  return (
    <div className="card">
      <div className="card-header">
        <h3>
          Outcome rates by kind of job and company
          <HelpTip term="segment" />
        </h3>
        <div className="spacer" />
        {analysis?.computed_at && (
          <span className="small muted">
            Computed {formatDate(analysis.computed_at)}
            {analysis.from_cache ? ' · stored run' : ''}
          </span>
        )}
        <button className="btn btn-sm" disabled={patterns.loading} onClick={onRecompute}>
          Recompute from the latest responses
        </button>
      </div>

      {patterns.loading && <Loading rows={4} />}
      {patterns.error && <ErrorBox error={patterns.error} onRetry={patterns.reload} />}

      {!patterns.loading && !patterns.error && analysis && (
        <>
          <div className="grid grid-4" style={{ marginBottom: 14 }}>
            <div className="stat-tile">
              <div className="stat-value">{resolved}</div>
              <div className="stat-label">
                Resolved applications
                <HelpTip term="resolved" />
              </div>
              <div className="stat-sub">of {sent} sent</div>
            </div>
            <div className="stat-tile">
              <div className="stat-value">{pct(analysis.baseline_rate)}</div>
              <div className="stat-label">
                Overall {noun} rate
                <HelpTip term="baseline_rate" />
              </div>
              <div className="stat-sub">every segment below is compared with this</div>
            </div>
            <div className="stat-tile">
              <div className="stat-value">{dims.length}</div>
              <div className="stat-label">Dimensions with figures</div>
              <div className="stat-sub">a value needs 3 resolved applications to appear</div>
            </div>
            <div className="stat-tile">
              <div className="stat-value">{outstanding}</div>
              <div className="stat-label">Awaiting a recorded response</div>
              <div className="stat-sub">
                <Link to="/responses">record what came back</Link>
              </div>
            </div>
          </div>

          {/* Prominent, not a footnote: this is what stops three points being over-read. */}
          {analysis.caveats?.length > 0 && (
            <div className="alert alert-warn">
              <div>
                <strong>What these figures cannot support</strong>
                <ul className="seg-caveats">
                  {analysis.caveats.map((c, i) => (
                    <li key={i}>{c}</li>
                  ))}
                </ul>
              </div>
            </div>
          )}

          {dims.length > 0 ? (
            <>
              <SegmentFigures analysis={analysis} />
              {analysis.readable && <Highlights readable={analysis.readable} />}
            </>
          ) : (
            /* An empty table would read as a finding of "nothing". Don't show one. */
            <FirstRun
              pathname="/insights"
              title={
                resolved === 0 ? 'Nothing has resolved yet' : 'Not enough in any one segment yet'
              }
              action={
                <Link className="btn btn-primary" to="/responses">
                  Record a response
                </Link>
              }
            >
              {`Rates need resolved applications behind them — one that got a reply, or one that has gone long enough without one to count as silence. You have ${resolved}; a single value has to reach 3 before it appears at all, and ${minToAdvise} have to resolve before any comparison means anything${
                outstanding
                  ? `. ${outstanding} sent ${
                      outstanding === 1 ? 'application is' : 'applications are'
                    } still awaiting a recorded response — recording what came back is what makes these figures move`
                  : ''
              }.`}
            </FirstRun>
          )}
        </>
      )}
    </div>
  )
}

/**
 * The backend's own sentences for the segments that stand out. They are only
 * present on a freshly computed analysis — the cached branch of /patterns
 * returns the stored run, which has no `readable` key — so this renders
 * nothing rather than pretending the analysis said less.
 */
function Highlights({ readable }) {
  const strongest = readable.strongest || []
  const weakest = readable.weakest || []
  if (!strongest.length && !weakest.length) return null

  return (
    <div className="grid grid-2" style={{ marginTop: 16 }}>
      {strongest.length > 0 && (
        <div>
          <h4 className="seg-highlight-head">Answering you more than average</h4>
          <ul className="seg-highlights">
            {strongest.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        </div>
      )}
      {weakest.length > 0 && (
        <div>
          <h4 className="seg-highlight-head">Answering you less than average</h4>
          <ul className="seg-highlights">
            {weakest.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
