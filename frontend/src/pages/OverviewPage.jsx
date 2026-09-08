/**
 * Overview - "Where I am". The landing screen.
 *
 * Dream Job is a ten-stage pipeline and the commonest way to get lost in it is
 * not knowing which stage you are in or what unblocks the next one. So this
 * screen answers three questions, in this order: what should I do next, where
 * am I in the process, and what has the process actually produced.
 *
 * The map itself is <WorkflowMap>, which is shared with every working screen.
 * Everything here is the frame around it.
 *
 * Colour follows the journey: the frame around the map is system slate
 * (phase-0), while each headline count carries the hue of the screen it points
 * at, so the row of numbers reads as the same spectrum as the map beneath it.
 *
 * One endpoint serves the whole screen: GET /api/overview/journey, which
 * returns { journey, counts, campaign, discretion_mode, next_action }.
 */

import { useEffect } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { api } from '../api/client'
import { Caution, FirstRun, HelpTip, ScreenIntro } from '../components/Help'
import Icon from '../components/Icon'
import WorkflowMap from '../components/WorkflowMap'
import {
  Badge,
  ErrorBox,
  Loading,
  Stat,
  formatMoney,
  phaseOf,
  useFetch,
} from '../components/ui'

/** NFR-502: a running campaign has to report progress without being reopened. */
const POLL_MS = 5000

const CAMPAIGN_TONE = {
  running: 'accent',
  planned: 'info',
  paused: 'warn',
  completed: 'ok',
  cancelled: undefined,
  failed: 'danger',
  draft: undefined,
}

export default function OverviewPage() {
  const { data, error, loading, reload, setData } = useFetch(() =>
    api.get('/overview/journey'),
  )

  const campaign = data?.campaign || null
  const live = campaign?.status === 'running'

  /*
   * NFR-502: progress on long-running work is visible without a manual
   * refresh. Polling is tied to the campaign status, so the effect tears the
   * interval down the moment the campaign stops running - a finished campaign
   * must not keep the screen calling the API forever. The refresh writes
   * straight into the fetch state rather than going through `reload`, so the
   * screen never drops back to its loading skeleton under the user.
   */
  useEffect(() => {
    if (!live) return undefined
    const id = setInterval(() => {
      api
        .get('/overview/journey')
        .then(setData)
        .catch(() => {
          /* A dropped poll is not worth an error state; the next one recovers. */
        })
    }, POLL_MS)
    return () => clearInterval(id)
  }, [live, setData])

  if (loading) {
    return (
      <div className="content-wide">
        <ScreenIntro pathname="/overview" />
        <Loading rows={4} />
      </div>
    )
  }

  if (error) {
    return (
      <div className="content-wide">
        <ScreenIntro pathname="/overview" />
        <ErrorBox error={error} onRetry={reload} />
      </div>
    )
  }

  if (!data) return null

  const journey = data.journey || {}
  const counts = data.counts || {}
  const next = data.next_action || null

  /*
   * The empty state is the whole screen for a brand-new account: nothing is
   * finished and nothing is running, so there are no counts worth showing and
   * no campaign to report. What such a user needs is the first step, not four
   * zeroes.
   */
  const started = Object.values(journey).some(
    (s) => s?.state === 'done' || s?.state === 'active',
  )

  // The router returns the most recent campaign whatever its status; a draft
  // has nothing to report that the map does not already say.
  const showCampaign = campaign && campaign.status !== 'draft'

  return (
    <div className="content-wide">
      <ScreenIntro pathname="/overview" />

      {/* FR-385: if the search is being run discreetly, say so plainly. */}
      {data.discretion_mode && <DiscretionNotice />}

      {!started ? (
        <>
          <FirstRun
            pathname="/overview"
            action={
              next ? (
                <Link className="btn btn-primary btn-lg" to={next.to}>
                  <Icon name="play" />
                  {next.label}
                </Link>
              ) : (
                <Link className="btn btn-primary btn-lg" to="/profile">
                  <Icon name="profile" />
                  Start with your profile
                </Link>
              )
            }
          />

          <div className="card phase-0 phase-edge" style={{ marginTop: 14 }}>
            <div className="card-header">
              <Icon name="overview" />
              <h3>The whole process</h3>
              <HelpTip term="journey_state" />
              <div className="spacer" />
              <span className="small muted">
                Each stage unlocks when the one before it has what it needs.
              </span>
            </div>
            <WorkflowMap journey={journey} current={next?.stage} />
          </div>
        </>
      ) : (
        <>
          <NextAction next={next} />

          {showCampaign && <CampaignCard campaign={campaign} live={live} />}

          <div className="card phase-0 phase-edge" style={{ marginTop: 14 }}>
            <div className="card-header">
              <Icon name="overview" />
              <h3>Where you are</h3>
              <HelpTip term="journey_state" />
              <div className="spacer" />
              <span className="small muted">
                Every stage links to the screen that advances it.
              </span>
            </div>
            {/* The stage you are being sent to is also the "you are here" mark. */}
            <WorkflowMap journey={journey} current={next?.stage} />
          </div>

          <h3
            className="phase-0"
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              margin: '24px 0 10px',
            }}
          >
            <Icon name="chart" className="phase-ink" />
            What the search has produced
          </h3>
          <Counts counts={counts} />

          {/* NFR-305 / CR-405: the whole screen is advice. Say so where the
              numbers are, not only in the help drawer. */}
          <div style={{ marginTop: 16 }}>
            <Caution title="These figures rank and suggest; they never decide.">
              The suggested next action, the ranking behind these counts and every
              score in the application are advisory. Nothing is collected, written
              or sent without you asking for it, and no opportunity is ever
              discarded on your behalf.
            </Caution>
          </div>
        </>
      )}
    </div>
  )
}

/* --- The one thing to do next --------------------------------------------- */

/**
 * The suggested next action (`next_action`), which the API derives as the first
 * stage in pipeline order that is ready or already running. It is deliberately
 * the loudest thing on the screen: a user who does not know what to do next
 * should not have to read the map to find out.
 */
function NextAction({ next }) {
  if (!next) {
    return (
      <div className="card next-action phase-0">
        <div className="next-action-body">
          <div className="next-action-eyebrow">
            <Icon name="target" />
            <span style={{ marginLeft: 6 }}>Suggested next action</span>
            <HelpTip term="next_action" />
          </div>
          <h3>Nothing is waiting on you</h3>
          <p>
            Every stage that can move has either finished or is running. Follow up
            on what comes back, or plan another campaign when you want more to
            choose from.
          </p>
        </div>
        <Link className="btn btn-lg" to="/campaigns">
          <Icon name="campaign" />
          Plan a campaign
        </Link>
      </div>
    )
  }

  return (
    <div className="card next-action phase-0">
      <div className="next-action-body">
        <div className="next-action-eyebrow">
          <Icon name="target" />
          <span style={{ marginLeft: 6 }}>Suggested next action</span>
          <HelpTip term="next_action" />
        </div>
        <h3>{next.label}</h3>
        <p>
          This is the earliest stage in the process that is ready to move. Nothing
          later can finish until it does.
        </p>
      </div>
      <Link className="btn btn-primary btn-lg" to={next.to}>
        <Icon name="play" />
        Continue
      </Link>
    </div>
  )
}

/* --- Live campaign (NFR-502) ---------------------------------------------- */

/*
 * A campaign is Plan work (phase-2), so the card wears the plan hue even here
 * on the slate overview: the colour says which part of the journey is running,
 * which is the whole point of the spectrum.
 */
function CampaignCard({ campaign, live }) {
  const used = campaign.tokens_used ?? 0
  const budget = campaign.token_budget ?? 0
  const pct = budget > 0 ? Math.min(100, (used / budget) * 100) : null

  return (
    <div className="card phase-2 phase-edge" style={{ marginTop: 14 }}>
      <div className="card-header">
        <Icon name="campaign" />
        <h3>{campaign.name || 'Campaign'}</h3>
        <Badge tone={CAMPAIGN_TONE[campaign.status]}>{campaign.status}</Badge>
        {live && (
          <Badge tone="accent">
            <Icon name="refresh" />
            Live · refreshed every 5 seconds
          </Badge>
        )}
        <div className="spacer" />
        <Link className="btn btn-sm" to={`/campaigns/${campaign.id}`}>
          <Icon name="external" />
          Open campaign
        </Link>
      </div>

      {campaign.stage && (
        <p className="small muted" style={{ margin: '0 0 12px' }}>
          Current stage: {campaign.stage}
        </p>
      )}

      <div className="budget-row">
        <span className="small">
          Token budget consumed
          <HelpTip term="token_budget" />
        </span>
        <span className="mono">
          {used.toLocaleString()} / {budget.toLocaleString()}
        </span>
      </div>

      <div className="progress-track">
        <div
          className={`progress-fill${pct == null && live ? ' indeterminate' : ''}`}
          style={pct == null ? undefined : { width: `${pct}%` }}
        />
      </div>

      <div className="row row-wrap small muted" style={{ marginTop: 8 }}>
        <span>{pct == null ? 'No budget set' : `${Math.round(pct)}% consumed`}</span>
        <span className="row" style={{ gap: 5 }}>
          <Icon name="money" />
          {formatMoney(campaign.cost_eur, 'EUR')} spent so far
        </span>
        <div className="spacer" />
        {/* The budget sheds optional work before it stops: worth a warning. */}
        {pct != null && pct >= 90 && (
          <Badge tone="warn">
            <Icon name="warning" />
            Budget nearly spent
          </Badge>
        )}
      </div>
    </div>
  )
}

/* --- Headline counts ------------------------------------------------------ */

/**
 * The four numbers that say how far the search has got. Each is a link: a
 * number you cannot act on is trivia.
 */
function Counts({ counts }) {
  const sent = counts.sent ?? 0

  return (
    <div className="grid grid-4">
      <Count
        icon="opportunities"
        to="/opportunities"
        value={counts.opportunities ?? 0}
        label="Opportunities"
        sub={
          <>
            <span>{counts.speculative ?? 0} speculative</span>
            <HelpTip term="speculative_opening" />
            <span>· {counts.scored ?? 0} ranked</span>
          </>
        }
      />

      <Count
        icon="applications"
        to="/applications"
        value={sent}
        label="Applications sent"
        tip={
          <HelpTip title="Applications sent">
            Counted per dispatch that actually left your mailbox. A package you
            have written but not approved is not sent, and nothing is ever sent
            on your behalf without your approval.
          </HelpTip>
        }
        sub={
          <span>
            {counts.approved ?? 0} approved of {counts.packages ?? 0} prepared
          </span>
        }
      />

      <Count
        icon="responses"
        to="/responses"
        value={counts.replies ?? 0}
        label="Responses received"
        tip={
          <HelpTip title="Responses received" align="right">
            Replies to a connected Gmail account are detected automatically.
            Anything else - a phone call, a LinkedIn message, an ATS portal - you
            record yourself on the Responses screen, and rejections count as much
            as good news.
          </HelpTip>
        }
        sub={<span>{sent ? `out of ${sent} sent` : 'nothing sent yet'}</span>}
      />

      <Count
        icon="pipeline"
        to="/pipeline"
        value={counts.interviews ?? 0}
        label="Interviews and offers"
        tip={
          <HelpTip title="Interviews and offers" align="right">
            Applications whose pipeline card has reached the interview or offer
            stage. Cards move on their own when a reply is classified; you can
            always drag one to correct it.
          </HelpTip>
        }
        sub={<span>{counts.cards ?? 0} on the board</span>}
      />
    </div>
  )
}

/**
 * One headline number: the shared <Stat> tile, tinted with the phase of the
 * screen it points at, plus the footnote that keeps the number honest.
 */
function Count({ icon, to, value, label, tip, sub }) {
  const navigate = useNavigate()

  /*
   * <Stat> renders a real anchor, which is what a link should be - it can be
   * opened in a new tab or copied. A plain click would reload the whole
   * application, though, so an unmodified left click routes instead, exactly
   * as <Link> did.
   */
  const go = (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return
    e.preventDefault()
    navigate(to)
  }

  return (
    <div>
      <Stat
        icon={icon}
        to={to}
        onClick={go}
        phase={phaseOf(to)}
        value={value}
        label={
          <>
            {label}
            {tip}
          </>
        }
      />
      {sub && (
        <div className="stat-sub" style={{ padding: '0 15px' }}>
          {sub}
        </div>
      )}
    </div>
  )
}

/* --- Discretion mode (FR-385) --------------------------------------------- */

function DiscretionNotice() {
  return (
    <div className="alert alert-info">
      <span style={{ flex: 'none', marginTop: 1 }}>
        <Icon name="lock" size={16} />
      </span>
      <div>
        <strong>Discretion mode is on.</strong>
        <HelpTip term="discretion_mode" />
        <div style={{ marginTop: 4 }}>
          Your current employer, its group companies and every company you flagged
          are excluded from collection, ranking and messaging, and contacts who
          would expose the search are never approached. Nothing on LinkedIn signals
          that you are looking. Change this in your{' '}
          <Link to="/directives">search directives</Link>.
        </div>
      </div>
    </div>
  )
}
