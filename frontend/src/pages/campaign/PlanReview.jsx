/**
 * The plan review (FR-163): every source, its native query, its rationale and
 * its estimate, with a toggle to exclude it. Nothing runs until launch.
 */

import { useEffect, useState } from 'react'

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, Empty, SectionCard, formatDuration } from '../../components/ui'

import ReuseReport from './ReuseReport'
import { ITEM_TONE, Stat, cost, num } from './shared'

export default function PlanReview({ plan, notes, busy, running, onPatch, onGenerate, onRecheckReuse }) {
  const items = plan?.items ?? []
  const totals = plan?.totals ?? {}

  if (!items.length) {
    return (
      <Empty
        title={
          <>
            <Icon name="document" /> No plan yet
          </>
        }
        action={
          <button className="btn btn-primary" onClick={onGenerate} disabled={busy === 'plan'}>
            {busy === 'plan' ? (
              <span className="spinner" />
            ) : (
              <>
                <Icon name="sparkle" /> Generate the plan
              </>
            )}
          </button>
        }
      >
        Planning reads your directives, your composite profile and your dream-job model, then turns
        them into each source&apos;s own query language. It fetches nothing: you see every source
        and its cost before anything runs.
      </Empty>
    )
  }

  return (
    <>
      <SectionCard
        icon="chart"
        title="Estimated for this plan"
        phase="phase-2"
        actions={<span className="small muted">stage: {plan.stage || 'planning'}</span>}
      >
        <div className="grid grid-4">
          <Stat icon="browser" label="Sources to run" value={totals.sources} />
          <Stat
            icon="document"
            label="Pages"
            tip={{ title: 'Estimated pages', body: 'Result pages to fetch per source, capped by the campaign ceilings. Editing a number below re-estimates that source’s duration and cost.' }}
            value={num(totals.estimated_pages)}
          />
          <Stat icon="clock" label="Duration" value={formatDuration(totals.estimated_seconds)} />
          <Stat
            icon="money"
            label="Cost"
            value={cost(totals.estimated_cost_eur)}
            note="fetching plus extraction"
          />
        </div>

        <div className="row row-wrap small muted" style={{ marginTop: 12 }}>
          <span>{totals.sources_excluded || 0} excluded by you</span>
          <span>· {totals.sources_skipped_by_reuse || 0} skipped as already fresh</span>
          {plan.caps && (
            <span>
              · ceilings: {num(plan.caps.max_pages)} pages, {num(plan.caps.max_companies)} companies,{' '}
              {num(plan.caps.max_people)} people, {formatDuration(plan.caps.max_duration_seconds)}{' '}
              {/* FR-186 */}
            </span>
          )}
        </div>

        <p className="small" style={{ margin: '12px 0 0' }}>
          <strong>Nothing has been fetched.</strong> Read the queries, exclude what you do not want,
          then launch.
        </p>
      </SectionCard>

      {notes && (notes.degraded || notes.rejected?.length) && (
        <SectionCard icon="sparkle" title="How this plan was built" phase="phase-2">
          <p className="small muted" style={{ marginTop: 0 }}>
            {notes.llmUsed
              ? 'Queries were translated into each source’s native form by the model.'
              : 'Queries were built deterministically from your directives.'}
            {notes.countries?.length ? ` Target countries: ${notes.countries.join(', ')}.` : ''}
          </p>
          {/* NFR-104: degradation is stated, never silent. */}
          {notes.degraded && (
            <div className="alert alert-warn">
              <Icon name="warning" />
              <div>{notes.degraded}</div>
            </div>
          )}
          {notes.rejected?.length > 0 && (
            <>
              <h4>Sources not used (FR-164)</h4>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>
                        <Icon name="browser" /> Source
                      </th>
                      <th>Type</th>
                      <th>
                        <Icon name="info" /> Why not
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {notes.rejected.map((r) => (
                      <tr key={r.adapter_key}>
                        <td>{r.display_name || r.adapter_key}</td>
                        <td className="small muted">{r.source_type}</td>
                        <td className="small">{r.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </SectionCard>
      )}

      <ReuseReport report={plan.reuse_report} busy={busy === 'reuse'} onRecheck={onRecheckReuse} />

      <SectionCard
        icon="browser"
        title="Source plan"
        phase="phase-2"
        actions={<span className="small muted">{items.length} items</span>}
      >
        <div className="col" style={{ gap: 12 }}>
          {items.map((item) => (
            <PlanItem
              key={item.id}
              item={item}
              busy={busy === item.id}
              locked={running}
              onPatch={onPatch}
            />
          ))}
        </div>
      </SectionCard>
    </>
  )
}

function PlanItem({ item, busy, locked, onPatch }) {
  const [pages, setPages] = useState(String(item.estimated_pages ?? 0))
  const excluded = Boolean(item.excluded_by_user)

  useEffect(() => setPages(String(item.estimated_pages ?? 0)), [item.estimated_pages])

  function commitPages() {
    const next = Number(pages)
    if (!Number.isFinite(next) || next < 0 || next === item.estimated_pages) return
    onPatch(item.id, { estimated_pages: next })
  }

  return (
    <div className={`cmp-item${excluded ? ' cmp-excluded' : ''}`}>
      <div className="row row-wrap">
        <strong>{item.display_name || item.adapter_key}</strong>
        <span className="badge">{item.source_type || 'source'}</span>
        <span className="badge">{item.access_method || 'http'}</span>
        {item.tos_status && item.tos_status !== 'permitted' && (
          <Badge tone={item.tos_status === 'prohibited' ? 'danger' : 'warn'}>
            terms: {item.tos_status}
          </Badge>
        )}
        <Badge tone={ITEM_TONE[item.status]}>{item.status}</Badge>
        <div className="spacer" />
        {/* FR-163: exclusion is the user's, and it survives re-planning. */}
        <label className="checkline">
          <input
            type="checkbox"
            checked={!excluded}
            disabled={busy || locked}
            onChange={() => onPatch(item.id, { excluded_by_user: !excluded })}
          />
          <span>
            <Icon name={excluded ? 'x' : 'check'} /> {excluded ? 'Excluded' : 'Include'}
          </span>
        </label>
      </div>

      {item.rationale && <p className="small" style={{ margin: '8px 0 0' }}>{item.rationale}</p>}

      <div className="row row-wrap" style={{ marginTop: 10 }}>
        <label className="small muted" htmlFor={`pages-${item.id}`}>
          Pages
        </label>
        <input
          id={`pages-${item.id}`}
          type="number"
          min="0"
          max="500"
          value={pages}
          disabled={busy || locked}
          onChange={(e) => setPages(e.target.value)}
          onBlur={commitPages}
          style={{ width: 84 }}
        />
        <span className="small muted">· {formatDuration(item.estimated_seconds)}</span>
        <span className="small muted">· {cost(item.estimated_cost_eur)}</span>
        {item.records_collected > 0 && (
          <span className="badge badge-ok">{num(item.records_collected)} records</span>
        )}
        {item.error_count > 0 && <span className="badge badge-warn">{item.error_count} errors</span>}
        {busy && <span className="spinner" />}
      </div>

      <div className="small muted" style={{ marginTop: 10 }}>
        <Icon name="search" /> Native query
        <HelpTip term="native_query" />
      </div>
      <pre className="cmp-query">{JSON.stringify(item.native_query ?? {}, null, 2)}</pre>

      {item.last_error && (
        <div className="small" style={{ marginTop: 6, color: 'var(--danger)' }}>
          <Icon name="error" /> {item.last_error}
        </div>
      )}
    </div>
  )
}
