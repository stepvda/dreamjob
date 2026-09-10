/**
 * The per-source list, grouped so that a plan with thousands of items in it can
 * be read (FR-162, FR-163, FR-166, FR-361).
 *
 * Two things were wrong with the flat list this replaces.
 *
 * The first is what the user reported: every row was titled with the adapter's
 * catalogue name, and `display_name` belongs to the *adapter*, not to the item.
 * A plan holding 2,417 Personio boards rendered 2,417 rows reading "Personio".
 * What separates them lives in the item's own query and is now named by the
 * server as `label` (which board, which region and sector, which website) with
 * the shared shape of the query in `label_detail` (FR-162).
 *
 * The second is that some of those rows were not different at all. The reuse
 * assessment leaves a `skipped` twin beside an item it has decided is already
 * fresh, and in one running campaign that is 1,701 of 6,524 rows - a quarter of
 * the list, naming targets that are already named above. No label can separate
 * rows that are the same row, so they are collapsed - on the target *and* the
 * label, for the reason `rowKey` gives - and the surviving row says how many
 * plan items named it (FR-166).
 *
 * Grouping by adapter on top of that is the same answer `plan_summary()` gives
 * on the plan screen and `CollectionOutcomes` gives above: 17 rows that expand,
 * rather than 6,524 that do not. It is also what makes the screen affordable -
 * the flat list reconciled roughly 117,000 DOM elements on every four-second
 * poll, and no amount of memoisation fixes rendering a list nobody can read.
 */

import { memo, useCallback, useMemo, useState } from 'react'

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, JobProgress, SectionCard } from '../../components/ui'

import { ITEM_TONE, OUTCOME_BADGE, num } from './shared'

/** How many targets one expanded adapter shows before it says how many it has. */
const MAX_ROWS = 100

/** One array, so a payload without sources does not rebuild the grouping memo. */
const NONE = []

/** Status chips in a fixed order, so a group's shape does not shuffle per poll. */
const STATUS_ORDER = ['failed', 'running', 'done', 'blocked', 'gone', 'capped', 'planned', 'skipped']

/**
 * The statuses this group holds, in the fixed order, with anything the order
 * has not heard of after them - a status nobody listed is still a status, and
 * dropping it would quietly under-report the group.
 */
function chips(counts) {
  const known = STATUS_ORDER.filter((status) => counts[status])
  const rest = Object.keys(counts).filter((status) => !STATUS_ORDER.includes(status))
  return [...known, ...rest]
}

/**
 * Which of two plan items naming one target should represent it.
 *
 * The one that collected something, and failing that the one that was not
 * skipped: a `skipped` twin is the reuse assessment's marker that the records
 * are already in the knowledge base, and showing it in place of the item that
 * actually ran would report the run as having done nothing.
 */
function preferred(candidate, current) {
  const collected = candidate.records_collected || 0
  const held = current.records_collected || 0
  if (collected !== held) return collected > held
  return current.status === 'skipped' && candidate.status !== 'skipped'
}

/**
 * What makes two rows indistinguishable to the reader.
 *
 * `target_key` alone is not it, and using it alone hid sources. It is the key
 * the *fetch ledger* is written under (FR-342): it answers "have we read this
 * target recently", so it deliberately ignores everything that does not change
 * which target is being read - `page`, `pages`, `max_records`, `countries`,
 * and, for an ATS board, every field except the vendor and the slug. Two plan
 * items can therefore share a target and still be two different questions put
 * to it. Measured over the 6,524-item campaign this screen was reported on,
 * `target_key` on its own collapses 1,740 rows, but only 48 of them are
 * genuine duplicates: 24 of the rest are Workday boards searched once per
 * search term, so "Deloitte - AI Engineer", "Deloitte - Data Engineer" and
 * four more became one row, and five sources the server had already named
 * apart could not be reached from the screen at all.
 *
 * So the key is the target *and* the name the reader sees. If the server gave
 * two rows different labels, they are different rows and both are shown; if it
 * gave them the same label and the same target, no label can separate them and
 * they collapse - which is the case the collapse exists for, the `skipped`
 * reuse twin that names a target already named above. Case-folded, because
 * "xfive" and "xFIVE" are one board written down twice, not two boards.
 *
 * `target_key` is only present once the backend half of this has shipped; until
 * then the plan item's own id stands in, every group has exactly one item, and
 * the list simply does not collapse anything - which is the honest answer when
 * the server has not said which rows are the same.
 */
function rowKey(source) {
  if (!source.target_key) return source.plan_item_id
  return `${source.target_key}\u0000${String(source.label || '').toLowerCase()}`
}

/**
 * One row per adapter, one row per distinct target inside it.
 */
function buildGroups(sources) {
  const byAdapter = new Map()

  for (const source of sources) {
    const adapterKey = source.adapter_key || 'unknown'
    let group = byAdapter.get(adapterKey)
    if (!group) {
      group = {
        adapter_key: adapterKey,
        display_name: source.display_name || adapterKey,
        targets: new Map(),
        records: 0,
        planItems: 0,
        counts: {},
      }
      byAdapter.set(adapterKey, group)
    }

    group.planItems += 1
    group.records += source.records_collected || 0
    const status = source.status || 'planned'
    group.counts[status] = (group.counts[status] || 0) + 1

    const key = rowKey(source)
    const held = group.targets.get(key)
    if (!held) {
      group.targets.set(key, { key, source, items: 1 })
    } else {
      held.items += 1
      if (preferred(source, held.source)) held.source = source
    }
  }

  return [...byAdapter.values()].map((group) => ({
    adapter_key: group.adapter_key,
    display_name: group.display_name,
    rows: [...group.targets.values()],
    total: group.targets.size,
    planItems: group.planItems,
    records: group.records,
    counts: group.counts,
  }))
}

/** Everything the filter box is allowed to match on. */
function haystack(source) {
  return `${source.label || ''} ${source.label_detail || ''} ${source.adapter_key || ''}`.toLowerCase()
}

export default function SourceList({ sources }) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(() => new Set())

  const rows = sources || NONE
  const groups = useMemo(() => buildGroups(rows), [rows])

  const needle = query.trim().toLowerCase()
  const shown = useMemo(() => {
    if (!needle) return groups
    return groups
      .map((group) => {
        // Matching the adapter itself keeps the whole group, so that "personio"
        // answers with every Personio board rather than with none of them.
        if (
          group.adapter_key.toLowerCase().includes(needle) ||
          group.display_name.toLowerCase().includes(needle)
        ) {
          return group
        }
        const matched = group.rows.filter((row) => haystack(row.source).includes(needle))
        return matched.length ? { ...group, rows: matched } : null
      })
      .filter(Boolean)
  }, [groups, needle])

  const targets = groups.reduce((sum, group) => sum + group.total, 0)
  const collapsed = groups.reduce((sum, group) => sum + (group.planItems - group.total), 0)

  // Stable, so that typing in the filter box re-renders the box and not the
  // seventeen group heads underneath it.
  const toggle = useCallback((adapterKey) => {
    setOpen((previous) => {
      const next = new Set(previous)
      if (next.has(adapterKey)) next.delete(adapterKey)
      else next.add(adapterKey)
      return next
    })
  }, [])

  return (
    <SectionCard
      icon="browser"
      title={
        <>
          Per source
          <HelpTip term="source_target" />
          <HelpTip term="extraction_rate" />
        </>
      }
      phase="phase-2"
      actions={
        <span className="small muted">
          {/* This used to read "6524 adapters" over the plan-item array. Those
              are plan items across 17 adapters; the count is taken from the
              grouping rather than from the length of the payload. */}
          {num(groups.length)} {groups.length === 1 ? 'adapter' : 'adapters'} · {num(targets)}{' '}
          {targets === 1 ? 'target' : 'targets'}
        </span>
      }
    >
      {groups.length > 1 && (
        <div className="row row-wrap" style={{ marginBottom: 12 }}>
          <input
            type="text"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Filter by company, board, region or adapter…"
            aria-label="Filter sources"
            style={{ maxWidth: 340 }}
          />
          <span className="small muted">
            {needle
              ? `${num(shown.length)} of ${num(groups.length)} adapters match`
              : collapsed > 0
                ? `${num(collapsed)} duplicate ${collapsed === 1 ? 'row' : 'rows'} collapsed`
                : ''}
          </span>
        </div>
      )}

      <div className="col" style={{ gap: 0 }}>
        {shown.map((group) => (
          <SourceGroup
            key={group.adapter_key}
            group={group}
            expanded={Boolean(needle) || open.has(group.adapter_key)}
            onToggle={toggle}
          />
        ))}
      </div>

      {!rows.length && (
        <p className="small muted" style={{ margin: 0 }}>
          No source plan items.
        </p>
      )}
      {rows.length > 0 && !shown.length && (
        <p className="small muted" style={{ margin: 0 }}>
          Nothing matches “{query.trim()}”.
        </p>
      )}
    </SectionCard>
  )
}

/**
 * One adapter, collapsed to a line until it is asked to open - the same shape
 * the outcomes ledger above uses, for the same reason: nobody reviews 2,417
 * boards item by item, but everybody wants to find one of them.
 */
const SourceGroup = memo(function SourceGroup({ group, expanded, onToggle }) {
  const visible = group.rows.slice(0, MAX_ROWS)

  return (
    <div style={{ borderTop: '1px solid var(--line)' }}>
      <button
        type="button"
        className="cmp-group-head"
        aria-expanded={expanded}
        onClick={() => onToggle(group.adapter_key)}
      >
        <span className="dir-caret" aria-hidden>
          {expanded ? '▾' : '▸'}
        </span>
        <strong className="cmp-group-name">{group.display_name}</strong>
        <span className="badge">{group.adapter_key}</span>
        <span className="small muted">
          {num(group.rows.length)}
          {group.rows.length === group.total ? '' : ` of ${num(group.total)}`}{' '}
          {group.total === 1 ? 'target' : 'targets'}
          {group.records > 0 ? ` · ${num(group.records)} records` : ''}
        </span>
        <span className="spacer" />
        <span className="row row-wrap" style={{ gap: 6 }}>
          {chips(group.counts).map((status) => (
            <Badge key={status} tone={ITEM_TONE[status]}>
              {num(group.counts[status])} {status}
            </Badge>
          ))}
        </span>
      </button>

      {expanded && (
        <div className="col" style={{ gap: 10, padding: '2px 0 14px 22px' }}>
          {visible.map((row) => (
            <SourceProgress key={row.source.plan_item_id} source={row.source} items={row.items} />
          ))}
          {group.rows.length > MAX_ROWS && (
            <p className="small muted" style={{ margin: 0 }}>
              Showing the first {num(MAX_ROWS)} of {num(group.rows.length)} targets. Use the filter
              above to reach the rest.
            </p>
          )}
        </div>
      )}
    </div>
  )
})

/**
 * One target's progress, shown through the same component as the campaign job
 * so a source and the run it belongs to read identically (NFR-502).
 */
const SourceProgress = memo(function SourceProgress({ source, items }) {
  // FR-186: the estimate now comes off the source row. It used to be joined
  // from `plan.items`, which is one page of 100 - so 6,424 of 6,524 rows fell
  // back to a hard-coded ten records a page and printed the wrong estimate.
  const perPage = source.records_per_page || 10
  const expected = Math.max(1, (source.estimated_pages || 0) * perPage)
  const name = source.display_name || source.adapter_key
  const job = {
    // FR-162: `display_name` belongs to the adapter, so 2,417 rows read
    // "Personio". `label` is what this particular item asked.
    kind: source.label ? `${name} — ${source.label}` : name,
    adapter_key: source.adapter_key,
    status: source.status === 'planned' ? 'pending' : source.status,
    progress_done: source.records_collected || 0,
    progress_total: expected,
    estimated_seconds: source.estimated_seconds ?? null,
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
        {source.label_detail && (
          <span className="cmp-src-detail" title={source.label_detail}>
            {source.label_detail}
          </span>
        )}
        <span>records against an estimate of {num(expected)}</span>
        {source.extraction_success_rate != null && (
          <span>· extraction {Math.round(source.extraction_success_rate * 100)}%</span>
        )}
        {items > 1 && (
          <span
            /* Not "the other": a target can be named by more than two items,
               and the reuse assessment is only the commonest reason - it is
               not something this row can check. */
            title={`${items} plan items ask this target the same question, so they are shown as one row. Usually the reuse assessment has skipped all but one of them as already fresh.`}
            className="row"
            style={{ gap: 4 }}
          >
            <Icon name="copy" size={12} /> {num(items)} plan items
          </span>
        )}
      </div>
    </div>
  )
})
