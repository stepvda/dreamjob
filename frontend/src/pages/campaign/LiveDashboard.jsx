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

import ActivityLog from './ActivityLog'
import CollectionOutcomes from './CollectionOutcomes'
import SourceList from './SourceList'
import { Stat, cost, num, secondsBetween } from './shared'

export default function LiveDashboard({ live, campaign, busy, onPause, onResume, onCancel }) {
  if (!live) return <Loading rows={4} />

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

      {/* FR-361: the progress bar says how far the run has got; this says what
          it is doing, newest first, with the time of each line. */}
      <ActivityLog
        campaignId={campaign.id}
        running={(live.status ?? campaign.status) === 'running'}
      />

      {/* FR-162, FR-166: one line per adapter, one row per distinct target
          inside it, named by what the item's own query asked for. */}
      <SourceList sources={live.sources} />

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
