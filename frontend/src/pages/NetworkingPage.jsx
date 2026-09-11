/**
 * Networking and export (FR-461, FR-462, FR-463).
 *
 * Three things that all point outwards, away from the application:
 *
 *   FR-461  the warm routes into a target company, through people the job
 *           seeker already knows. Ranked per *company*, not per role, because
 *           a company with three open roles has one network around it.
 *   FR-462  the event radar: what is happening near enough to attend, who
 *           from a target company is named on the public page, and the
 *           calendar entry.
 *   FR-463  the campaign package: one file a career coach can read.
 *
 * The one distinction this screen has to hold on to, and states in as many
 * places as it can without nagging: the message drafted here goes to somebody
 * in *your* network asking them to introduce you. The introduction email to
 * the hiring contact is a different artefact and lives on the Applications
 * screen. Confusing the two would send a stranger a note written for a friend.
 */

import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { Caution, FirstRun, HelpTip, ScreenIntro } from '../components/Help'
import Icon from '../components/Icon'
import IntroductionRoutes from '../components/IntroductionRoutes'
import WorkflowMap from '../components/WorkflowMap'
import { ErrorBox, Loading, Tabs, useFetch } from '../components/ui'
import CampaignExport from './networking/CampaignExport'
import EventRadar from './networking/EventRadar'

export default function NetworkingPage() {
  const campaigns = useFetch(() => api.get('/campaigns'))
  const journey = useFetch(() => api.get('/overview/journey').catch(() => null))

  const [campaignId, setCampaignId] = useState('')
  const [tab, setTab] = useState('introductions')

  // The opportunities are re-read per campaign because the company list on the
  // introductions tab is derived from them: a target company is a company this
  // seeker has an opportunity at.
  const opportunities = useFetch(
    () =>
      api.get(
        `/opportunities?limit=200&sort=score${campaignId ? `&campaign_id=${encodeURIComponent(campaignId)}` : ''}`,
      ),
    [campaignId],
  )

  const campaignList = campaigns.data || []
  const items = opportunities.data?.items || []

  /** FR-461 works per target company; roles are kept so a draft can name one. */
  const companies = useMemo(() => {
    const byId = new Map()
    for (const opportunity of items) {
      if (!opportunity.company_id) continue
      const entry = byId.get(opportunity.company_id) || {
        company_id: opportunity.company_id,
        company_name: opportunity.company_name || 'Unnamed company',
        country: opportunity.company_country,
        roles: [],
      }
      entry.roles.push({
        id: opportunity.id,
        title: opportunity.title,
        kind: opportunity.kind,
        score: opportunity.score,
      })
      byId.set(opportunity.company_id, entry)
    }
    return [...byId.values()].sort((a, b) => a.company_name.localeCompare(b.company_name))
  }, [items])

  const loading = campaigns.loading || opportunities.loading
  const error = campaigns.error || opportunities.error
  // Three distinct states: still reading, failed, and genuinely nothing yet.
  const empty = !loading && !error && campaignList.length === 0 && companies.length === 0

  const tabs = [
    { key: 'introductions', label: 'Introduction routes', count: companies.length },
    { key: 'events', label: 'Event radar' },
    { key: 'export', label: 'Campaign export' },
  ]

  return (
    <div className="content-wide">
      <WorkflowMap journey={journey.data?.journey || {}} compact current="contacts" />
      <ScreenIntro pathname="/networking" />

      {/* NFR-305: the ranking orders a list, it does not decide anything. The
          backend says the same thing in its own `note` on each response. */}
      <Caution title="Everything here is advisory, and nothing is sent for you.">
        Routes are ranked by how likely somebody is to help and how close they sit to the
        decision. Whether to approach anyone at all is your call, and no message on this
        screen leaves the machine until you send it yourself from your own account.
      </Caution>

      <div className="card">
        <div className="row row-wrap">
          <label className="small muted" style={{ display: 'flex', alignItems: 'center' }}>
            Campaign
            <HelpTip title="Why the campaign matters here">
              The campaign decides which companies count as targets, which directives set your
              travel tolerance, and what an export contains. Leave it on the most recent one
              unless you are looking back at an older search.
            </HelpTip>
          </label>
          <select
            value={campaignId}
            onChange={(e) => setCampaignId(e.target.value)}
            style={{ maxWidth: 340 }}
          >
            <option value="">Most recent campaign</option>
            {campaignList.map((campaign) => (
              <option key={campaign.id} value={campaign.id}>
                {campaign.name} · {campaign.status}
              </option>
            ))}
          </select>
          <div className="spacer" />
          <span className="small muted">
            {companies.length} target {companies.length === 1 ? 'company' : 'companies'} in scope
          </span>
        </div>
      </div>

      {error && (
        <ErrorBox
          error={error}
          onRetry={() => {
            campaigns.reload()
            opportunities.reload()
          }}
        />
      )}

      {loading && <Loading rows={4} />}

      {empty && (
        <FirstRun
          pathname="/networking"
          action={
            <Link className="btn btn-primary" to="/campaigns">
              Start a campaign
            </Link>
          }
        >
          Networking works on the companies a campaign found for you, so there is nothing to
          rank yet. Run a campaign first; then import your own connections on the Contacts
          screen, and the routes into each company appear here.
        </FirstRun>
      )}

      {!loading && !error && !empty && (
        <>
          <Tabs tabs={tabs} active={tab} onChange={setTab} />

          {tab === 'introductions' && (
            <IntroductionRoutes campaignId={campaignId} companies={companies} />
          )}
          {tab === 'events' && <EventRadar campaignId={campaignId} />}
          {tab === 'export' && (
            <CampaignExport
              campaigns={campaignList}
              campaignId={campaignId}
              onPickCampaign={setCampaignId}
            />
          )}
        </>
      )}

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-header">
          <Icon name="info" />
          <h3>Two different messages, easy to confuse</h3>
        </div>
        <div className="grid grid-2">
          <div className="entry-card">
            <strong className="small">The introduction request — this screen</strong>
            <p className="small muted" style={{ margin: '6px 0 0', lineHeight: 1.55 }}>
              Written to somebody in your own network. It asks them to introduce you, names how
              you know each other, and needs their consent before anything happens. Warmer,
              slower, and it never claims a vacancy exists.
            </p>
          </div>
          <div className="entry-card">
            <strong className="small">The introduction email — Applications screen</strong>
            <p className="small muted" style={{ margin: '6px 0 0', lineHeight: 1.55 }}>
              Written to the hiring contact at the company, attached to a tailored CV, and sent
              from your own mailbox.{' '}
              <Link to="/applications">Open the Applications screen</Link> for that one.
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}
