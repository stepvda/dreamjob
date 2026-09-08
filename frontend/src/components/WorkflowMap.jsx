/**
 * The journey map: profile → plan → discover → apply → follow up.
 *
 * Dream Job is a ten-stage pipeline (specification section 2.3) and the single
 * most common way to get lost in it is not knowing which stage you are in or
 * what unblocks the next one. This component is the answer to "where am I?" —
 * it is rendered on the overview screen at full size and as a compact strip at
 * the top of each stage's own screen.
 *
 * Every node reports real state from the API, not a static picture: what is
 * done, what is running, what is blocked and why, and what it produced. A node
 * you can act on is a link to the screen that acts on it.
 */

import { Link } from 'react-router-dom'

import { HelpTip } from './Help'
import Icon from './Icon'

/**
 * The pipeline, grouped into the five phases users actually think in.
 * `key` matches the state keys returned by GET /api/overview/journey.
 */
export const PHASES = [
  {
    id: 'profile',
    label: 'Profile',
    caption: 'Who you are, and what you want',
    stages: [
      { key: 'profile', icon: 'profile', label: 'Profile intake', to: '/profile', hint: 'LinkedIn export and CV, merged and versioned' },
      { key: 'composite', icon: 'composite', label: 'Composite profile', to: '/composite', hint: 'Synthesised and evidence-traced', term: 'composite_profile' },
      { key: 'dream_job', icon: 'dream', label: 'Dream job', to: '/dream-job', hint: 'Your statement, turned into a model' },
    ],
  },
  {
    id: 'plan',
    label: 'Plan',
    caption: 'Where to look, and how much',
    stages: [
      { key: 'directives', icon: 'directives', label: 'Directives', to: '/directives', hint: 'Structured search constraints', term: 'directive' },
      { key: 'plan', icon: 'campaign', label: 'Campaign plan', to: '/campaigns', hint: 'Per-source queries, volume and cost' },
      { key: 'collection', icon: 'refresh', label: 'Collection', to: '/campaigns', hint: 'Job boards, registries, company sites' },
    ],
  },
  {
    id: 'discover',
    label: 'Discover',
    caption: 'What the market actually holds',
    stages: [
      { key: 'companies', icon: 'companies', label: 'Company profiles', to: '/companies', hint: 'Standardised profiles and five-year financials' },
      { key: 'opportunities', icon: 'opportunities', label: 'Opportunities', to: '/opportunities', hint: 'Vacancies and speculative openings', term: 'speculative_opening' },
      { key: 'scoring', icon: 'chart', label: 'Ranking', to: '/opportunities', hint: 'Explainable scores you can override' },
    ],
  },
  {
    id: 'apply',
    label: 'Apply',
    caption: 'Reaching the right person',
    stages: [
      { key: 'contacts', icon: 'contacts', label: 'Hiring contacts', to: '/contacts', hint: 'Found, validated, or a warm introduction', term: 'reachability' },
      { key: 'documents', icon: 'document', label: 'Documents', to: '/applications', hint: 'Tailored CV, briefing, motivation' },
      { key: 'dispatch', icon: 'send', label: 'Dispatch', to: '/applications', hint: 'Sent from your own mailbox' },
    ],
  },
  {
    id: 'followup',
    label: 'Follow up',
    caption: 'What came back, and what it teaches',
    stages: [
      { key: 'responses', icon: 'responses', label: 'Responses', to: '/pipeline', hint: 'Replies captured and classified' },
      { key: 'pipeline', icon: 'pipeline', label: 'Pipeline', to: '/pipeline', hint: 'Interview, offer, outcome' },
      { key: 'learning', icon: 'insights', label: 'What works', to: '/pipeline', hint: 'Patterns across outcomes, and where to redirect' },
    ],
  },
]

const STATE_LABEL = {
  done: 'Complete',
  active: 'In progress',
  ready: 'Ready to start',
  blocked: 'Waiting on an earlier stage',
  pending: 'Not started',
}

/**
 * @param journey  { [stageKey]: { state, count?, detail?, blockedBy? } }
 * @param compact  strip form, for the top of a stage's own screen
 * @param current  stage key to highlight as "you are here"
 */
export default function WorkflowMap({ journey = {}, compact = false, current }) {
  if (compact) return <WorkflowStrip journey={journey} current={current} />

  return (
    <div className="wfmap">
      {PHASES.map((phase, pi) => (
        <div className="wfmap-phase" key={phase.id}>
          <div className="wfmap-phase-head">
            <span className="wfmap-phase-index">{pi + 1}</span>
            <div>
              <div className="wfmap-phase-label">{phase.label}</div>
              <div className="wfmap-phase-caption">{phase.caption}</div>
            </div>
          </div>

          <div className="wfmap-stages">
            {phase.stages.map((stage) => {
              const s = journey[stage.key] || {}
              const state = s.state || 'pending'
              const clickable = state !== 'blocked'
              const body = (
                <>
                  <span className={`wfmap-dot wfmap-dot-${state}`} aria-hidden>
                    {state === 'done' ? '✓' : state === 'active' ? '' : ''}
                  </span>
                  <span className="wfmap-stage-text">
                    <span className="wfmap-stage-label">
                      <Icon name={stage.icon} />
                      {stage.label}
                      {stage.term && <HelpTip term={stage.term} />}
                    </span>
                    <span className="wfmap-stage-hint">
                      {state === 'blocked' && s.blockedBy
                        ? `Needs ${s.blockedBy} first`
                        : s.detail || stage.hint}
                    </span>
                  </span>
                  {s.count != null && <span className="badge">{s.count}</span>}
                </>
              )

              const className =
                `wfmap-stage wfmap-${state}` +
                (current === stage.key ? ' wfmap-current' : '')

              return clickable ? (
                <Link
                  to={stage.to}
                  key={stage.key}
                  className={className}
                  title={STATE_LABEL[state]}
                >
                  {body}
                </Link>
              ) : (
                <div key={stage.key} className={className} title={STATE_LABEL[state]}>
                  {body}
                </div>
              )
            })}
          </div>

          {pi < PHASES.length - 1 && (
            <svg className="wfmap-arrow" viewBox="0 0 24 14" aria-hidden focusable="false">
              <path
                d="M1 7h18M14 2l6 5-6 5"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          )}
        </div>
      ))}
    </div>
  )
}

/**
 * Compact strip. Same states, one line, for the top of a working screen where
 * the full map would take space the actual work needs.
 */
function WorkflowStrip({ journey, current }) {
  return (
    <nav className="wfstrip" aria-label="Where this screen sits in the process">
      {PHASES.map((phase, pi) => {
        const states = phase.stages.map((s) => journey[s.key]?.state || 'pending')
        const state = states.every((x) => x === 'done')
          ? 'done'
          : states.some((x) => x === 'active')
            ? 'active'
            : states.some((x) => x === 'done' || x === 'ready')
              ? 'ready'
              : 'pending'
        const here = phase.stages.some((s) => s.key === current)

        return (
          <div className="wfstrip-item" key={phase.id}>
            <Link
              to={phase.stages[0].to}
              className={`wfstrip-node wfmap-${state}${here ? ' wfmap-current' : ''}`}
            >
              <span className={`wfmap-dot wfmap-dot-${state}`} aria-hidden>
                {state === 'done' ? '✓' : ''}
              </span>
              {phase.label}
            </Link>
            {pi < PHASES.length - 1 && <span className="wfstrip-sep" aria-hidden>›</span>}
          </div>
        )
      })}
    </nav>
  )
}

/**
 * Derive journey state on the client when the overview endpoint has not been
 * called — used so a screen embedding the compact strip does not need its own
 * fetch. Pass whatever the screen already knows.
 */
export function deriveJourney({
  profile,
  composite,
  dreamJob,
  directives,
  campaign,
  counts = {},
} = {}) {
  const j = {}
  const set = (k, ok, extra = {}) => {
    j[k] = { state: ok ? 'done' : 'pending', ...extra }
  }

  set('profile', Boolean(profile?.id))
  set('composite', Boolean(composite?.id))
  set('dream_job', Boolean(dreamJob?.confirmed_by_user))
  set('directives', Boolean(directives?.id))
  set('plan', Boolean(campaign?.status && campaign.status !== 'draft'))
  set('collection', campaign?.status === 'completed', {
    state: campaign?.status === 'running' ? 'active' : campaign?.status === 'completed' ? 'done' : 'pending',
  })
  set('companies', (counts.companies || 0) > 0, { count: counts.companies })
  set('opportunities', (counts.opportunities || 0) > 0, { count: counts.opportunities })
  set('scoring', (counts.scored || 0) > 0, { count: counts.scored })
  set('contacts', (counts.contacts || 0) > 0, { count: counts.contacts })
  set('documents', (counts.packages || 0) > 0, { count: counts.packages })
  set('dispatch', (counts.sent || 0) > 0, { count: counts.sent })
  set('responses', (counts.replies || 0) > 0, { count: counts.replies })
  set('pipeline', (counts.cards || 0) > 0, { count: counts.cards })
  set('learning', (counts.sent || 0) >= 5, {
    detail: (counts.sent || 0) < 5 ? `${counts.sent || 0} of 5 applications needed` : undefined,
  })
  return j
}
