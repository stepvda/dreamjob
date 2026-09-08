/**
 * The Checks tab: the factual-consistency report (FR-322) and the leak scan
 * (NFR-206).
 *
 * This is the tab that decides whether anything may be sent, so it is written
 * to be read rather than skimmed: every claim the checker looked at, whether
 * the profile supports it, and what it was matched against. A `high` finding
 * fails the document, and the approval control on the header says so - the
 * blockers listed here are the same objects the API refuses an approval with.
 */

import { Caution, HelpTip } from '../../components/Help'
import { Badge, Empty } from '../../components/ui'

import { ConsistencyBadge, LeakBadge, SEVERITY_TONE, hardBlockers, softBlockers } from './shared'

const SIGNAL_LABEL = {
  deterministic: 'Matched against the profile',
  judge: 'Flagged by the reviewing model',
}

export default function ChecksPanel({ pkg, onRecheck, busy }) {
  const report = pkg.consistency_report || {}
  const findings = report.findings || []
  const leaks = report.leaks || []
  const summary = report.summary || {}
  const hard = hardBlockers(pkg)
  const soft = softBlockers(pkg)

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="row row-wrap">
        <span className="small muted">
          Factual consistency
          <HelpTip term="factual_consistency" />
        </span>
        <ConsistencyBadge status={pkg.consistency_status} />
        <span className="small muted" style={{ marginLeft: 10 }}>
          Leak scan
          <HelpTip term="leak_scan" />
        </span>
        <LeakBadge status={pkg.leak_scan_status} />
        <div className="spacer" />
        <button className="btn btn-sm" onClick={onRecheck} disabled={busy}>
          {busy ? <span className="spinner" /> : 'Re-run the checks'}
        </button>
      </div>

      {/* FR-322: a failed check blocks approval, and the reason is named here.
          Only a draft can be approved, so a decided package says nothing here. */}
      {pkg.status === 'draft' && hard.length > 0 && (
        <Caution title="This application cannot be approved">
          <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
            {hard.map((b, i) => (
              <li key={i}>{b.detail}</li>
            ))}
          </ul>
          Nothing on this list can be overridden. Correct the text, or regenerate, and re-run the
          checks.
        </Caution>
      )}

      {pkg.status === 'draft' && hard.length === 0 && soft.length > 0 && (
        <Caution title="Approval needs a recorded reason">
          {soft.map((b) => b.detail).join(' ')} You may still approve it, but FR-322 asks you to
          write down why the claim is defensible; the reason is kept in the audit trail with your
          name against it.
          <HelpTip term="approval_override" />
        </Caution>
      )}

      <div className="row row-wrap small muted">
        <span>{report.claims_checked ?? 0} claims checked</span>
        {report.parts?.length > 0 && <span>· across {report.parts.join(', ')}</span>}
        <span>· {summary.high ?? 0} blocking</span>
        <span>· {summary.medium ?? 0} to look at</span>
        <span>· {summary.low ?? 0} minor</span>
        {report.judge_ran ? (
          <span>· a reviewing model read it as a second signal</span>
        ) : (
          <span>· deterministic checks only</span>
        )}
        {report.judge_error && <Badge tone="warn">judge: {report.judge_error}</Badge>}
      </div>

      <FindingTable
        title="Claims in the documents"
        caption="Each claim the checker isolated, and what in your profile it was matched against."
        rows={findings}
        emptyTitle="Every claim traced back to your profile"
        emptyBody="Employers, titles, schools, dates, skills and figures in the generated documents were all found in the profile version this package was built from."
      />

      <FindingTable
        title="Leak scan"
        caption="Content in the documents that is not traceable to material you are entitled to (NFR-206)."
        rows={leaks}
        emptyTitle="Nothing unattributable"
        emptyBody="Every name, address, domain and figure in the generated documents comes from your own profile, or from the company and vacancy this application is for."
      />
    </div>
  )
}

function FindingTable({ title, caption, rows, emptyTitle, emptyBody }) {
  return (
    <section>
      <div className="row" style={{ marginBottom: 6 }}>
        <strong>{title}</strong>
      </div>
      <p className="small muted" style={{ margin: '0 0 8px' }}>
        {caption}
      </p>
      {rows.length === 0 ? (
        <Empty title={emptyTitle}>{emptyBody}</Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Severity</th>
                <th>Claim</th>
                <th>Where</th>
                <th>Supported by</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((f, i) => (
                <tr key={i}>
                  <td>
                    <Badge tone={SEVERITY_TONE[f.severity]}>{f.severity}</Badge>
                  </td>
                  <td>
                    <div>{f.claim}</div>
                    <div className="small muted" style={{ marginTop: 3 }}>
                      {f.detail}
                    </div>
                  </td>
                  <td className="small nowrap">
                    {f.locus}
                    <div className="muted">{f.kind}</div>
                  </td>
                  <td className="small">
                    {f.evidence ? (
                      <span>{f.evidence}</span>
                    ) : (
                      <span className="muted">Nothing in the profile matched it</span>
                    )}
                    <div className="muted" style={{ marginTop: 3 }}>
                      {SIGNAL_LABEL[f.signal] || f.signal}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
