/**
 * The gate: every claim, and whether the profile supports it (FR-322, NFR-206).
 *
 * Two different checks are shown together because they answer two different
 * fears, and a job seeker has both. The factual-consistency check asks whether
 * the CV says anything your profile does not support — an employer you never
 * worked for, a date that moved, a title that grew. The leak scan asks whether
 * anything in the generated text is not traceable to *you* at all, which is
 * what would happen if another person's material ever reached this document.
 *
 * The difference between them is not cosmetic and the panel does not blur it:
 * a consistency failure can be overridden by a person who has read the finding
 * and recorded why, and a leak cannot be overridden by anybody. That is read
 * off `blockers[].overridable`, which the backend computes, rather than being
 * decided again here.
 *
 * A finding names what it was matched against. "Unsupported" with no evidence
 * is an accusation; with the evidence beside it, it is something you can
 * settle in ten seconds.
 */

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge } from '../../components/ui'

import { ConsistencyBadge, LeakBadge, SEVERITY_TONE, hardBlockers, softBlockers } from './shared'

export default function ChecksTab({ detail, busy, onRecheck }) {
  const consistency = detail.consistency || {}
  const report = consistency.report || {}
  const findings = report.findings || []
  const leaks = report.leaks || []
  const pkg = detail.package || {}
  const hard = hardBlockers(pkg)
  const soft = softBlockers(pkg)

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="apl-meta">
        <span className="muted">
          Factual consistency
          <HelpTip term="factual_consistency" />
        </span>
        <span>
          <ConsistencyBadge status={consistency.status} />
          <span className="small muted">
            {report.claims_checked ?? 0} claims read
            {report.parts?.length > 0 && ` across ${report.parts.join(', ')}`}
          </span>
        </span>

        <span className="muted">
          Leak scan
          <HelpTip term="leak_scan" />
        </span>
        <span>
          <LeakBadge status={consistency.leak_scan_status} />
          <span className="small muted">
            {leaks.length === 0
              ? 'nothing in the text is untraceable to you'
              : `${leaks.length} passage${leaks.length === 1 ? '' : 's'} could not be traced`}
          </span>
        </span>

        <span className="muted">Second reading</span>
        <span>
          {report.judge_ran ? (
            <Badge tone="info">a model read it too</Badge>
          ) : (
            <span className="muted">deterministic checks only</span>
          )}
          {report.judge_error && <span className="small muted">{report.judge_error}</span>}
        </span>

        <span className="muted">Checked</span>
        <span className="small muted">{report.checked_at || 'not yet'}</span>
      </div>

      {(hard.length > 0 || soft.length > 0) && (
        <div className={`alert ${hard.length ? 'alert-danger' : 'alert-warn'}`}>
          <div style={{ flex: 1 }}>
            <strong>
              {hard.length > 0
                ? 'This one cannot be sent as it stands.'
                : 'This one needs your judgement before it goes.'}
            </strong>
            <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
              {[...hard, ...soft].map((b, i) => (
                <li key={i}>
                  {b.detail}{' '}
                  <span className="tiny muted">
                    {b.overridable
                      ? '— you may override this with a recorded reason'
                      : '— not overridable'}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      <FindingList
        title="Claims the profile does not support"
        empty="Every claim in the generated documents traces back to something in your profile."
        findings={findings}
      />

      <FindingList
        title="Passages that could not be traced to you"
        empty="Nothing in the text came from outside your own material."
        findings={leaks}
      />

      <div className="row row-wrap">
        <button className="btn" disabled={busy === 'check'} onClick={onRecheck}>
          {busy === 'check' ? <span className="spinner" /> : 'Run the checks again'}
        </button>
        <span className="small muted">
          Re-reads the current text. Editing the email or regenerating a document runs them again
          by itself.
        </span>
      </div>
    </div>
  )
}

function FindingList({ title, empty, findings }) {
  return (
    <div>
      <h4>{title}</h4>
      {findings.length === 0 ? (
        <p className="small muted" style={{ margin: 0 }}>
          <Icon name="check" /> {empty}
        </p>
      ) : (
        <div className="col" style={{ gap: 8 }}>
          {findings.map((f, i) => (
            <div key={i} className={`finding finding-${f.severity || 'low'}`}>
              <div className="row row-wrap" style={{ gap: 6 }}>
                <Badge tone={SEVERITY_TONE[f.severity]}>{f.severity}</Badge>
                <Badge>{f.kind}</Badge>
                <span className="tiny muted">in the {f.locus}</span>
                <div className="spacer" />
                <span className="tiny muted">{f.signal}</span>
              </div>
              <p style={{ margin: '6px 0 4px' }}>
                <strong>“{f.claim}”</strong>
              </p>
              <p className="small" style={{ margin: 0 }}>
                {f.detail}
              </p>
              {f.evidence && (
                <p className="small muted" style={{ margin: '4px 0 0' }}>
                  Matched against: {f.evidence}
                </p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
