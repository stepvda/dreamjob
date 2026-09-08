/**
 * The two panels beside the editor: saved directive sets (FR-148) and the
 * pre-launch collection estimate (FR-147).
 *
 * The estimate belongs next to the controls rather than on the campaign screen
 * because FR-147 is about seeing the consequence of a setting while you are
 * still changing it — "how much will this collect?" answered before you commit
 * to a campaign, not after.
 */

import Icon from '../../components/Icon'
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  Stat,
  formatDate,
  formatDuration,
  formatMoney,
} from '../../components/ui'
import { HelpTip } from '../../components/Help'

/* --- Saved sets (FR-148) --------------------------------------------------- */

export function SavedSets({ sets, loading, error, onRetry, currentId, onLoad, onDuplicate, onVersions, onDelete }) {
  return (
    <div className="card phase-2 phase-edge">
      <div className="card-header">
        <Icon name="directives" />
        <h3>Saved directive sets</h3>
        <HelpTip title="Sets and versions">
          A set is named, versioned and reusable across campaigns. Editing a set a campaign has
          already used is refused — you save a new version of the same name instead, so the
          ranking that campaign produced stays explainable.
        </HelpTip>
      </div>

      {loading && <Loading rows={3} />}
      {!loading && error && <ErrorBox error={error} onRetry={onRetry} />}
      {!loading && !error && (sets?.length ?? 0) === 0 && (
        <p className="small muted" style={{ margin: 0 }}>
          Nothing saved yet. Your first save names this set.
        </p>
      )}

      {!loading && !error && sets?.length > 0 && (
        <div className="col" style={{ gap: 8 }}>
          {sets.map((s) => (
            <div className={`dir-setrow${s.id === currentId ? ' on' : ''}`} key={s.id}>
              <div className="col" style={{ gap: 2, minWidth: 0 }}>
                <div className="row" style={{ gap: 6 }}>
                  <strong className="small">{s.name}</strong>
                  <Badge>v{s.version}</Badge>
                  {s.discretion_mode && (
                    <Badge tone="warn">
                      <Icon name="lock" /> discreet
                    </Badge>
                  )}
                  {s.spontaneous_only && (
                    <Badge tone="speculative">
                      <Icon name="speculative" /> speculative only
                    </Badge>
                  )}
                </div>
                <span className="tiny muted">
                  {formatDate(s.created_at)} ·{' '}
                  {(s.job_content?.target_titles || []).slice(0, 2).join(', ') || 'no titles'}
                </span>
              </div>
              <div className="spacer" />
              <div className="row" style={{ gap: 4 }}>
                <button className="btn btn-sm" onClick={() => onLoad(s)}>
                  <Icon name={s.id === currentId ? 'refresh' : 'download'} />
                  {s.id === currentId ? 'Reload' : 'Load'}
                </button>
                <button
                  className="btn btn-sm btn-ghost"
                  onClick={() => onVersions(s)}
                  title="Version history"
                  aria-label="Version history"
                >
                  <Icon name="clock" />
                </button>
                <button
                  className="btn btn-sm btn-ghost"
                  onClick={() => onDuplicate(s)}
                  title="Duplicate under a new name"
                  aria-label="Duplicate under a new name"
                >
                  <Icon name="copy" />
                </button>
                <button
                  className="btn btn-sm btn-ghost"
                  onClick={() => onDelete(s)}
                  title="Delete"
                  aria-label="Delete"
                >
                  <Icon name="trash" />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/* --- Pre-launch estimate (FR-147) ------------------------------------------ */

export function EstimatePanel({ estimate, busy, error, onRetry }) {
  return (
    <div className="card phase-2 phase-edge">
      <div className="card-header">
        <Icon name="chart" />
        <h3>Before you launch</h3>
        <HelpTip term="collection_estimate" />
        <div className="spacer" />
        {busy && <span className="spinner" />}
      </div>

      {error && <ErrorBox error={error} onRetry={onRetry} />}

      {!error && !estimate && busy && <Loading rows={3} />}

      {!error && !estimate && !busy && (
        <p className="small muted" style={{ margin: 0 }}>
          Change a directive and the estimate appears here.
        </p>
      )}

      {!error && estimate && (
        <>
          <div
            className="grid"
            style={{
              gridTemplateColumns: 'repeat(auto-fit, minmax(136px, 1fr))',
              gap: 10,
              marginBottom: 12,
            }}
          >
            <Stat icon="browser" label="Sources" value={estimate.source_count} phase="phase-2" />
            <Stat icon="search" label="Queries" value={estimate.query_count} phase="phase-2" />
            <Stat icon="document" label="Pages" value={estimate.total_pages} phase="phase-2" />
            <Stat
              icon="clock"
              label="Duration"
              value={formatDuration(estimate.total_seconds)}
              phase="phase-2"
            />
          </div>
          <div className="row small" style={{ marginBottom: 10 }}>
            <span className="muted row" style={{ gap: 6 }}>
              <Icon name="money" /> Estimated collection cost
            </span>
            <div className="spacer" />
            <strong>{formatMoney(estimate.total_cost_eur, 'EUR')}</strong>
          </div>

          {estimate.sources?.length > 0 ? (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>
                      <Icon name="browser" /> Source
                    </th>
                    <th className="nowrap">
                      <Icon name="search" /> Queries
                    </th>
                    <th className="nowrap">
                      <Icon name="document" /> Pages
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {estimate.sources.map((s) => (
                    <tr key={s.adapter_key}>
                      <td>
                        <div className="col" style={{ gap: 1 }}>
                          <span>{s.display_name || s.adapter_key}</span>
                          <span className="tiny muted">
                            {s.source_type} · {s.access_method}
                          </span>
                        </div>
                      </td>
                      <td className="nowrap">{s.queries}</td>
                      <td className="nowrap">{s.estimated_pages}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty
              title={
                <>
                  <Icon name="warning" /> No source would run
                </>
              }
            >
              Every registered adapter is disabled, unacknowledged or outside the countries these
              directives cover. Widen the location, or enable a source under Administration.
            </Empty>
          )}

          {estimate.skipped?.length > 0 && (
            <div style={{ marginTop: 10 }}>
              <div className="tiny muted" style={{ marginBottom: 4 }}>
                Not counted
              </div>
              <div className="chips">
                {estimate.skipped.map((s, i) => (
                  <span className="chip" key={`${s.adapter_key}-${i}`}>
                    {s.adapter_key} · {String(s.reason).replace(/_/g, ' ')}
                  </span>
                ))}
              </div>
            </div>
          )}

          {estimate.notes?.length > 0 && (
            <ul className="help-tips" style={{ marginTop: 12 }}>
              {estimate.notes.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          )}

          <p className="tiny muted" style={{ marginTop: 10, marginBottom: 0 }}>
            An order-of-magnitude figure computed from these settings, not a promise. The campaign
            planner refines it once it knows what the knowledge base can be reused for.
          </p>
        </>
      )}
    </div>
  )
}
