/**
 * FR-384 - companies whose values conflict with the dream-job statement.
 *
 * There is no "list every conflict" endpoint: FR-384 is served per company at
 * GET /api/intelligence/values-match/{company_id}, because the company profile
 * is where the warning has to appear. This screen therefore takes the
 * companies behind the current ranked list and asks about each of them, then
 * shows only the ones that came back with a mismatch.
 *
 * The backend's own rule is repeated in the layout rather than in a sentence:
 * a mismatch is reported only where the company's own material contradicts
 * you, and silence comes back as `unknown` - a question for the interview,
 * which is why unknowns are listed quietly and never as warnings (CR-405).
 */

import { Link } from 'react-router-dom'

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, Empty, ErrorBox, Loading, Meter, Provenance } from '../../components/ui'

/** values_match.py: blocking when the dream-job cue was marked hard. */
const SEVERITY_TONE = { blocking: 'danger', warning: 'warn', caution: undefined }

function Conflict({ match }) {
  const warnings = match.warnings || []
  const unknown = match.unknown || []

  return (
    <div className={`card int-values ${match.has_blocking_warning ? 'int-blocking' : 'int-significant'}`}>
      <div className="card-header">
        <Icon name="companies" />
        <h3>
          <Link to={`/companies/${match.company_id}`}>{match.company_name || 'Company'}</Link>
        </h3>
        <div className="spacer" />
        {match.has_blocking_warning && <Badge tone="danger">Conflicts with a hard requirement</Badge>}
        <Badge tone="warn">
          {warnings.length} mismatch{warnings.length === 1 ? '' : 'es'}
        </Badge>
      </div>

      <div className="row row-wrap" style={{ marginBottom: 10 }}>
        <span className="small muted nowrap">Alignment</span>
        <Meter value={match.score == null ? null : match.score * 100} width={160} />
        <span className="tiny muted">
          {match.counts?.aligned ?? 0} aligned · {match.counts?.warnings ?? 0} mismatched ·{' '}
          {match.counts?.unknown ?? 0} unknown
        </span>
        {match.generated_by && <Badge>{match.generated_by}</Badge>}
      </div>

      {warnings.map((w, i) => (
        <div className="conflict unresolved" key={`${w.cue}-${i}`} style={i ? { marginTop: 10 } : undefined}>
          <div className="row row-wrap" style={{ marginBottom: 5 }}>
            <strong>{w.cue}</strong>
            <Badge tone={SEVERITY_TONE[w.severity]}>{w.severity || 'warning'}</Badge>
            <Badge tone="info">{w.polarity === 'avoid' ? 'you said avoid' : 'you said seek'}</Badge>
          </div>
          <div className="small" style={{ lineHeight: 1.55 }}>
            {w.explanation}
          </div>
          {w.quote && (
            <div className="tiny muted" style={{ marginTop: 5, fontStyle: 'italic' }}>
              Your words: “{w.quote}”
            </div>
          )}
          {(w.company_cues || []).length > 0 && (
            <div className="int-cues">
              {w.company_cues.map((c, ci) => (
                <div className="int-cue" key={ci}>
                  <div className="tiny">
                    <Badge>{c.kind}</Badge>
                    {c.sentiment && <Badge tone={c.sentiment === 'negative' ? 'warn' : undefined}>{c.sentiment}</Badge>}
                  </div>
                  <div className="small" style={{ marginTop: 3 }}>
                    {c.cue}
                  </div>
                  {c.evidence && <div className="tiny muted">{c.evidence}</div>}
                  <Provenance source={c.source} />
                </div>
              ))}
            </div>
          )}
        </div>
      ))}

      {unknown.length > 0 && (
        <p className="tiny muted" style={{ margin: '10px 0 0' }}>
          Nothing was found either way on: {unknown.map((u) => u.cue).join(', ')}. Ask about those
          in the interview rather than reading silence as agreement.
        </p>
      )}

      {(match.derived_from || []).length > 0 && (
        <div className="tiny muted" style={{ marginTop: 8 }}>
          Read from {match.derived_from.join(', ')}
          {match.confidence != null ? ` · confidence ${Math.round(match.confidence * 100)}%` : ''}
        </div>
      )}
    </div>
  )
}

export default function ValuesConflicts({
  matches,
  loading,
  error,
  onLoad,
  checkedCount,
  companyCount,
  excludedCount,
  hasCampaign,
}) {
  const conflicted = (matches || []).filter((m) => (m.warnings || []).length > 0)
  const clean = (matches || []).filter((m) => (m.warnings || []).length === 0)

  return (
    <>
      <div className="card">
        <div className="card-header">
          <Icon name="warning" />
          <h3>
            Where a company contradicts what you said you want
            <HelpTip term="values_match" />
          </h3>
          <div className="spacer" />
          <button className="btn btn-sm" disabled={loading || !companyCount} onClick={onLoad}>
            {loading ? <span className="spinner" /> : matches ? 'Re-check' : 'Check the companies'}
          </button>
        </div>

        <p className="small muted" style={{ margin: 0, lineHeight: 1.6 }}>
          Each company behind your ranked list is compared against your dream-job statement. A
          mismatch is reported only where the company’s own material — its stated values, how it
          describes working there, its job adverts, its reviews — contradicts something you asked
          for. Where a company says nothing, that is reported as unknown, not as a warning.
        </p>

        {matches && (
          <div className="row row-wrap small muted" style={{ marginTop: 10 }}>
            <span>
              {checkedCount} of {companyCount} compan{companyCount === 1 ? 'y' : 'ies'} checked
            </span>
            <span>· {conflicted.length} with a mismatch</span>
            <span>· {clean.length} with none</span>
            {/* FR-385: a company discretion mode excludes is not surfaced here at all. */}
            {excludedCount > 0 && <span>· {excludedCount} withheld by discretion mode</span>}
          </div>
        )}
      </div>

      {loading && <Loading rows={3} />}
      {error && <ErrorBox error={error} onRetry={onLoad} />}

      {!loading && !error && !matches && (
        <Empty title="Not checked yet">
          {hasCampaign
            ? 'This runs one comparison per company, so it is on a button rather than automatic. Check them and anything that contradicts your statement appears here.'
            : 'The comparison needs companies from a campaign’s ranked list. Create and run a campaign first.'}
        </Empty>
      )}

      {!loading && !error && matches && conflicted.length === 0 && (
        <Empty title="No conflicts found">
          None of the {checkedCount} companies checked says anything that contradicts your
          dream-job statement. That is not the same as agreeing with it — most companies simply
          say nothing, and those silences are listed on each company’s own profile.
        </Empty>
      )}

      {conflicted.length > 0 && (
        <div className="stack" style={{ marginTop: 14 }}>
          {conflicted.map((m) => (
            <Conflict key={m.company_id} match={m} />
          ))}
        </div>
      )}

      {clean.length > 0 && (
        <div className="card">
          <div className="card-header">
            <h3>No contradiction found</h3>
          </div>
          <div className="chips">
            {clean.map((m) => (
              <Link className="chip clickable" key={m.company_id} to={`/companies/${m.company_id}`}>
                {m.company_name || m.company_id}
              </Link>
            ))}
          </div>
        </div>
      )}
    </>
  )
}
