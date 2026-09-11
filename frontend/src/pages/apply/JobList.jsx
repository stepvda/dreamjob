/**
 * The left half: every selected job at a glance.
 *
 * A row has to answer four questions without being opened — which company,
 * which role, who would receive it, and how far its package has got — because
 * the product owner is populating five hundred of these and opening five
 * hundred rows is not a workflow.
 *
 * Every filter is applied by the server and every chip carries the count it
 * would leave, from the same aggregate that produced the list. That is the
 * whole reason the counts can be trusted: a chip that says "42" and a list
 * that then shows 42 rows came out of one query over one join, not out of a
 * page of rows the browser happened to be holding.
 *
 * Within a group the chips are mutually exclusive, because "has a contact" and
 * "no contact" are the same question with two answers and offering them as
 * independent toggles only invites someone to select both and get nothing.
 * Across groups they combine, which is how "speculative, generated, no contact
 * yet" — the pile that actually needs work — is one click away.
 */

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { KindBadge } from '../../components/ui'

import { StateBadge, isSendable } from '../../components/package/shared'

/** The server's filter keys, grouped into the questions they answer. */
const GROUPS = [
  {
    label: 'Contact',
    options: [
      { key: 'has_contact', label: 'Has a contact' },
      { key: 'no_contact', label: 'None yet' },
      { key: 'unreachable', label: 'Unreachable' },
      { key: 'contact_pending', label: 'Not looked' },
    ],
  },
  {
    label: 'Package',
    options: [
      { key: 'generated', label: 'Generated' },
      { key: 'not_generated', label: 'Not generated' },
      { key: 'approved', label: 'Approved' },
      { key: 'sent', label: 'Sent' },
    ],
  },
  {
    label: 'Checks',
    options: [
      { key: 'consistency_passed', label: 'Passed' },
      { key: 'consistency_failed', label: 'Failed' },
    ],
  },
  {
    label: 'Kind',
    options: [
      { key: 'vacancy', label: 'Advertised' },
      { key: 'speculative', label: 'Speculative' },
    ],
  },
]

export default function JobList({
  rows,
  total,
  facets,
  loading,
  query,
  onQuery,
  filters,
  onFilters,
  selectedId,
  onSelect,
  checked,
  onToggle,
  onCheckAll,
  onClearChecked,
  limit,
  offset,
  onPage,
}) {
  const sendableHere = rows.filter(isSendable)
  const allChecked =
    sendableHere.length > 0 && sendableHere.every((r) => checked.includes(r.opportunity_id))
  const from = total === 0 ? 0 : offset + 1
  const to = offset + rows.length

  /** One answer per group: choosing a sibling replaces it, choosing it again clears it. */
  function pick(group, key) {
    const siblings = group.options.map((o) => o.key)
    const kept = filters.filter((f) => !siblings.includes(f))
    onFilters(filters.includes(key) ? kept : [...kept, key])
  }

  return (
    <div className="card apl-side phase-4">
      <div className="row" style={{ marginBottom: 8 }}>
        <Icon name="send" />
        <h3 style={{ margin: 0 }}>The selection</h3>
        <div className="spacer" />
        {loading && <span className="spinner" />}
      </div>

      <input
        type="search"
        value={query}
        placeholder="Search role, company or contact…"
        onChange={(e) => onQuery(e.target.value)}
        style={{ width: '100%' }}
      />

      {GROUPS.map((group) => (
        <div className="row" key={group.label} style={{ marginTop: 6, gap: 6 }}>
          <span className="tiny muted" style={{ width: 54, flex: 'none' }}>
            {group.label}
          </span>
          <div className="chips">
            {group.options.map((option) => (
              <span
                key={option.key}
                className={`chip clickable${filters.includes(option.key) ? ' on' : ''}`}
                onClick={() => pick(group, option.key)}
                title={`${facets?.[option.key] ?? 0} of your selection`}
              >
                {option.label}
                <span className="tiny muted">{facets?.[option.key] ?? 0}</span>
              </span>
            ))}
          </div>
        </div>
      ))}

      <div className="row small" style={{ marginTop: 10 }}>
        <strong>{Number(total || 0).toLocaleString('en-GB')}</strong>
        <span className="muted">
          {total === 1 ? 'job matches' : 'jobs match'}
          {rows.length > 0 ? ` · showing ${from}–${to}` : ''}
        </span>
        <div className="spacer" />
        {filters.length > 0 && (
          <button className="btn btn-sm btn-ghost" onClick={() => onFilters([])}>
            Clear filters
          </button>
        )}
      </div>
      <div className="tiny muted">
        Of {Number(facets?.total || 0).toLocaleString('en-GB')} in the whole selection. Every count
        beside a chip comes from the same query as the list.
      </div>

      {sendableHere.length > 0 && (
        <div className="apl-bulk">
          <input
            type="checkbox"
            checked={allChecked}
            onChange={() => (allChecked ? onClearChecked() : onCheckAll(sendableHere))}
            aria-label="Choose every approved job on this page"
          />
          <span className="small">
            {checked.length > 0
              ? `${checked.length} chosen for “Send all”, across every page`
              : `${sendableHere.length} ready to send on this page`}
          </span>
          <HelpTip term="bulk_approval_summary" />
          <div className="spacer" />
          {checked.length > 0 && (
            <button className="btn btn-sm btn-ghost" onClick={onClearChecked}>
              Clear
            </button>
          )}
        </div>
      )}

      <div className="apl-list" style={{ marginTop: 10 }}>
        {rows.length === 0 && !loading && (
          <p className="small muted" style={{ padding: '14px 4px' }}>
            No job matches. Clear a filter, or widen the search.
          </p>
        )}

        {rows.map((row) => (
          <div
            key={row.opportunity_id}
            className={`apl-row${selectedId === row.opportunity_id ? ' on' : ''}${
              row.kind === 'speculative' ? ' speculative' : ''
            }`}
          >
            <input
              type="checkbox"
              checked={checked.includes(row.opportunity_id)}
              disabled={!isSendable(row)}
              onChange={() => onToggle(row)}
              aria-label={`Choose ${row.title} for a bulk send`}
              title={
                isSendable(row)
                  ? 'Include in “Send all with attachment”'
                  : 'Only an approved package with a contact and a CV can be included'
              }
            />
            <button className="apl-row-main" onClick={() => onSelect(row.opportunity_id)}>
              <span className="apl-row-title">{row.title || 'Untitled role'}</span>
              <span className="small muted">{row.company_name || 'Unknown company'}</span>
              <span className="small">
                {row.contact_email ? (
                  <>
                    <Icon name="contacts" /> {row.contact_name || row.contact_email}
                    {row.contact_role ? <span className="muted"> · {row.contact_role}</span> : null}
                  </>
                ) : (
                  <span className="muted">
                    <Icon name="search" />{' '}
                    {row.reachability === 'unreachable'
                      ? 'no address survived validation'
                      : 'no contact looked for yet'}
                  </span>
                )}
              </span>
              <span className="row row-wrap" style={{ gap: 5, marginTop: 3 }}>
                <StateBadge state={row.state} />
                <KindBadge kind={row.kind} />
                {row.consistency_status === 'fail' && (
                  <span className="badge badge-danger">checks failed</span>
                )}
                {row.contact_objected && <span className="badge badge-danger">objected</span>}
              </span>
            </button>
          </div>
        ))}
      </div>

      <div className="row" style={{ marginTop: 10 }}>
        <button
          className="btn btn-sm"
          disabled={offset === 0 || loading}
          onClick={() => onPage(Math.max(0, offset - limit))}
        >
          Previous
        </button>
        <span className="tiny muted">{limit} per page</span>
        <div className="spacer" />
        <button
          className="btn btn-sm"
          disabled={offset + limit >= total || loading}
          onClick={() => onPage(offset + limit)}
        >
          Next
        </button>
      </div>
    </div>
  )
}
