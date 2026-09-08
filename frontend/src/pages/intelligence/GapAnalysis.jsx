/**
 * FR-381 - the gap analysis, as three things per gap.
 *
 * The requirement asks for a gap, a concrete closing action, an estimated
 * effort, and the opportunities where the gap was decisive. All four are laid
 * out on one card in that order, because the closing action is the only part
 * that is actionable and the decisive opportunities are the only part that is
 * evidence.
 *
 * `points_lost` is the price of the gap in the same units the ranked list is
 * scored in, so it is shown as a number next to each linked opportunity rather
 * than described in prose.
 */

import { Link } from 'react-router-dom'

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, Empty, formatDate, formatPercent } from '../../components/ui'

/** gap_analysis.py builds ids as `<dimension>:<subject>`; these are its six. */
const DIMENSION_LABEL = {
  skill: 'Skill',
  certification: 'Certification',
  language: 'Language',
  experience: 'Experience',
  leadership: 'Leadership scope',
  visibility: 'Public visibility',
}

/** _severity(): blocking above half the corpus, significant above a fifth. */
const SEVERITY_TONE = { blocking: 'danger', significant: 'warn', minor: undefined }

/** _effort(): months bucketed into a word, so the word is what leads. */
const EFFORT_ICON = { weeks: 'clock', months: 'clock', quarters: 'calendar', years: 'calendar' }

function Gap({ gap }) {
  const action = gap.closing_action || {}
  const effort = gap.effort || {}
  const links = gap.decisive_opportunities || []
  const demand = gap.demand || {}

  return (
    <div className={`card int-gap int-${gap.severity || 'significant'}`}>
      <div className="card-header">
        <h3>{gap.label}</h3>
        <div className="spacer" />
        <Badge tone={SEVERITY_TONE[gap.severity]}>{gap.severity || 'significant'}</Badge>
        <Badge>{DIMENSION_LABEL[gap.dimension] || gap.dimension}</Badge>
      </div>

      <p className="small muted" style={{ margin: '0 0 10px', lineHeight: 1.55 }}>
        {gap.evidence}
        {demand.postings != null && demand.corpus ? (
          <>
            {' '}
            <span className="nowrap">
              ({demand.postings}/{demand.corpus} postings
              {demand.share != null ? `, ${formatPercent(demand.share, 0)}` : ''})
            </span>
          </>
        ) : null}
      </p>

      {/* FR-381: the closing action has to name a thing to do, not a thing to be. */}
      <div className="int-action">
        <span className="int-action-label">
          <Icon name="target" /> What closes it
        </span>
        <strong>{action.action || 'No action derived'}</strong>
        {action.detail && <div style={{ marginTop: 4 }}>{action.detail}</div>}
        <div className="row row-wrap small" style={{ marginTop: 8 }}>
          {action.kind && <Badge tone="info">{action.kind}</Badge>}
          <span className="muted">
            <Icon name={EFFORT_ICON[effort.level] || 'clock'} />{' '}
            {effort.level || 'unknown'} effort
            {effort.months != null ? ` · about ${effort.months} months` : ''}
          </span>
          <HelpTip term="closing_effort" />
        </div>
        {effort.detail && (
          <div className="tiny muted" style={{ marginTop: 4 }}>
            {effort.detail}
          </div>
        )}
      </div>

      <div className="row small" style={{ marginBottom: 6 }}>
        <strong>
          Where it was decisive
          <HelpTip term="decisive_opportunity" />
        </strong>
        <div className="spacer" />
        {gap.total_points_lost != null && (
          <span className="muted nowrap">{gap.total_points_lost} profile-fit points lost</span>
        )}
      </div>

      {links.length === 0 ? (
        <p className="tiny muted" style={{ margin: 0 }}>
          No opportunity in this campaign measurably lost points on this dimension. The gap is
          derived from the profile and the dream-job model, not from the ranked list.
        </p>
      ) : (
        <div className="int-decisive">
          {links.map((link) => (
            <div key={link.opportunity_id} className="int-decisive-row">
              <div>
                {/* FR-283: the link through to the opportunity the gap cost points on. */}
                <Link to={`/opportunities/${link.opportunity_id}`} className="int-decisive-title">
                  {link.title || 'Untitled opportunity'}
                </Link>
                <div className="tiny muted">{link.company_name || 'Company unknown'}</div>
                <div className="tiny muted int-why">{link.why}</div>
              </div>
              <div className="int-decisive-figs">
                <span className="int-points">−{link.points_lost}</span>
                <span className="tiny muted nowrap">
                  {link.sub_score === 'profile_fit' ? 'profile fit' : link.sub_score}
                  {link.sub_score_value != null ? ` ${Math.round(link.sub_score_value)}` : ''}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}

      {gap.source === 'llm+deterministic' && (
        <div className="tiny muted" style={{ marginTop: 8 }}>
          The wording of the action was sharpened by the language model; the gap, the dimension
          and every number above are computed (CR-405).
        </div>
      )}
    </div>
  )
}

export default function GapAnalysis({ analysis, runResult, onRun, running, hasCampaign }) {
  const gaps = analysis?.gaps || []
  const computedAt = analysis?.updated_at || analysis?.created_at

  return (
    <>
      <div className="card">
        <div className="card-header">
          <Icon name="chart" />
          <h3>
            What stands between your profile and the dream job
            <HelpTip term="gap_dimension" />
          </h3>
          <div className="spacer" />
          <button className="btn btn-primary btn-sm" disabled={running || !hasCampaign} onClick={onRun}>
            {running ? <span className="spinner" /> : analysis ? 'Recompute' : 'Run the gap analysis'}
          </button>
        </div>

        {analysis?.summary && <p className="advice-rationale">{analysis.summary}</p>}

        <div className="row row-wrap small muted">
          {computedAt && <span>Computed {formatDate(computedAt)}</span>}
          {analysis?.generated_by && <Badge>{analysis.generated_by}</Badge>}
          {gaps.length > 0 && (
            <span>
              {gaps.length} gap{gaps.length === 1 ? '' : 's'} ·{' '}
              {gaps.filter((g) => (g.decisive_opportunities || []).length).length} with linked
              opportunities
            </span>
          )}
        </div>

        {/* NFR-104: a recompute without the model still produces the analysis. */}
        {(runResult?.warnings || []).map((w, i) => (
          <div className="alert alert-warn" key={i}>
            <div>{w}</div>
          </div>
        ))}

        {runResult?.market && (
          <p className="tiny muted" style={{ margin: '10px 0 0' }}>
            Read from {runResult.market.opportunities} collected opportunit
            {runResult.market.opportunities === 1 ? 'y' : 'ies'} (
            {runResult.market.postings_with_required_skills} of them list required skills) against{' '}
            {runResult.market.skills_held} skills your profile evidences.
          </p>
        )}
      </div>

      {gaps.length === 0 ? (
        <Empty title="No gaps computed yet">
          {hasCampaign
            ? 'Run the analysis to compare your composite profile and dream-job model against the postings this campaign collected. It works without the language model — a model only sharpens the wording.'
            : 'The analysis reads a campaign’s collected postings. Create and run a campaign first, then come back here.'}
        </Empty>
      ) : (
        <div className="stack" style={{ marginTop: 14 }}>
          {gaps.map((gap) => (
            <Gap key={gap.id} gap={gap} />
          ))}
        </div>
      )}
    </>
  )
}
