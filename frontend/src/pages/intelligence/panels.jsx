/**
 * The three blocks of furniture around the five readings: the advisory notice
 * that has to be read before any number on this screen, the empty state that
 * teaches, and the confirmation in front of the one call that writes to the
 * ranked list.
 *
 * They live here rather than in IntelligencePage so that file stays about
 * fetching and orchestration.
 */

import { Link } from 'react-router-dom'

import { Caution, FirstRun, HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, Modal } from '../../components/ui'

/**
 * NFR-305 / CR-405. Stated before the numbers, because a caveat read
 * afterwards is a caveat that was not read.
 */
export function AdvisoryNotice({ discretionMode }) {
  return (
    <>
      <Caution title="A reading of the market, not a verdict on you">
        Every gap, route and mismatch below is derived from your own profile and from the
        postings and company pages this campaign actually collected. Nothing here is a statement
        about you that the material does not support, no score decides anything on its own, and
        nothing changes your ranked list or your profile unless you ask it to.
      </Caution>

      {/* FR-385: the flag every screen in this module is told about. */}
      {discretionMode && (
        <div className="row row-wrap" style={{ marginBottom: 12 }}>
          <Badge tone="warn">
            <Icon name="lock" /> Discretion mode
          </Badge>
          <span className="small muted">
            Excluded companies and contacts are withheld from everything on this screen.
          </span>
          <HelpTip term="discretion_mode" />
        </div>
      )}
    </>
  )
}

/** The empty screen is where a new user gives up, so it teaches rather than reports. */
export function IntelligenceFirstRun({ hasCampaign, running, onRun }) {
  return (
    <FirstRun
      pathname="/intelligence"
      action={
        <div className="row row-wrap">
          <button className="btn btn-primary" disabled={running || !hasCampaign} onClick={onRun}>
            {running ? <span className="spinner" /> : 'Run the gap analysis'}
          </button>
          {!hasCampaign && (
            <Link className="btn" to="/campaigns">
              Create a campaign first
            </Link>
          )}
          <Link className="btn btn-ghost" to="/dream-job">
            Review your dream-job statement
          </Link>
        </div>
      }
    >
      {hasCampaign
        ? 'Nothing has been computed for this campaign yet. The gap analysis is the place to start — it works without the language model, and the stepping-stone paths are built from what it finds.'
        : 'This screen reads a campaign’s collected postings and company pages. Create a campaign and run a collection, then come back.'}
    </FirstRun>
  )
}

/**
 * FR-284: writing to the ranked list is confirmed first, and the modal says
 * exactly what it will and will not touch.
 */
export function TagConfirm({ onCancel, onConfirm }) {
  return (
    <Modal
      title="Tag these opportunities?"
      onClose={onCancel}
      actions={
        <>
          <button className="btn" onClick={onCancel}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={onConfirm}>
            Add the two tags
          </button>
        </>
      }
    >
      <p>
        This adds <strong>destination</strong> to every opportunity clearing your dream-job
        threshold and <strong>stepping_stone</strong> to the ones behind the proposed paths.
      </p>
      <p className="muted small">
        Your own tags, pins, statuses and manual order are untouched — this call only adds those
        two labels and never removes anything.
      </p>
    </Modal>
  )
}
