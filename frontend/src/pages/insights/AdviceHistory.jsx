/**
 * What was already decided (CR-408).
 *
 * Applied and dismissed proposals, kept so a change of direction can be traced
 * back to the figures that prompted it and undone. An applied proposal names
 * the directive-set version it created; reverting is done by restoring the
 * previous version on the directives screen, so the row links there rather
 * than offering a second, competing undo.
 */

import { Link } from 'react-router-dom'

import { HelpTip } from '../../components/Help'
import { Badge, formatDate } from '../../components/ui'
import { DIMENSION_LABELS } from './figures'

export default function AdviceHistory({ history }) {
  if (!history?.length) {
    return (
      <p className="muted small">
        Nothing has been applied or dismissed yet. Once you act on a proposal it is kept here
        with the figures it rested on, so a change of direction stays explainable.
      </p>
    )
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Decision</th>
            <th>Proposal</th>
            <th>Dimension</th>
            <th>Move</th>
            <th className="num">Effect</th>
            <th>
              Result
              <HelpTip term="directive_set_version" align="right" />
            </th>
          </tr>
        </thead>
        <tbody>
          {history.map((a) => (
            <tr key={a.id}>
              <td>
                <Badge tone={a.status === 'applied' ? 'ok' : undefined}>{a.status}</Badge>
                <div className="tiny muted" style={{ marginTop: 3 }}>
                  {formatDate(a.resolved_at)}
                </div>
              </td>
              <td>
                <div className="seg-value">{a.headline}</div>
                {a.status === 'dismissed' && (
                  <div className="tiny muted" style={{ marginTop: 3 }}>
                    {a.dismissed_reason
                      ? `Reason: ${a.dismissed_reason}`
                      : 'No reason recorded.'}
                  </div>
                )}
              </td>
              <td className="small">{DIMENSION_LABELS[a.dimension] || a.dimension}</td>
              <td className="small">
                {a.from_value || '—'} → {a.to_value || '—'}
              </td>
              <td className="num">
                {a.expected_effect == null
                  ? '–'
                  : `${a.expected_effect > 0 ? '+' : a.expected_effect < 0 ? '−' : ''}${Math.abs(
                      Number(a.expected_effect),
                    ).toFixed(1)}`}
              </td>
              <td className="small">
                {a.status === 'applied' && a.applied_directive_set_id ? (
                  <>
                    New directive-set version.{' '}
                    <Link to="/directives">Review or revert</Link>
                  </>
                ) : a.status === 'applied' ? (
                  'Applied.'
                ) : (
                  <span className="muted">Directives unchanged.</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
