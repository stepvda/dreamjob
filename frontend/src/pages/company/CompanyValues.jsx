/**
 * Values, working style, and how they compare with what you said you want
 * (FR-384).
 *
 * The comparison reports a mismatch only where the company's own material
 * contradicts a value you stated. Silence is reported as unknown - a question
 * for the interview rather than a warning - which is why the three columns are
 * kept apart instead of being folded into one percentage.
 */

import { HelpTip } from '../../components/Help'
import { Badge, Empty, Provenance } from '../../components/ui'

const SENTIMENT_TONE = { positive: 'ok', mixed: 'warn', negative: 'danger' }
const SEVERITY_TONE = { blocking: 'danger', serious: 'warn', minor: 'info' }

function CueList({ title, items, textKeys, tip }) {
  if (!items?.length) return null
  return (
    <div style={{ marginBottom: 14 }}>
      <div className="tiny muted" style={{ marginBottom: 6 }}>
        {title}
        {tip}
      </div>
      <div className="col" style={{ gap: 6 }}>
        {items.map((item, i) => {
          const text = textKeys.map((k) => item[k]).find(Boolean)
          return (
            <div key={i} className="row small">
              <span className="chip">{text}</span>
              {item.sentiment && (
                <Badge tone={SENTIMENT_TONE[item.sentiment]}>{item.sentiment}</Badge>
              )}
              {item.evidence && <span className="muted">“{item.evidence}”</span>}
              <div className="spacer" />
              <Provenance source={item.source} confidence={item.confidence} />
            </div>
          )
        })}
      </div>
    </div>
  )
}

function Finding({ finding }) {
  return (
    <div className="entry-card">
      <div className="entry-head">
        <strong>{finding.cue}</strong>
        {finding.severity && (
          <Badge tone={SEVERITY_TONE[finding.severity]}>{finding.severity}</Badge>
        )}
        {finding.polarity && <span className="tiny muted">{finding.polarity}</span>}
        <div className="spacer" />
        {finding.source && <span className="tiny muted">{finding.source}</span>}
      </div>
      <div className="small">{finding.explanation}</div>
      {finding.quote && (
        <div className="small muted" style={{ marginTop: 6 }}>
          “{finding.quote}”
        </div>
      )}
    </div>
  )
}

export default function CompanyValues({ profile, valuesMatch }) {
  const culture = profile.values_culture || {}
  const hasCulture = Boolean(
    (culture.stated_values || []).length ||
      (culture.working_style || []).length ||
      (culture.leadership_statements || []).length ||
      (culture.job_ad_language || []).length ||
      (culture.employer_review_themes || []).length,
  )

  return (
    <div className="stack">
      <div className="card">
        <div className="card-header">
          <h3>
            Values match
            <HelpTip term="values_match" />
          </h3>
          <div className="spacer" />
          {valuesMatch?.score != null && (
            <Badge tone={valuesMatch.score >= 0.7 ? 'ok' : valuesMatch.score >= 0.4 ? 'warn' : 'danger'}>
              {Math.round(valuesMatch.score * 100)}% of compared values align
            </Badge>
          )}
        </div>

        {!valuesMatch && (
          <p className="small muted" style={{ margin: 0 }}>
            No comparison is available. It needs both a dream-job statement that names what you
            want from an employer and values material read from this company.
          </p>
        )}

        {valuesMatch?.excluded_reason && (
          <div className="alert alert-info">
            <div>{valuesMatch.excluded_reason}</div>
          </div>
        )}

        {valuesMatch && (
          <>
            <div className="row row-wrap small muted" style={{ marginBottom: 12 }}>
              <span>{valuesMatch.counts?.aligned ?? 0} aligned</span>
              <span>· {valuesMatch.counts?.warnings ?? 0} mismatched</span>
              <span>· {valuesMatch.counts?.unknown ?? 0} not addressed</span>
              {valuesMatch.generated_by && <span>· read {valuesMatch.generated_by}</span>}
              {(valuesMatch.derived_from || []).length > 0 && (
                <span>· from {valuesMatch.derived_from.join(', ')}</span>
              )}
            </div>

            {(valuesMatch.warnings || []).length > 0 && (
              <>
                <div className="tiny muted" style={{ marginBottom: 6 }}>
                  Where the company's own material contradicts what you said you want
                </div>
                {valuesMatch.warnings.map((w, i) => (
                  <Finding key={i} finding={w} />
                ))}
              </>
            )}

            {(valuesMatch.aligned || []).length > 0 && (
              <>
                <div className="tiny muted" style={{ margin: '12px 0 6px' }}>
                  Where it lines up
                </div>
                {valuesMatch.aligned.map((a, i) => (
                  <Finding key={i} finding={a} />
                ))}
              </>
            )}

            {(valuesMatch.unknown || []).length > 0 && (
              <>
                <div className="tiny muted" style={{ margin: '12px 0 6px' }}>
                  Not addressed anywhere — worth asking at interview
                </div>
                <div className="chips">
                  {valuesMatch.unknown.map((u, i) => (
                    <span key={i} className="chip" title={u.explanation}>
                      {u.cue}
                    </span>
                  ))}
                </div>
              </>
            )}

            {valuesMatch.note && (
              <p className="tiny muted" style={{ marginTop: 12 }}>
                {valuesMatch.note}
              </p>
            )}
          </>
        )}
      </div>

      <div className="card">
        <div className="card-header">
          <h3>What the company says about itself</h3>
          <div className="spacer" />
          {(culture.derived_from || []).length > 0 && (
            <span className="small muted">read from {culture.derived_from.join(', ')}</span>
          )}
        </div>

        {!hasCulture ? (
          <Empty title="No values material was found">
            The comparison above needs stated values, working-style cues, leadership statements,
            job-advert language or employer reviews. None were read from this company's pages.
          </Empty>
        ) : (
          <>
            <CueList
              title="Stated values"
              items={culture.stated_values}
              textKeys={['value', 'name', 'text']}
            />
            <CueList
              title="Working style"
              tip={
                <HelpTip title="Working style">
                  Short phrases read from how the company describes working there — “autonomy”,
                  “consensus decision-making”, “on-site five days”. They are the cues the values
                  comparison actually matches against, so each keeps the sentence it came from.
                </HelpTip>
              }
              items={culture.working_style}
              textKeys={['cue', 'text']}
            />
            <CueList
              title="Language in its job adverts"
              items={culture.job_ad_language}
              textKeys={['cue', 'text']}
            />
            <CueList
              title="Themes in employer reviews"
              items={culture.employer_review_themes}
              textKeys={['theme', 'text']}
            />

            {(culture.leadership_statements || []).length > 0 && (
              <div>
                <div className="tiny muted" style={{ marginBottom: 6 }}>
                  Leadership statements
                </div>
                {culture.leadership_statements.map((l, i) => (
                  <div key={i} className="entry-card">
                    <div className="small">“{l.quote}”</div>
                    <div className="tiny muted" style={{ marginTop: 4 }}>
                      {[l.speaker, l.role].filter(Boolean).join(' · ')}
                      <Provenance source={l.source} />
                    </div>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
