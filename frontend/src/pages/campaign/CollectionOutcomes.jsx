/**
 * What the collection run actually did, per source (FR-185, FR-361, NFR-403).
 *
 * This screen used to say "538 errors". It was not lying about the number and
 * it was useless anyway, because almost none of them were failures: 308 were
 * ATS boards answering 404 (a registry harvested from Wayback is live 27.5% of
 * the time, so dead slugs are the measured cost of having one at all), 192 were
 * robots.txt refusals, which is the product declining a source on purpose
 * (FR-182, CR-402), and a dozen were the thing an operator actually has to fix.
 *
 * So the six answers are separated, and the layout says which one matters:
 *
 *   failed   gets the colour, the size and the top of the block. Nothing else
 *            is allowed to compete with it - that is the entire change.
 *   blocked  is a decision, not a defect, and it expands to show which sources
 *            and why, with their IR-101 acknowledgement state, because an
 *            operator defending how this product collects needs that list.
 *   gone     is the world changing, not the code breaking - unless an adapter
 *            is 404 on every slug it tried, which is breakage and is called out
 *            separately (NFR-403).
 *   capped   sits below a rule, on its own, because a page budget the run has
 *            not spent yet is not an outcome at all (FR-186).
 *
 * The opposite bug is the one to keep in mind while reading this file: before
 * the outcomes existed, a source that fetched nothing was recorded as "done, 0
 * records, 0 errors" and a total retrieval failure hid behind it. Quiet is not
 * the goal here. True labels are, and a real failure has to stay loud.
 */

import { useState } from 'react'

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, SectionCard } from '../../components/ui'

import { num } from './shared'

/** Percentages of dead slugs, written the way the registry design measures them. */
function share(value) {
  if (value == null) return null
  return `${Math.round(value * 100)}%`
}

export default function CollectionOutcomes({ outcomes }) {
  if (!outcomes) return null

  const counts = outcomes.counts || {}
  const failed = counts.failed || 0
  const blocked = counts.blocked || 0
  const gone = counts.gone || 0
  const skipped = counts.skipped || 0
  const capped = counts.capped || 0
  const running = counts.running || 0
  const pending = counts.pending || 0

  // NFR-403: a source that returns 404 for every slug it has is not liveness
  // decay, it is an adapter that has broken. The distinction is worth drawing,
  // so it is drawn - in the warn hue, a step below the failure band.
  const breakage = (outcomes.gone || []).filter((g) => g.suspected_breakage)

  return (
    <SectionCard
      icon="chart"
      title={
        <>
          What each source did
          <HelpTip term="collection_outcome" />
        </>
      }
      phase="phase-2"
      actions={
        (running > 0 || pending > 0) && (
          <span className="small muted">
            {running > 0 && `${num(running)} running`}
            {running > 0 && pending > 0 && ' · '}
            {pending > 0 && `${num(pending)} not reached yet`}
          </span>
        )
      }
    >
      <FailureBand failed={failed} items={outcomes.failed || []} listed={outcomes.failed_listed} />

      <div className="col" style={{ gap: 0, marginTop: 12 }}>
        <Ledger
          icon="success"
          term="outcome_succeeded"
          label="collected"
          count={outcomes.records || 0}
          unit="records"
          note={`from ${num(outcomes.producing_sources || 0)} ${
            (outcomes.producing_sources || 0) === 1 ? 'source' : 'sources'
          }`}
        />
        <Ledger
          icon="lock"
          term="outcome_blocked"
          label="blocked"
          count={blocked}
          note="robots.txt and terms — we declined, on purpose"
          groups={outcomes.blocked}
          render={(group) => <BlockedGroup group={group} />}
        />
        <Ledger
          icon="error"
          term="outcome_gone"
          label="gone"
          count={gone}
          note="boards that no longer exist — retired from the registry"
          groups={outcomes.gone}
          render={(group) => <GoneGroup group={group} />}
        />
        <Ledger
          icon="minus"
          term="outcome_skipped"
          label="skipped"
          count={skipped}
          note="nothing to collect: excluded, or the source answered and holds none"
          groups={outcomes.skipped}
          render={(group) => <PlainGroup group={group} />}
        />
      </div>

      {breakage.length > 0 && (
        <div className="alert alert-warn" style={{ marginTop: 12, marginBottom: 0 }}>
          <Icon name="warning" />
          <div>
            <strong>
              {breakage.length === 1 ? 'One adapter is' : `${breakage.length} adapters are`} 404 on
              every slug tried.
            </strong>{' '}
            {breakage.map((g) => `${g.display_name} (${num(g.count)}/${num(g.attempted)})`).join(', ')}
            . A harvested registry decays and dead slugs are expected, but a whole adapter answering
            nothing is breakage rather than decay (NFR-403) — check the URL shape before retiring
            these boards.
          </div>
        </div>
      )}

      {/* FR-186: not an outcome. The run has not asked these sources anything
          yet; the page budget stopped it first. Kept below the rule so that it
          cannot be read as a result. */}
      {capped > 0 && (
        <div
          className="row row-wrap small muted"
          style={{ marginTop: 12, paddingTop: 10, borderTop: '1px solid var(--line)' }}
        >
          <Icon name="pause" />
          <span className="mono" style={{ fontVariantNumeric: 'tabular-nums' }}>
            {num(capped)}
          </span>
          <span>
            not started: page budget (FR-186)
            <HelpTip term="outcome_capped" />
          </span>
          <div className="spacer" />
          <span>raise the cap to continue them from where they stopped</span>
        </div>
      )}
    </SectionCard>
  )
}

/* --- The one that matters ------------------------------------------------- */

/**
 * The failure count, given the colour and the size, and the list underneath it.
 * Rendered at zero too: "no failures" is a finding, and an operator who has
 * learned to look here should find an answer rather than an absent block.
 */
function FailureBand({ failed, items, listed }) {
  const [open, setOpen] = useState(false)
  const any = failed > 0

  return (
    <div
      className={`alert ${any ? 'alert-danger' : 'alert-ok'}`}
      style={{ alignItems: 'flex-start', marginBottom: 0 }}
    >
      <Icon name={any ? 'warning' : 'success'} size={any ? 20 : 16} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="row" style={{ gap: 8, alignItems: 'baseline' }}>
          <span
            style={{
              fontSize: any ? 30 : 20,
              fontWeight: 650,
              letterSpacing: '-0.02em',
              fontVariantNumeric: 'tabular-nums',
              lineHeight: 1.1,
            }}
          >
            {num(failed)}
          </span>
          <strong style={{ fontSize: any ? 15 : 13 }}>failed</strong>
          <HelpTip term="outcome_failed" />
          <div className="spacer" />
          {any && (
            <button
              type="button"
              className="btn btn-sm"
              aria-expanded={open}
              onClick={() => setOpen((v) => !v)}
            >
              <Icon name={open ? 'minus' : 'plus'} />
              {open ? 'hide' : `show the ${num(Math.min(failed, listed ?? failed))}`}
            </button>
          )}
        </div>
        <div style={{ marginTop: 2 }}>
          {any
            ? 'Something actually went wrong: a server error, a transport error, a parse crash or an adapter exception. This is the list to work through — nothing else on this screen is a defect.'
            : 'Nothing went wrong. Everything below is an expected outcome: a source declined on principle, a board that no longer exists, or work the page budget has not reached.'}
        </div>

        {any && open && (
          <div className="table-wrap" style={{ marginTop: 10 }}>
            <table>
              <thead>
                <tr>
                  <th>Source</th>
                  <th>What went wrong</th>
                  <th className="num">Errors</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.plan_item_id}>
                    <td>
                      <div className="nowrap">{item.display_name}</div>
                      <span className="badge">{item.adapter_key}</span>
                    </td>
                    <td className="small">
                      {item.reason || 'no reason recorded'}
                      {item.state && (
                        <>
                          {' '}
                          <span className="badge badge-danger">{item.state}</span>
                        </>
                      )}
                    </td>
                    <td className="num">{num(item.error_count)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {listed != null && failed > listed && (
              <p className="small muted" style={{ margin: '8px 0 0' }}>
                Showing the {num(listed)} with the most errors, of {num(failed)}. The per-source list
                below carries the rest.
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

/* --- The ones that must not compete with it -------------------------------- */

/**
 * One line of the ledger: a number, what it is, and — where there is something
 * to defend or to retire — the sources behind it. Deliberately typographic and
 * uncoloured: these are not warnings, and colouring them is exactly how the
 * twelve failures got lost among five hundred expected outcomes.
 */
function Ledger({ icon, term, label, count, unit, note, groups, render }) {
  const [open, setOpen] = useState(false)
  const rows = groups || []
  const expandable = rows.length > 0

  return (
    <div style={{ borderTop: '1px solid var(--line)' }}>
      <div className="row row-wrap" style={{ gap: 10, padding: '9px 2px' }}>
        <span className="muted" style={{ display: 'flex' }}>
          <Icon name={icon} />
        </span>
        <span
          style={{
            minWidth: 76,
            textAlign: 'right',
            fontVariantNumeric: 'tabular-nums',
            fontWeight: 600,
            fontSize: 15,
          }}
        >
          {num(count)}
        </span>
        <span style={{ fontWeight: 550 }}>
          {label}
          {unit ? ` ${unit}` : ''}
        </span>
        {term && <HelpTip term={term} />}
        <span className="small muted">{note}</span>
        <div className="spacer" />
        {expandable && (
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            <span className="dir-caret" aria-hidden>
              {open ? '▾' : '▸'}
            </span>
            {open ? 'hide' : label === 'blocked' ? 'see why' : 'which'}
          </button>
        )}
      </div>
      {expandable && open && (
        <div className="col" style={{ gap: 8, padding: '0 2px 12px 28px' }}>
          {rows.map((group) => (
            <div key={group.adapter_key}>{render(group)}</div>
          ))}
        </div>
      )}
    </div>
  )
}

/** The head every group shares: which adapter, and how many of them. */
function GroupHead({ group, tail }) {
  return (
    <div className="row row-wrap" style={{ gap: 8 }}>
      <strong className="small">{group.display_name}</strong>
      <span className="badge">{group.adapter_key}</span>
      <span className="small muted">{num(group.count)}</span>
      {tail}
    </div>
  )
}

/** The reasons themselves, verbatim: a refusal paraphrased is not defensible. */
function Reasons({ reasons, omitted }) {
  if (!reasons?.length) return null
  return (
    <>
      <ul className="small muted" style={{ margin: '4px 0 0', paddingLeft: 18 }}>
        {reasons.map((reason) => (
          <li key={reason}>{reason}</li>
        ))}
      </ul>
      {omitted > 0 && (
        <p className="small muted" style={{ margin: '4px 0 0', paddingLeft: 18 }}>
          and {num(omitted)} more like these.
        </p>
      )}
    </>
  )
}

/**
 * FR-182, CR-402, IR-101: why this source was declined, and — where the source
 * is one an administrator has to acknowledge — whether that has been done. Both
 * halves belong here, because "we declined 192 sources on principle" is only
 * defensible if the principle is on the screen next to the number.
 */
function BlockedGroup({ group }) {
  const tos = group.tos_status
  const awaiting = group.requires_ack && !group.acknowledged_at
  return (
    <div className="cmp-item">
      <GroupHead
        group={group}
        tail={
          <>
            {tos && tos !== 'permitted' && (
              <Badge tone={tos === 'prohibited' ? 'warn' : 'info'}>terms: {tos}</Badge>
            )}
            {awaiting && <Badge tone="info">awaiting acknowledgement (IR-101)</Badge>}
            {group.requires_ack && group.acknowledged_at && (
              <span className="small muted">acknowledged (IR-101)</span>
            )}
          </>
        }
      />
      <Reasons reasons={group.reasons} omitted={group.reasons_omitted} />
    </div>
  )
}

/**
 * A board that answered 404 or 410. The share is the point: a registry
 * harvested from Common Crawl and Wayback is measured at 87.5% and 27.5% live,
 * so a fraction of dead slugs is the design working. All of them is not.
 */
function GoneGroup({ group }) {
  return (
    <div className="cmp-item">
      <GroupHead
        group={group}
        tail={
          <>
            {group.attempted > 0 && (
              <span className="small muted">
                of {num(group.attempted)} tried
                {group.share != null ? ` · ${share(group.share)} dead` : ''}
              </span>
            )}
            {group.suspected_breakage && (
              <Badge tone="warn">every slug — adapter, not decay (NFR-403)</Badge>
            )}
          </>
        }
      />
      <Reasons reasons={group.reasons} omitted={group.reasons_omitted} />
    </div>
  )
}

/** Nothing to collect, said plainly. */
function PlainGroup({ group }) {
  return (
    <div className="cmp-item">
      <GroupHead group={group} />
      <Reasons reasons={group.reasons} omitted={group.reasons_omitted} />
    </div>
  )
}
