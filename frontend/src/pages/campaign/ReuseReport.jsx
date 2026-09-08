import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, SectionCard, formatDuration } from '../../components/ui'

import { ACTION_LABEL, ACTION_TONE, Stat, cost, num } from './shared'

/**
 * FR-342: what is reused rather than re-collected, per entity type, and what
 * that saves. This is an acceptance criterion, so it is shown concretely -
 * counts and money, not a reassuring sentence.
 */
export default function ReuseReport({ report, busy, onRecheck }) {
  const perEntity = report?.per_entity || {}
  const fresh = report?.fresh_in_knowledge_base || {}
  const policy = report?.policy_days || {}
  const rows = Array.from(new Set([...Object.keys(perEntity), ...Object.keys(fresh)])).sort()

  return (
    <SectionCard
      icon="copy"
      title={
        <>
          Reused from the knowledge base
          <HelpTip term="knowledge_base_reuse" />
        </>
      }
      phase="phase-2"
      actions={
        <button className="btn btn-sm" onClick={onRecheck} disabled={busy}>
          {busy ? (
            <span className="spinner" />
          ) : (
            <>
              <Icon name="refresh" /> Re-check
            </>
          )}
        </button>
      }
    >

      {!report ? (
        <p className="small muted" style={{ margin: 0 }}>
          Not assessed yet. Re-check compares this plan against everything already collected — by
          you or by any other search — and skips what is still fresh.
        </p>
      ) : (
        <>
          <p className="small" style={{ marginTop: 0 }}>
            {report.headline}
          </p>

          <div className="grid grid-3" style={{ marginBottom: 14 }}>
            <Stat
              icon="clock"
              label="Collection time saved"
              value={formatDuration(report.estimated_seconds_saved)}
            />
            <Stat icon="money" label="Cost saved" value={cost(report.estimated_cost_saved_eur)} />
            <Stat
              icon="calendar"
              label="Vacancies stay fresh for"
              tip={{ term: 'staleness' }}
              value={policy.vacancy != null ? `${policy.vacancy} days` : '–'}
              note={`companies ${policy.company ?? '–'} d · filed accounts ${policy.financial_year ?? '–'} d`}
            />
          </div>

          <div className="table-wrap" style={{ marginBottom: 14 }}>
            <table>
              <thead>
                <tr>
                  <th>Record type</th>
                  <th className="num">
                    <Icon name="check" /> Fresh in the knowledge base
                  </th>
                  <th className="num">
                    <Icon name="copy" /> Reused for this campaign
                  </th>
                  <th className="num">
                    <Icon name="download" /> Still to collect
                  </th>
                  <th className="num">
                    <Icon name="clock" /> Re-fetched after
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((key) => (
                  <tr key={key}>
                    <td>{key}</td>
                    <td className="num">{num(fresh[key])}</td>
                    <td className="num">{num(perEntity[key]?.reused)}</td>
                    <td className="num">{num(perEntity[key]?.scheduled)}</td>
                    <td className="num">{policy[key] != null ? `${policy[key]} days` : '–'}</td>
                  </tr>
                ))}
                {!rows.length && (
                  <tr>
                    <td colSpan={5} className="muted small">
                      Nothing reusable yet — the full plan will be collected.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {report.decisions?.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>
                      <Icon name="browser" /> Source
                    </th>
                    <th>Decision</th>
                    <th className="num">Reused / expected</th>
                    <th className="num">Pages</th>
                    <th>
                      <Icon name="info" /> Why
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {report.decisions.map((d) => (
                    <tr key={d.plan_item_id}>
                      <td className="small">{d.adapter_key}</td>
                      <td>
                        <Badge tone={ACTION_TONE[d.action]}>
                          {ACTION_LABEL[d.action] || d.action}
                        </Badge>
                      </td>
                      <td className="num">
                        {num(d.reused_records)} / {num(d.expected_records)}
                      </td>
                      <td className="num">
                        {d.pages_before} → {d.pages_after}
                      </td>
                      <td className="small muted">{d.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </SectionCard>
  )
}
