/**
 * Profile tab bar with progressive disclosure.
 *
 * The first-run job on this screen is to get the two documents in, so the
 * refinement tabs (Sections, Skills, Evidence, Personas, Privacy) sit behind a
 * "More profile detail" toggle instead of crowding the first screen. The
 * toggle is a disclosure, not a gate: every tab stays one click away and none
 * is removed.
 *
 * Once a profile version exists the detail group opens by default, which is
 * the "reveal normally" state; the seeker can still collapse it. The tab that
 * is currently active is never hidden, so collapsing the group can never
 * strand an open panel behind a control the user cannot see.
 */

import Icon from '../../components/Icon'
import { Tabs } from '../../components/ui'

export default function ProfileTabs({ primary, detail, active, onChange, open, onToggle }) {
  const activeInDetail = detail.some((t) => t.key === active)
  const showDetail = open || activeInDetail
  const hiddenCount = detail.filter((t) => t.key !== active).length

  return (
    <div className="col" style={{ gap: 0 }}>
      <Tabs tabs={primary} active={active} onChange={onChange} />

      {detail.length > 0 && (
        <div
          className="row"
          style={{ alignItems: 'center', marginTop: -10, marginBottom: showDetail ? 8 : 18 }}
        >
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            aria-expanded={showDetail}
            onClick={onToggle}
          >
            <Icon name={showDetail ? 'minus' : 'plus'} />
            {showDetail ? 'Hide extra profile tabs' : 'More profile detail'}
            {!showDetail && hiddenCount > 0 && (
              <span className="muted">· {hiddenCount} more</span>
            )}
          </button>
        </div>
      )}

      {showDetail && (
        <div id="profile-detail-tabs">
          <Tabs tabs={detail} active={active} onChange={onChange} />
        </div>
      )}
    </div>
  )
}
