/**
 * The live dashboard (FR-361, NFR-502): per-adapter progress, records, outcomes,
 * token consumption, cost, elapsed and estimated remaining, with pause, resume
 * and cancel on the run itself.
 *
 * The headline used to carry an "Errors" tile, and it counted 538 things of
 * which about a dozen were failures. What replaced it is <CollectionOutcomes>,
 * which separates the six answers a source can end on so that the failures are
 * findable rather than buried (FR-185).
 */

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import {
  Badge,
  Empty,
  JobProgress,
  Loading,
  Meter,
  SectionCard,
  formatDate,
  formatDuration,
} from '../../components/ui'

import CollectionOutcomes from './CollectionOutcomes'
import { OUTCOME_BADGE, Stat, cost, num, secondsBetween } from './shared'

export default function LiveDashboard({ live, plan, campaign, busy, onPause, onResume, onCancel }) {
  if (!live) return <Loading rows={4} />

  // The status payload carries no per-source estimate, so the plan supplies it.
  const estimateByItem = Object.fromEntries(
    (plan?.items || []).map((i) => [i.id, { seconds: i.estimated_seconds, caps: i.caps }]),
  )
  const collected = live.collected || {}
  const records = Object.values(collected).reduce((a, b) => a + b, 0)
  const budget = live.budget || {}
  const llm = live.llm || {}
  const ran = secondsBetween(live.started_at || campaign.started_at, live.finished_at)
  const ratio = budget.token_budget ? (budget.tokens_used || 0) / budget.token_budget : 0

  if (!live.started_at && !campaign.started_at) {
    return (
      <Empty
        title={
          <>
            <Icon name="play" /> Not launched yet
          </>
        }
      >
        Progress, records, what each source did, token consumption and cost appear here the
        moment collection starts. Review the plan first.
      </Empty>
    )
  }

  return (
    <>
      <div className="grid grid-4" style={{ marginBottom: 14 }}>
        <Stat
          icon="opportunities"
          label="Records collected"
          value={num(records)}
          note={
            Object.entries(collected)
              .map(([k, v]) => `${v} ${k}`)
              .join(' · ') || 'none yet'
          }
        />
        <Stat
          icon="sparkle"
          label="Tokens used"
          tip={{ term: 'token_budget' }}
          value={num(budget.tokens_used)}
          note={`of ${num(budget.token_budget)} · ${num(llm.calls)} AI calls`}
        >
          <Meter
            value={budget.tokens_used || 0}
            max={budget.token_budget || 1}
            tone={ratio >= 0.9 ? 'danger' : ratio >= 0.8 ? 'warn' : undefined}
          />
        </Stat>
        <Stat
          icon="money"
          label="Cost"
          value={cost(budget.cost_eur)}
          note="fetching plus AI extraction"
        />
        <Stat
          icon="clock"
          label="Elapsed"
          value={formatDuration(ran)}
          note={live.finished_at ? 'finished' : 'running'}
        />
        <Stat
          icon="target"
          label="Estimated remaining"
          value={formatDuration(live.progress?.estimated_seconds_remaining)}
          note={
            live.progress?.pages_total
              ? `${num(live.progress.pages_done)} of ${num(live.progress.pages_total)} pages`
              : `${num(live.progress?.pages_done)} pages done`
          }
        />
      </div>

      {/* FR-185: the six answers a source can end on, with the one that means
          something went wrong given the colour and the top of the block. */}
      <CollectionOutcomes outcomes={live.outcomes} />

      {/* NFR-403: an adapter whose extraction rate has collapsed is reported
          rather than quietly returning less. */}
      {live.adapter_breakage?.length > 0 && (
        <div className="alert alert-warn">
          <Icon name="warning" />
          <div>
            <strong>Extraction quality has dropped.</strong>{' '}
            {live.adapter_breakage
              .map((b) => `${b.adapter_key} (${Math.round(b.extraction_success_rate * 100)}%)`)
              .join(', ')}
            . The site has probably changed shape; results from these sources will be thin until the
            adapter is fixed.
          </div>
        </div>
      )}

      {/* NFR-502: progress, an estimate and a cancel control for the run itself. */}
      <JobProgress
        job={live.job}
        onPause={busy ? undefined : onPause}
        onResume={busy ? undefined : onResume}
        onCancel={busy ? undefined : onCancel}
      />

      <SectionCard
        icon="browser"
        title={
          <>
            Per source
            <HelpTip term="extraction_rate" />
          </>
        }
        phase="phase-2"
        actions={<span className="small muted">{(live.sources || []).length} adapters</span>}
      >
        <div className="col" style={{ gap: 10 }}>
          {(live.sources || []).map((source) => (
            <SourceProgress
              key={source.plan_item_id}
              source={source}
              estimate={estimateByItem[source.plan_item_id]}
            />
          ))}
          {!(live.sources || []).length && (
            <p className="small muted" style={{ margin: 0 }}>
              No source plan items.
            </p>
          )}
        </div>
      </SectionCard>

      {live.jobs?.length > 1 && (
        <SectionCard icon="clock" title="Job history" phase="phase-2">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Kind</th>
                  <th>Status</th>
                  <th className="num">Progress</th>
                  <th className="num">
                    <Icon name="warning" /> Errors
                  </th>
                  <th>
                    <Icon name="calendar" /> Started
                  </th>
                </tr>
              </thead>
              <tbody>
                {live.jobs.map((j) => (
                  <tr key={j.id}>
                    <td>{j.kind}</td>
                    <td>
                      <Badge tone={j.status === 'failed' ? 'danger' : j.status === 'done' ? 'ok' : undefined}>
                        {j.status}
                      </Badge>
                    </td>
                    <td className="num">
                      {num(j.progress_done)}
                      {j.progress_total ? ` / ${num(j.progress_total)}` : ''}
                    </td>
                    <td className="num">{num(j.error_count)}</td>
                    <td className="small muted">{formatDate(j.started_at || j.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </SectionCard>
      )}
    </>
  )
}

/**
 * One adapter's progress, shown through the same component as the campaign job
 * so a source and the run it belongs to read identically (NFR-502).
 */
function SourceProgress({ source, estimate }) {
  const perPage = estimate?.caps?.records_per_page || 10
  const expected = Math.max(1, (source.estimated_pages || 0) * perPage)
  const job = {
    kind: source.display_name || source.adapter_key,
    adapter_key: source.adapter_key,
    status: source.status === 'planned' ? 'pending' : source.status,
    progress_done: source.records_collected || 0,
    progress_total: expected,
    estimated_seconds: estimate?.seconds ?? null,
    error_count: source.error_count || 0,
    last_error: source.last_error,
  }
  return (
    <div>
      <JobProgress job={job} />
      <div className="row row-wrap small muted" style={{ marginTop: 4 }}>
        {source.excluded_by_user && <span className="badge">excluded by you</span>}
        {/* FR-185: the source's own row says which of the six answers it ended
            on, so a line here and the ledger above can never disagree. */}
        {source.outcome && (
          <span className={`badge ${OUTCOME_BADGE[source.outcome] ?? ''}`}>
            {source.outcome.replace(/_/g, ' ')}
          </span>
        )}
        <span>records against an estimate of {num(expected)}</span>
        {source.extraction_success_rate != null && (
          <span>· extraction {Math.round(source.extraction_success_rate * 100)}%</span>
        )}
      </div>
    </div>
  )
}
