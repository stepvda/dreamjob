/**
 * What this installation has actually done (FR-361, NFR-701).
 *
 * Three altitudes, in the order an operator reads them: the platform counters,
 * consumption over time, then one campaign in full. The campaign dashboard is
 * the FR-361 surface proper — stage progress, records per source, errors,
 * tokens, cost and a remaining-time estimate that says what it is based on
 * rather than pretending to precision it does not have.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { FirstRun, HelpTip } from '../../components/Help'
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  SectionCard,
  Stat,
  formatDuration,
  useFetch,
} from '../../components/ui'
import { eur, num, stamp, tokens as fmtTokens } from './format'

import UsageChart, { buildSeries } from './UsageChart'

const ESTIMATE_BASIS = {
  observed_rate: 'extrapolated from the rate of work already done',
  planner_estimate: "the planner's own estimate — no progress reported yet",
  finished: 'finished',
  unknown: 'not enough information to estimate',
}

export default function ActivityTab({ onGoToSources }) {
  const overview = useFetch(() => api.get('/admin/overview'), [])
  // 500 is the endpoint's ceiling; enough for a fortnight on any real install.
  const calls = useFetch(() => api.get('/admin/llm-calls?limit=500'), [])
  const [selected, setSelected] = useState(null)

  if (overview.loading) return <Loading rows={6} />
  if (overview.error) return <ErrorBox error={overview.error} onRetry={overview.reload} />

  const counters = overview.data?.counters || {}
  const campaigns = overview.data?.campaigns || []
  const adapters = overview.data?.adapters || []
  const series = buildSeries(calls.data?.items || [])

  // Nothing has run at all: the empty state teaches rather than reports.
  if (!campaigns.length && !counters.llm_calls) {
    return (
      <FirstRun
        pathname="/admin"
        title="Nothing has run on this installation yet"
        action={
          <button className="btn btn-primary" onClick={onGoToSources}>
            Review the source catalogue
          </button>
        }
      >
        Job runs, records per source, errors, tokens and cost all appear here once a
        campaign has run. Until then the two tabs worth your time are Models — where the
        provider, the per-task model and the token budget are set — and Sources, where you
        decide which adapters may be used and on what terms.
      </FirstRun>
    )
  }

  return (
    <div className="stack">
      <div className="grid grid-4">
        <Stat icon="profile" label="Job seekers" value={num(counters.seekers)} phase="phase-0" />
        <Stat icon="campaign" label="Campaigns" value={num(counters.campaigns)} phase="phase-0" />
        <Stat
          icon="companies"
          label="Companies known"
          value={num(counters.companies)}
          phase="phase-0"
        />
        <Stat
          icon="vacancy"
          label="Vacancies collected"
          value={num(counters.vacancies)}
          phase="phase-0"
        />
        <Stat icon="document" label="Pages fetched" value={num(counters.pages_fetched)} phase="phase-0" />
        <Stat icon="sparkle" label="AI calls" value={num(counters.llm_calls)} phase="phase-0" />
        <Stat icon="chart" label="Tokens" value={fmtTokens(counters.tokens)} phase="phase-0" />
        <Stat icon="money" label="Cost" value={eur(counters.cost_eur)} phase="phase-0" />
      </div>

      {counters.failed_jobs > 0 && (
        <div className="alert alert-warn">
          <div>
            <strong>
              {num(counters.failed_jobs)} job run{counters.failed_jobs === 1 ? '' : 's'} failed.
            </strong>{' '}
            Open the campaign
            below to see which stage and which adapter, or check the extraction rates on
            the Sources tab — a failed run is usually a source that changed shape.
          </div>
        </div>
      )}

      <SectionCard
        icon="chart"
        title={
          <>
            Consumption over time
            <HelpTip
              title="Where these figures come from"
              align="right"
            >
              Built from the AI call log, bucketed by the day each call was made. A day
              with no calls is drawn as a real zero rather than skipped. Cost is computed
              from the token prices set on the Models tab, not fetched from the provider.
            </HelpTip>
          </>
        }
        phase="phase-0"
      >
        {calls.loading ? (
          <Loading rows={2} />
        ) : calls.error ? (
          <ErrorBox error={calls.error} onRetry={calls.reload} />
        ) : series.length ? (
          <UsageChart series={series} />
        ) : (
          <p className="small muted" style={{ margin: 0 }}>
            No AI calls have been recorded yet, so there is nothing to plot.
          </p>
        )}
      </SectionCard>

      <SectionCard icon="opportunities" title="Records per source" phase="phase-0">
        <SourceActivity adapters={adapters} />
      </SectionCard>

      <SectionCard icon="campaign" title="Campaigns" phase="phase-0">
        {!campaigns.length ? (
          <Empty title="No campaigns yet">
            The counters above come from work that ran outside a campaign — profile intake
            or company research.
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Campaign</th>
                  <th>Status</th>
                  <th>Stage</th>
                  <th style={{ textAlign: 'right' }}>Records</th>
                  <th style={{ textAlign: 'right' }}>
                    Budget
                    <HelpTip term="token_budget" align="right" />
                  </th>
                  <th style={{ textAlign: 'right' }}>Cost</th>
                  <th style={{ textAlign: 'right' }}>Failed jobs</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {campaigns.map((c) => (
                  <tr key={c.id}>
                    <td>
                      <div style={{ fontWeight: 600 }}>{c.name}</div>
                      <div className="tiny muted">{stamp(c.created_at)}</div>
                    </td>
                    <td>
                      <Badge tone={c.status === 'running' ? 'accent' : c.status === 'failed' ? 'danger' : undefined}>
                        {c.status}
                      </Badge>
                    </td>
                    <td className="small">{c.stage || '–'}</td>
                    <td style={{ textAlign: 'right' }}>{num(c.records_collected)}</td>
                    <td style={{ textAlign: 'right', minWidth: 120 }}>
                      <Bar
                        fraction={c.token_budget ? c.tokens_used / c.token_budget : 0}
                        tone={
                          c.token_budget && c.tokens_used / c.token_budget > 0.85
                            ? 'warn'
                            : undefined
                        }
                      />
                      <div className="tiny muted">
                        {fmtTokens(c.tokens_used)} / {fmtTokens(c.token_budget)}
                      </div>
                    </td>
                    <td style={{ textAlign: 'right' }}>{eur(c.cost_eur)}</td>
                    <td style={{ textAlign: 'right' }}>
                      {c.failed_jobs ? <Badge tone="danger">{c.failed_jobs}</Badge> : '–'}
                    </td>
                    <td style={{ textAlign: 'right' }}>
                      <button
                        className="btn btn-sm"
                        onClick={() => setSelected(selected === c.id ? null : c.id)}
                      >
                        {selected === c.id ? 'Hide' : 'Open'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </SectionCard>

      {selected && <CampaignDashboard campaignId={selected} />}
    </div>
  )
}

/**
 * A proportion bar with no number of its own. `Meter` prints its value, which
 * would put a percentage next to the count it is illustrating — two numbers
 * saying the same thing, one of them meaningless on its own.
 */
function Bar({ fraction, tone }) {
  const pct = Math.max(0, Math.min(100, (fraction || 0) * 100))
  return (
    <div className="meter-track">
      <div className={`meter-fill ${tone || ''}`} style={{ width: `${pct}%` }} />
    </div>
  )
}

/* --- Per-adapter collection (NFR-403, NFR-701) ----------------------------- */

function SourceActivity({ adapters }) {
  const used = adapters.filter((a) => a.plan_items || a.records_collected || a.errors)
  if (!used.length) {
    return (
      <Empty title="No source has collected anything yet">
        Once a campaign runs, each adapter reports what it fetched and what it could not
        read. A rate that drops sharply usually means the site changed shape.
      </Empty>
    )
  }
  const max = Math.max(...used.map((a) => a.records_collected || 0), 1)
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Adapter</th>
            <th>Records collected</th>
            <th style={{ textAlign: 'right' }}>Plan items</th>
            <th style={{ textAlign: 'right' }}>Errors</th>
            <th className="nowrap">Last success</th>
          </tr>
        </thead>
        <tbody>
          {used.map((a) => (
            <tr key={a.adapter_key}>
              <td>
                <div style={{ fontWeight: 600 }}>{a.display_name}</div>
                <div className="mono tiny muted">{a.adapter_key}</div>
              </td>
              <td style={{ minWidth: 160 }}>
                <Bar fraction={(a.records_collected || 0) / max} tone="ok" />
                <div className="tiny muted">{num(a.records_collected)}</div>
              </td>
              <td style={{ textAlign: 'right' }}>{num(a.plan_items)}</td>
              <td style={{ textAlign: 'right' }}>
                {a.errors ? <Badge tone="warn">{a.errors}</Badge> : '–'}
              </td>
              <td className="small muted nowrap">{stamp(a.last_success_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/* --- One campaign in full (FR-361) ----------------------------------------- */

function CampaignDashboard({ campaignId }) {
  const dash = useFetch(() => api.get(`/admin/campaigns/${campaignId}/dashboard`), [campaignId])

  if (dash.loading) return <Loading rows={4} />
  if (dash.error) return <ErrorBox error={dash.error} onRetry={dash.reload} />

  const d = dash.data
  const t = d.timing || {}
  const tk = d.tokens || {}

  return (
    <SectionCard
      icon="pipeline"
      title={`${d.campaign?.name || 'Campaign'} — stages and errors`}
      phase="phase-0"
      actions={<Badge tone={d.campaign?.status === 'running' ? 'accent' : undefined}>{d.campaign?.status}</Badge>}
    >
      <div className="row row-wrap" style={{ marginBottom: 12 }}>
        <Badge>{num(d.records_total)} records</Badge>
        <Badge>{num(tk.calls)} AI calls</Badge>
        <Badge>{fmtTokens(tk.used)} of {fmtTokens(tk.budget)} tokens</Badge>
        <Badge tone="accent">{eur(d.cost_eur)}</Badge>
        {tk.failed_calls > 0 && <Badge tone="danger">{tk.failed_calls} failed calls</Badge>}
        {tk.avg_latency_ms > 0 && <span className="small muted">{num(tk.avg_latency_ms)} ms average latency</span>}
      </div>

      <p className="small muted" style={{ marginTop: 0 }}>
        Ran for {formatDuration(t.elapsed_seconds)}
        {t.estimated_remaining_seconds != null && t.estimate_basis !== 'finished'
          ? `, about ${formatDuration(t.estimated_remaining_seconds)} remaining`
          : ''}{' '}
        — {ESTIMATE_BASIS[t.estimate_basis] || t.estimate_basis}.
      </p>

      <div className="table-wrap" style={{ marginBottom: 14 }}>
        <table>
          <thead>
            <tr>
              <th>Stage</th>
              <th>Adapter</th>
              <th>Status</th>
              <th>Progress</th>
              <th style={{ textAlign: 'right' }}>Errors</th>
            </tr>
          </thead>
          <tbody>
            {(d.stages || []).map((s) => (
              <tr key={s.job_id}>
                <td className="small">{s.kind}</td>
                <td className="mono tiny">{s.adapter_key || '–'}</td>
                <td>
                  <Badge tone={s.status === 'failed' ? 'danger' : s.status === 'running' ? 'accent' : undefined}>
                    {s.status}
                  </Badge>
                </td>
                <td style={{ minWidth: 140 }}>
                  <Bar fraction={s.total ? s.done / s.total : 0} />
                  <div className="tiny muted">
                    {num(s.done)}
                    {s.total ? ` / ${num(s.total)}` : ''}
                  </div>
                </td>
                <td style={{ textAlign: 'right' }}>
                  {s.errors ? <Badge tone="warn">{s.errors}</Badge> : '–'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {(d.errors || []).length > 0 && (
        <div className="alert alert-warn">
          <div style={{ flex: 1 }}>
            <strong>Errors during this campaign</strong>
            <ul className="help-tips" style={{ marginTop: 6 }}>
              {d.errors.map((e, i) => (
                <li key={i}>
                  <strong>{e.source}</strong> — {num(e.count)}
                  {e.last ? `: ${e.last}` : ''}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </SectionCard>
  )
}
