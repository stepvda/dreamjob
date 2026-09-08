/**
 * The enrichment review queue (FR-123, FR-124, RK-02).
 *
 * RK-02 is the homonym risk: a stranger who shares the job seeker's name
 * ending up in their CV. The defence is not a better classifier, it is showing
 * the evidence. Each finding therefore lists every identity signal that was
 * scored - name variants, employer overlap, cross-links, location, timeline,
 * photo similarity - with its own score, its weight and the text that matched,
 * so the decision can actually be made rather than trusted.
 *
 * FR-124: a rejection is permanent. `enrichment_finding.rejected_permanently`
 * makes the URL invisible to every future run, so the control says so.
 */

import { useState } from 'react'

import { Caution, HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, Empty, Meter, Modal, formatDate } from '../../components/ui'

/** identity_match.py SIGNAL_WEIGHTS, plus the negative signal. */
const SIGNAL_LABEL = {
  name_match: 'Name variants',
  employer_overlap: 'Employer overlap',
  cross_link: 'Cross-links',
  location_match: 'Location',
  timeline_consistency: 'Timeline',
  photo_similarity: 'Photo similarity',
  conflicting_evidence: 'Conflicting evidence',
}

const CLASSIFICATION_TONE = { confirmed: 'ok', probable: 'warn', doubtful: 'danger' }

/** The classification is the first thing to read, so it carries a mark as well
    as a colour - a doubtful finding must not look like a confirmed one. */
const CLASSIFICATION_ICON = { confirmed: 'success', probable: 'warning', doubtful: 'error' }

const FILTERS = [
  { key: 'pending', label: 'Awaiting your decision' },
  { key: 'confirmed', label: 'Confirmed' },
  { key: 'probable', label: 'Probable' },
  { key: 'doubtful', label: 'Doubtful' },
  { key: 'rejected', label: 'Rejected' },
  { key: 'all', label: 'Everything' },
]

export default function FindingsQueue({ findings, enabled, busy, onDecide }) {
  const [filter, setFilter] = useState('pending')
  const [rejecting, setRejecting] = useState(null)
  const [reason, setReason] = useState('')

  const shown = findings.filter((f) => {
    if (filter === 'all') return true
    if (filter === 'pending') return f.status === 'pending' && f.classification !== 'confirmed'
    if (filter === 'rejected') return f.status === 'rejected'
    return f.classification === filter && f.status !== 'rejected'
  })

  return (
    <>
      {/* RK-02: the whole reason this queue exists. */}
      <Caution title="Someone else may share your name">
        A page that merely names you proves nothing. Nothing here is merged into your
        profile unless the identity match is “confirmed” or you accept it yourself, so
        read the signals below before you decide — that is the only thing standing
        between a namesake’s career and your CV.
      </Caution>

      {!enabled && (
        <div className="alert alert-info" style={{ marginTop: 12 }}>
          <Icon name="info" />
          <div>
            Online enrichment is switched off, so nothing new will be found. The findings
            below are what earlier runs collected; your decisions on them still hold.
          </div>
        </div>
      )}

      <div className="row row-wrap" style={{ margin: '16px 0 12px' }}>
        <h4 className="row" style={{ margin: 0, gap: 6 }}>
          <Icon name="search" />
          Findings
          <HelpTip term="online_finding" />
        </h4>
        {FILTERS.map((f) => (
          <span
            key={f.key}
            className={`chip clickable${filter === f.key ? ' on' : ''}`}
            onClick={() => setFilter(f.key)}
          >
            {f.label}
          </span>
        ))}
        <div className="spacer" />
        <span className="small muted">
          {shown.length} of {findings.length}
        </span>
      </div>

      {shown.length === 0 ? (
        <div className="phase-1">
          <Empty
            title={
              <>
                <span className="icon-chip icon-chip-lg phase-chip">
                  <Icon name={filter === 'pending' ? 'check' : 'search'} />
                </span>
                <span style={{ display: 'block' }}>Nothing in this view</span>
              </>
            }
          >
            {filter === 'pending'
              ? 'Every finding has been ruled on. Run enrichment again from the Enrichment tab to look for more.'
              : 'No finding matches this filter yet.'}
          </Empty>
        </div>
      ) : (
        shown.map((f) => (
          <FindingCard
            key={f.id}
            finding={f}
            busy={busy === `finding:${f.id}`}
            onConfirm={() => onDecide(f.id, 'confirm')}
            onReject={() => {
              setReason('')
              setRejecting(f)
            }}
          />
        ))
      )}

      {rejecting && (
        <Modal
          title="Reject this finding for good?"
          onClose={() => setRejecting(null)}
          actions={
            <>
              <button className="btn btn-sm" onClick={() => setRejecting(null)}>
                <Icon name="check" /> Keep it in the queue
              </button>
              <button
                className="btn btn-sm btn-danger"
                onClick={() => {
                  const f = rejecting
                  setRejecting(null)
                  // FR-124: a permanent rejection is recorded against the URL and
                  // the next enrichment run skips it entirely.
                  onDecide(f.id, 'reject', { permanent: true, reason: reason || null })
                }}
              >
                <Icon name="trash" /> Reject — never propose again
              </button>
            </>
          }
        >
          <p className="small">
            <strong>{rejecting.title || rejecting.url}</strong>
          </p>
          <p className="small muted">
            This URL will be remembered as rejected. Re-running enrichment will not
            re-propose it, and nothing on it will ever reach your composite profile.
          </p>
          <div className="field">
            <label>Why (optional)</label>
            <input
              type="text"
              value={reason}
              placeholder="Different person, wrong employer, outdated…"
              onChange={(e) => setReason(e.target.value)}
            />
            <span className="hint">Kept with the decision so you can see later why.</span>
          </div>
        </Modal>
      )}
    </>
  )
}

function FindingCard({ finding, busy, onConfirm, onReject }) {
  const [open, setOpen] = useState(false)
  const facts = finding.extracted_facts || {}
  const identity = finding.identity_signals || {}
  const signals = identity.signals || []
  const decided = finding.status !== 'pending'

  return (
    <div
      className={`card phase-1 finding finding-${finding.classification}${
        decided ? ' finding-decided' : ''
      }`}
    >
      <div className="card-header">
        <Icon name="browser" />
        <h3 style={{ minWidth: 0 }}>{finding.title || finding.url}</h3>
        <div className="spacer" />
        <Badge tone={CLASSIFICATION_TONE[finding.classification]}>
          <Icon name={CLASSIFICATION_ICON[finding.classification] || 'info'} />
          {finding.classification}
        </Badge>
        {finding.status === 'accepted' && (
          <Badge tone="ok">
            <Icon name="check" />
            You accepted this
          </Badge>
        )}
        {finding.status === 'rejected' && (
          <Badge tone="danger">
            <Icon name="x" />
            {finding.rejected_permanently ? 'Rejected — never re-proposed' : 'Rejected'}
          </Badge>
        )}
      </div>

      <div className="row row-wrap" style={{ marginBottom: 10 }}>
        <a className="small mono" href={finding.url} target="_blank" rel="noreferrer noopener">
          <Icon name="external" /> {finding.url}
        </a>
        {facts.page_kind && <Badge>{String(facts.page_kind).replace(/_/g, ' ')}</Badge>}
        {facts.extraction === 'heuristic' && (
          <Badge tone="warn">read without the AI — facts may be thin</Badge>
        )}
        {facts.special_category_removals > 0 && (
          <Badge tone="info">{facts.special_category_removals} sensitive details removed</Badge>
        )}
        <span className="small muted">{formatDate(finding.created_at)}</span>
      </div>

      <div className="row" style={{ marginBottom: 12 }}>
        <span className="small muted nowrap">
          Identity match
          <HelpTip term="identity_match" />
        </span>
        <Meter value={(finding.identity_score || 0) * 100} width={220} />
        {identity.rule && <span className="small muted">{identity.rule}</span>}
      </div>

      <h4 className="row" style={{ gap: 6 }}>
        <Icon name="document" />
        What the page says about you
      </h4>
      {(facts.facts || []).length === 0 ? (
        <p className="small muted">
          No facts were extracted. {facts.excerpt ? 'An excerpt is kept for scoring only.' : ''}
        </p>
      ) : (
        <ul className="help-tips" style={{ marginBottom: 12 }}>
          {facts.facts.slice(0, 8).map((fact, i) => (
            <li key={i}>
              {fact.statement || fact.text}
              {fact.category && <span className="muted"> · {fact.category}</span>}
            </li>
          ))}
        </ul>
      )}

      <button className="btn btn-sm btn-ghost" onClick={() => setOpen(!open)}>
        <Icon name={open ? 'minus' : 'eye'} />
        {open ? 'Hide the identity evidence' : `Why this was attributed to you (${signals.length} signals)`}
      </button>

      {open && (
        <div className="table-wrap" style={{ marginTop: 10 }}>
          <table>
            <thead>
              <tr>
                <th>Signal</th>
                <th className="num">Score</th>
                <th className="num">Weight</th>
                <th>What matched</th>
              </tr>
            </thead>
            <tbody>
              {signals.map((s) => (
                <tr key={s.signal}>
                  <td className="nowrap">{SIGNAL_LABEL[s.signal] || s.signal}</td>
                  <td className="num">
                    <Meter value={(s.score || 0) * 100} width={90} />
                  </td>
                  <td className="num">{s.weight ? s.weight.toFixed(2) : '–'}</td>
                  <td>
                    <div className="signal-detail">{s.detail}</div>
                    {(s.evidence || []).length > 0 && (
                      <div className="chips" style={{ marginTop: 4 }}>
                        {s.evidence.map((e, i) => (
                          <span className="chip" key={i}>
                            {e}
                          </span>
                        ))}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!decided && (
        <div className="row" style={{ marginTop: 14 }}>
          <button className="btn btn-primary btn-sm" disabled={busy} onClick={onConfirm}>
            <Icon name="check" /> This is me — merge it
          </button>
          <button className="btn btn-sm btn-danger" disabled={busy} onClick={onReject}>
            <Icon name="x" /> Not me — never propose again
          </button>
          {busy && <span className="spinner" />}
        </div>
      )}
    </div>
  )
}

