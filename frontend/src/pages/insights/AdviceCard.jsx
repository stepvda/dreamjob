/**
 * One redirection proposal (FR-285, FR-148, NFR-305).
 *
 * The proposal is shown next to the figures it was computed from, because the
 * point of `postapp/redirection.py` is that the numbers come first and the
 * model second: the rates, both sample sizes and the confidence label are
 * re-attached server-side from the measured analysis, so what is rendered here
 * is measurement rather than the model's recollection of it.
 *
 * Nothing here applies itself. Accepting writes a NEW directive-set version
 * (FR-148) and leaves the current one intact, which is what makes the change
 * reversible; the button says so before it is pressed.
 */

import { Link } from 'react-router-dom'

import { HelpTip } from '../../components/Help'
import { Badge } from '../../components/ui'
import { DIMENSION_LABELS, pct } from './figures'

const GROUP_LABELS = {
  job_content: 'Job content',
  company_type: 'Company type',
  location: 'Location',
  work_arrangement: 'Work arrangement',
}

const CONFIDENCE_TONE = { suggestive: 'accent', indicative: 'info' }

/** The figures for one side of the move, or for the baseline. */
function Side({ label, figure, muted }) {
  if (!figure) return null
  const [low, high] = figure.interval || []
  return (
    <div className={`advice-side${muted ? ' advice-side-muted' : ''}`}>
      <div className="advice-side-label">{label}</div>
      <div className="advice-side-value">{figure.value || '—'}</div>
      <div className="advice-side-figs">
        <span className="advice-side-rate">{pct(figure.rate)}</span>
        <span className="muted small">
          {' '}
          {figure.successes != null ? `${figure.successes} of ${figure.n}` : `n = ${figure.n}`}
        </span>
      </div>
      {low != null && high != null && (
        <div className="tiny muted">
          plausible range {pct(low)}–{pct(high)}
        </div>
      )}
      {figure.evidence && (
        <div style={{ marginTop: 6 }}>
          <Badge tone={figure.evidence === 'thin' ? undefined : 'info'}>{figure.evidence}</Badge>
        </div>
      )}
    </div>
  )
}

function PatchSentence({ patch }) {
  if (!patch || !patch.group) return null
  const group = GROUP_LABELS[patch.group] || patch.group
  const field = String(patch.field || '').replace(/_/g, ' ')
  const verb =
    patch.action === 'remove' ? 'Removes' : patch.action === 'replace' ? 'Replaces' : 'Adds'
  return (
    <div className="advice-patch">
      <span className="advice-patch-label">
        Change to your directives
        <HelpTip term="directive" />
      </span>
      <span>
        {verb} “{patch.value}”
        {patch.action === 'replace' && patch.replaces ? ` in place of “${patch.replaces}”` : ''}{' '}
        {patch.action === 'remove' ? 'from' : 'to'} {group} · {field}
      </span>
    </div>
  )
}

export default function AdviceCard({ advice, conflict, applied, busy, onApply, onDismiss }) {
  const evidence = advice.evidence || {}
  const effect =
    advice.expected_effect_points != null ? advice.expected_effect_points : advice.expected_effect
  const label = advice.dimension_label || DIMENSION_LABELS[advice.dimension] || advice.dimension

  /* Conflict flags are only on the generate response — see the note in the page. */
  const conflicts = advice.conflicts_with_dream_job ?? conflict?.conflicts_with_dream_job
  const conflictNote = advice.conflict_note || conflict?.conflict_note
  const extrapolated = advice.extrapolated_target ?? conflict?.extrapolated_target

  return (
    <div className={`card advice-card${conflicts ? ' advice-conflict' : ''}`}>
      <div className="card-header">
        <h3>{advice.headline}</h3>
        <div className="spacer" />
        <Badge tone="info">{label}</Badge>
        <Badge tone={CONFIDENCE_TONE[advice.confidence]}>{advice.confidence || 'indicative'}</Badge>
        <Badge>{advice.outcome}</Badge>
      </div>

      {advice.rationale && <p className="advice-rationale">{advice.rationale}</p>}

      {/*
        NFR-305: advice is advisory. The two sets of figures sit side by side so
        the proposal can be judged rather than trusted.
      */}
      <div className="advice-sides">
        <Side label="Moving away from" figure={evidence.from} />
        <Side label="Towards" figure={evidence.to} />
        <Side
          label={`Your overall ${evidence.baseline?.outcome || advice.outcome} rate`}
          figure={
            evidence.baseline
              ? {
                  value: 'All resolved applications',
                  rate: evidence.baseline.rate,
                  n: evidence.baseline.n,
                }
              : null
          }
          muted
        />
      </div>

      {effect != null && (
        <div className="advice-effect-row">
          <span className={`advice-effect ${effect > 0 ? 'seg-up' : effect < 0 ? 'seg-down' : ''}`}>
            {effect > 0 ? '+' : effect < 0 ? '−' : ''}
            {Math.abs(Number(effect)).toFixed(1)}
          </span>
          <span className="small muted">
            percentage points, observed difference between the two segments
            <HelpTip term="expected_effect" />
          </span>
        </div>
      )}

      {extrapolated && (
        <div className="alert alert-info small">
          <div>
            You have not applied to the suggested segment yet, so its rate is a hypothesis
            rather than a measurement. Treat this as something to test.
          </div>
        </div>
      )}

      {/* The data does not overrule what the person said they want (NFR-305). */}
      {conflicts && (
        <div className="alert alert-warn">
          <div>
            <strong>This conflicts with your dream job.</strong>{' '}
            {conflictNote ||
              'What the figures suggest runs against something you said you wanted. The numbers describe what has happened so far; they do not decide what you are looking for.'}
            <div className="small" style={{ marginTop: 6 }}>
              Applying this only changes where the search looks. Your dream-job statement is
              untouched.
            </div>
          </div>
        </div>
      )}

      <PatchSentence patch={advice.directive_patch} />

      {applied ? (
        <div className="alert alert-ok">
          <div>
            <strong>Applied.</strong> Directive set “{applied.name || 'directives'}” is now at
            version {applied.version}. The previous version is untouched — open it to compare or
            revert.{' '}
            <Link to="/directives">Open directives</Link>
          </div>
        </div>
      ) : (
        <div className="row" style={{ marginTop: 12 }}>
          <button
            className="btn btn-primary"
            disabled={busy || !advice.directive_patch}
            onClick={onApply}
          >
            Apply — creates a new directive-set version
          </button>
          <HelpTip term="directive_set_version" />
          <button className="btn" disabled={busy} onClick={onDismiss}>
            Dismiss…
          </button>
          {!advice.directive_patch && (
            <span className="small muted">
              This proposal carries no directive change, so there is nothing to apply. Read it
              and dismiss it.
            </span>
          )}
        </div>
      )}
    </div>
  )
}
