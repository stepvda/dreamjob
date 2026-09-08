/**
 * The two framing blocks of the directive editor: how a first-time user starts,
 * and the name-and-save band above the five groups.
 *
 * The empty state teaches rather than reporting emptiness — a directive set is
 * the point at which a new user has to make a dozen decisions at once, and
 * FR-147's proposal from the composite profile is the shortest way past that.
 */

import { Link } from 'react-router-dom'

import { Caution, FirstRun } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, ErrorBox } from '../../components/ui'

export function StartState({ isFirstRun, proposing, onPropose, onBlank }) {
  const buttons = (
    <div className="row row-wrap">
      <button className="btn btn-primary" onClick={onPropose} disabled={proposing}>
        {proposing ? (
          <span className="spinner" />
        ) : (
          <>
            <Icon name="sparkle" /> Propose directives from my profile
          </>
        )}
      </button>
      <button className="btn" onClick={onBlank}>
        <Icon name="plus" /> Start from an empty set
      </button>
      <Link className="btn btn-ghost" to="/dream-job">
        <Icon name="dream" /> Write my dream-job statement first
      </Link>
    </div>
  )

  if (isFirstRun) return <FirstRun pathname="/directives" action={buttons} />

  return (
    <div className="card phase-2 phase-edge">
      <div className="card-header">
        <Icon name="directives" />
        <h3>Pick up where you left off</h3>
      </div>
      <p className="small muted">
        Load one of your saved sets from the panel beside this, or start a new one. A proposal is
        built from your composite profile and your dream-job statement — nothing is invented, and
        nothing is saved until you say so.
      </p>
      {buttons}
    </div>
  )
}

export function EditorHeader({
  draft,
  current,
  savedNote,
  saveError,
  needsVersion,
  onRename,
  onSaveVersion,
}) {
  return (
    <div className="card phase-2 phase-edge">
      <div className="row row-wrap">
        <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 220 }}>
          <label>
            <Icon name="directives" className="phase-ink" /> Directive set name
          </label>
          <input
            type="text"
            value={draft.name}
            maxLength={120}
            onChange={(e) => onRename(e.target.value)}
          />
        </div>
        {current && (
          <div className="col" style={{ gap: 2 }}>
            <span className="tiny muted">Saved</span>
            <Badge>
              <Icon name="check" /> version {current.version}
            </Badge>
          </div>
        )}
      </div>

      {savedNote && (
        <div className="alert alert-ok" style={{ marginTop: 12, marginBottom: 0 }}>
          <Icon name="success" />
          <div>{savedNote}</div>
        </div>
      )}

      {saveError && !needsVersion && (
        <div style={{ marginTop: 12 }}>
          <ErrorBox error={saveError} />
        </div>
      )}

      {/* FR-148: a set a campaign has already used is versioned, never overwritten,
          so the ranking that campaign produced stays explainable afterwards. */}
      {needsVersion && (
        <div style={{ marginTop: 12 }}>
          <Caution title="This set has already been used by a campaign">
            Editing it in place would leave that campaign's ranking unexplainable. Save the change
            as the next version of the same name instead — the campaign keeps pointing at the
            version it actually ran under.
            <div style={{ marginTop: 10 }}>
              <button className="btn btn-sm" onClick={onSaveVersion}>
                <Icon name="copy" /> Save as a new version
              </button>
            </div>
          </Caution>
        </div>
      )}
    </div>
  )
}
