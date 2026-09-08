/**
 * The online-enrichment switch and the run control (FR-126, NFR-502).
 *
 * FR-126 wants one unambiguous decision: off means nothing is searched at all,
 * not "searched but not merged". It is stored as a consent decision like every
 * other outbound choice in the application, so a withdrawal stays visible in
 * the audit trail rather than looking like a profile that was never asked.
 */

import { useState } from 'react'

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, Modal } from '../../components/ui'

export default function EnrichmentSettings({ settings, consent, busy, running, onToggle, onRun }) {
  const enabled = Boolean(settings?.enabled)
  const [confirming, setConfirming] = useState(false)

  return (
    <>
      <div className="card phase-edge phase-1">
        <div className="card-header">
          <Icon name="browser" />
          <h3>
            Online enrichment
            <HelpTip title="Online enrichment">
              Publicly available pages about you are searched and read, and what they say is
              scored against your profile before anything is proposed. Switch it off and the
              composite profile is built from your own documents alone.
            </HelpTip>
          </h3>
          <div className="spacer" />
          <Badge tone={enabled ? 'ok' : undefined}>
            <Icon name={enabled ? 'check' : 'pause'} />
            {enabled ? 'On' : 'Off'}
          </Badge>
        </div>

        <p className="small" style={{ marginTop: 0 }}>
          {consent?.enrichment?.text ||
            'Publicly available pages about you are searched and read to enrich the composite profile.'}
        </p>

        <div className="row">
          {/* FR-126: the switch is a single, unambiguous control — off means no
              searching at all, not "search but do not merge". */}
          <button
            className={enabled ? 'btn btn-sm btn-danger' : 'btn btn-sm btn-primary'}
            disabled={busy === 'settings'}
            onClick={() => (enabled ? setConfirming(true) : onToggle(true))}
          >
            <Icon name={enabled ? 'pause' : 'play'} />
            {enabled ? 'Switch online enrichment off' : 'Switch online enrichment on'}
          </button>
          <button
            className="btn btn-sm"
            disabled={!enabled || running || busy === 'run'}
            onClick={onRun}
          >
            {running ? <span className="spinner" /> : <Icon name="search" />}
            {running ? 'Searching…' : 'Search for me now'}
          </button>
          <span className="small muted">
            Rejected pages are skipped on every future run.
          </span>
        </div>
      </div>

      <div className="card phase-1">
        <div className="card-header">
          <Icon name="lock" />
          <h3>
            Never searched
            <HelpTip title="Excluded domains">
              Sites whose terms prohibit automated access, and sites that would expose the
              search itself, are never fetched by enrichment — regardless of this switch.
            </HelpTip>
          </h3>
        </div>
        <div className="chips">
          {(settings?.excluded_domains || []).map((d) => (
            <span className="chip" key={d}>
              {d}
            </span>
          ))}
          {(settings?.excluded_domains || []).length === 0 && (
            <span className="small muted">The backend reported no exclusions.</span>
          )}
        </div>
      </div>

      {confirming && (
        <Modal
          title="Switch online enrichment off?"
          onClose={() => setConfirming(false)}
          actions={
            <>
              <button className="btn btn-sm" onClick={() => setConfirming(false)}>
                <Icon name="check" /> Leave it on
              </button>
              <button
                className="btn btn-sm btn-danger"
                onClick={() => {
                  setConfirming(false)
                  onToggle(false)
                }}
              >
                <Icon name="pause" /> Switch it off
              </button>
            </>
          }
        >
          <p className="small">
            No further pages will be searched or read. Findings you already accepted stay in
            the profile; the next rebuild will use your documents alone unless you switch it
            back on.
          </p>
        </Modal>
      )}
    </>
  )
}
