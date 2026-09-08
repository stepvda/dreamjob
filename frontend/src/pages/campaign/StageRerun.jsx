/**
 * Stage re-run controls (NFR-603). Every stage stores what it produced, so one
 * can be run again from the previous stage's output without collecting twice.
 */

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Empty, SectionCard } from '../../components/ui'

export default function StageRerun({ stages, busy, result, disabled, onRerun }) {
  if (!stages.length) {
    return (
      <Empty
        title={
          <>
            <Icon name="refresh" /> No re-runnable stages
          </>
        }
      >
        No pipeline stage has registered itself yet.
      </Empty>
    )
  }
  return (
    <SectionCard
      icon="refresh"
      title={
        <>
          Re-run one stage
          <HelpTip term="stage_rerun" />
        </>
      }
      phase="phase-2"
    >
      <p className="small muted" style={{ marginTop: 0 }}>
        Every stage stores what it produced, so a stage can be run again on its own — re-plan after
        editing your directives, re-score after changing your weights — without collecting anything
        twice.
      </p>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>
                <Icon name="pipeline" /> Stage
              </th>
              <th>What it does</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {stages.map((s) => (
              <tr key={s.stage}>
                <td className="mono">{s.stage}</td>
                <td className="small muted">{s.description || '—'}</td>
                <td>
                  <button
                    className="btn btn-sm"
                    disabled={disabled || busy === `stage:${s.stage}`}
                    title={disabled ? 'Pause or cancel the running collection first' : undefined}
                    onClick={() => onRerun(s.stage)}
                  >
                    {busy === `stage:${s.stage}` ? (
                      <span className="spinner" />
                    ) : (
                      <>
                        <Icon name="refresh" /> Re-run
                      </>
                    )}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {result && (
        <>
          <h4 style={{ marginTop: 16 }}>Last re-run: {result.stage}</h4>
          <pre className="cmp-query">{JSON.stringify(result.result, null, 2)}</pre>
        </>
      )}
    </SectionCard>
  )
}
