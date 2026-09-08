/**
 * Which model handles which task (FR-362).
 *
 * Split out of ModelsTab because it carries its own data shaping: the task
 * list is the union of what the pipeline can issue, what has actually run,
 * and what an administrator has already routed by hand — and the cost column
 * is llm_by_task summed across the models a task has been through, which is
 * exactly the evidence needed to decide whether routing it cheaper is worth it.
 */

import { eur, num, tokens as fmtTokens } from './format'

/** The task ids the pipeline actually issues (grep of llm.complete call sites). */
const KNOWN_TASKS = [
  'analysis.financial', 'analysis.gap', 'analysis.values', 'classify.reply',
  'company.competitors', 'company.speculative', 'company.values_culture',
  'extract.company', 'extract.contact', 'extract.profile', 'extract.table',
  'extract.vacancy', 'generate.briefing', 'generate.cv', 'generate.email',
  'generate.linkedin', 'generate.motivation', 'interview.mock',
  'normalise.skill', 'plan.campaign', 'plan.stepping_stones',
  'profile.composite', 'profile.dreamjob', 'score.opportunity', 'summarise.page',
]

export default function TaskRoutingTable({ draft, effective, routing, byTask, onChange }) {
  const map = draft.task_models || {}
  const seen = new Set([...KNOWN_TASKS, ...routing.map((r) => r.task), ...Object.keys(map)])
  const routedNow = Object.fromEntries(routing.map((r) => [r.task, r.model]))

  const usage = {}
  for (const row of byTask) {
    const u = usage[row.task] || { calls: 0, tokens: 0, cost: 0, models: new Set() }
    u.calls += row.calls || 0
    u.tokens += row.tokens || 0
    u.cost += row.cost_eur || 0
    if (row.model) u.models.add(row.model)
    usage[row.task] = u
  }

  const options = [
    { value: '', label: `Strong (${effective.model_strong || 'unset'})` },
    { value: effective.model_cheap, label: `Cheap (${effective.model_cheap || 'unset'})` },
    { value: effective.model_strong, label: `Strong (${effective.model_strong || 'unset'})` },
  ].filter((o, i) => i === 0 || o.value)

  function pick(task, value) {
    const next = { ...map }
    if (!value) delete next[task]
    else next[task] = value
    onChange(next)
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Task</th>
            <th>Model</th>
            <th className="nowrap">Resolves to</th>
            <th style={{ textAlign: 'right' }}>Calls</th>
            <th style={{ textAlign: 'right' }}>Tokens</th>
            <th style={{ textAlign: 'right' }}>Cost</th>
          </tr>
        </thead>
        <tbody>
          {[...seen].sort().map((task) => {
            const u = usage[task]
            const chosen = map[task] || ''
            return (
              <tr key={task}>
                <td className="mono">{task}</td>
                <td>
                  <select
                    className="dir-inline-select"
                    value={chosen}
                    onChange={(e) => pick(task, e.target.value)}
                  >
                    {options.map((o) => (
                      <option key={o.label + o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                    {chosen && !options.some((o) => o.value === chosen) && (
                      <option value={chosen}>{chosen}</option>
                    )}
                  </select>
                </td>
                <td className="small muted nowrap">
                  {chosen || routedNow[task] || effective.model_strong || '–'}
                </td>
                <td style={{ textAlign: 'right' }}>{u ? num(u.calls) : '–'}</td>
                <td style={{ textAlign: 'right' }}>{u ? fmtTokens(u.tokens) : '–'}</td>
                <td style={{ textAlign: 'right' }}>{u ? eur(u.cost) : '–'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
