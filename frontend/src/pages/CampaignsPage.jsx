/**
 * Campaigns (FR-161..166, FR-185).
 *
 * One campaign is one run of the collection and analysis pipeline under one
 * directive set. This screen is the ledger of those runs - what each one cost
 * in tokens and money, how far it got, and how long it took - plus the one
 * action that starts a new one.
 *
 * The substantial screen is the detail page: the plan review, the
 * knowledge-base saving and the live dashboard all live there, because they
 * only make sense for a single campaign.
 */

import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { api } from '../api/client'
import { Caution, FirstRun, HelpTip, ScreenIntro } from '../components/Help'
import Icon from '../components/Icon'
import WorkflowMap from '../components/WorkflowMap'
import {
  Badge,
  ErrorBox,
  Field,
  Loading,
  Meter,
  Modal,
  formatDate,
  formatDuration,
  useFetch,
} from '../components/ui'

import { STATUS_TONE, cost, num, secondsBetween } from './campaign/shared'

/** NFR-104: at this share of the budget the pipeline starts shedding work. */
const DEGRADE_AT = 0.8

export default function CampaignsPage() {
  const navigate = useNavigate()

  const { data, error, loading, reload } = useFetch(async () => {
    const [campaigns, directives, versions] = await Promise.all([
      api.get('/campaigns'),
      api.get('/directives/?all_versions=true').catch(() => []),
      api.get('/profile/versions').catch(() => []),
    ])
    return { campaigns: campaigns || [], directives: directives || [], versions: versions || [] }
  })

  // The strip is orientation, not data the screen depends on: never block on it.
  const journey = useFetch(() => api.get('/overview/journey').catch(() => null))

  const [creating, setCreating] = useState(false)
  const [deleting, setDeleting] = useState(null)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState(null)

  const campaigns = data?.campaigns ?? []
  const directives = data?.directives ?? []
  const versions = data?.versions ?? []
  const canCreate = directives.length > 0 && versions.length > 0

  const directiveName = (id) =>
    directives.find((d) => d.id === id)?.name || (id ? 'unavailable' : '–')

  // NFR-104: warn before the budget runs out, not after the campaign degraded.
  const nearBudget = campaigns.filter(
    (c) =>
      ['running', 'paused'].includes(c.status) &&
      c.token_budget > 0 &&
      c.tokens_used / c.token_budget >= DEGRADE_AT,
  )

  async function remove() {
    setBusy(true)
    setActionError(null)
    try {
      await api.del(`/campaigns/${deleting.id}`)
      setDeleting(null)
      reload()
    } catch (e) {
      setActionError(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    // Campaigns are the Plan phase: everything on this screen carries its blue.
    <div className="content-wide phase-2">
      <WorkflowMap journey={journey.data?.journey || {}} compact current="plan" />

      <ScreenIntro pathname="/campaigns" />

      {actionError && <ErrorBox error={actionError} />}

      {nearBudget.length > 0 && (
        <Caution title="A campaign is close to its token budget">
          {nearBudget.map((c) => c.name).join(', ')} {nearBudget.length > 1 ? 'have' : 'has'} used
          more than {Math.round(DEGRADE_AT * 100)}% of the AI budget. From here the pipeline sheds
          optional work first — speculative openings for low-ranked companies — rather than stopping
          mid-run (NFR-104). Raise the budget on the campaign, or let it finish reduced.
        </Caution>
      )}

      {loading && <Loading rows={4} />}
      {error && <ErrorBox error={error} onRetry={reload} />}

      {!loading && !error && campaigns.length === 0 && (
        <FirstRun
          pathname="/campaigns"
          action={
            canCreate ? (
              <button className="btn btn-primary" onClick={() => setCreating(true)}>
                <Icon name="plus" /> Create your first campaign
              </button>
            ) : (
              <Link className="btn btn-primary" to={directives.length ? '/profile' : '/directives'}>
                <Icon name={directives.length ? 'profile' : 'directives'} />{' '}
                {directives.length ? 'Import your profile first' : 'Set your directives first'}
              </Link>
            )
          }
        >
          A campaign needs a directive set (where to look) and a profile version (who is looking).
          Nothing is collected until you have read the plan and pressed launch.
        </FirstRun>
      )}

      {!loading && !error && campaigns.length > 0 && (
        <>
          <div className="row" style={{ marginBottom: 12 }}>
            <h3 style={{ margin: 0, display: 'flex', alignItems: 'center', gap: 8 }}>
              <Icon name="campaign" className="phase-ink" />
              {campaigns.length} campaigns
            </h3>
            <div className="spacer" />
            <button
              className="btn btn-primary"
              onClick={() => setCreating(true)}
              disabled={!canCreate}
              title={canCreate ? undefined : 'A directive set and a profile version are needed'}
            >
              <Icon name="plus" /> New campaign
            </button>
          </div>

          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Campaign</th>
                  <th>Status</th>
                  <th>
                    <Icon name="directives" /> Directive set
                    <HelpTip term="directive" />
                  </th>
                  <th>
                    <Icon name="pipeline" /> Stage
                    <HelpTip title="Stage">
                      Where the campaign has got to: planning, collection, then the analysis stages
                      that read what collection produced. Each stage stores its own output, so any
                      one of them can be re-run on its own.
                    </HelpTip>
                  </th>
                  <th style={{ minWidth: 180 }}>
                    <Icon name="sparkle" /> Tokens used
                    <HelpTip term="token_budget" />
                  </th>
                  <th className="num">
                    <Icon name="money" /> Cost
                    <HelpTip title="Cost" align="right">
                      Everything this campaign has spent: paid API calls to sources plus the AI
                      tokens used to read the pages they returned. It is measured, not estimated.
                    </HelpTip>
                  </th>
                  <th>
                    <Icon name="clock" /> Timing
                  </th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {campaigns.map((c) => {
                  const ratio = c.token_budget ? (c.tokens_used || 0) / c.token_budget : 0
                  const ran = secondsBetween(c.started_at, c.finished_at)
                  return (
                    <tr key={c.id}>
                      <td>
                        <Link to={`/campaigns/${c.id}`} style={{ fontWeight: 600 }}>
                          {c.name}
                        </Link>
                        <div className="tiny muted">created {formatDate(c.created_at)}</div>
                      </td>
                      <td>
                        <Badge tone={STATUS_TONE[c.status]}>{c.status}</Badge>
                      </td>
                      <td className="small">
                        {directiveName(c.directive_set_id)}
                        {/* FR-148: a campaign keeps the directive version it ran under. */}
                      </td>
                      <td className="small muted">{c.stage || 'not started'}</td>
                      <td>
                        <Meter
                          value={c.tokens_used || 0}
                          max={c.token_budget || 1}
                          tone={ratio >= 0.9 ? 'danger' : ratio >= DEGRADE_AT ? 'warn' : undefined}
                        />
                        <div className="tiny muted">
                          {num(c.tokens_used)} / {num(c.token_budget)}
                        </div>
                      </td>
                      <td className="num">{cost(c.cost_eur)}</td>
                      <td className="small">
                        {c.started_at ? (
                          <>
                            <div>{formatDate(c.started_at)}</div>
                            <div className="tiny muted">
                              {c.finished_at ? 'ran for ' : 'running for '}
                              {formatDuration(ran)}
                            </div>
                          </>
                        ) : (
                          <span className="muted">not launched</span>
                        )}
                      </td>
                      <td>
                        <button
                          className="btn btn-sm btn-ghost"
                          onClick={() => setDeleting(c)}
                          disabled={c.status === 'running'}
                          title={
                            c.status === 'running'
                              ? 'Cancel the run before deleting it'
                              : 'Delete this campaign'
                          }
                        >
                          <Icon name="trash" /> Delete
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </>
      )}

      {creating && (
        <CreateCampaign
          directives={directives}
          versions={versions}
          onClose={() => setCreating(false)}
          onCreated={(campaign) => navigate(`/campaigns/${campaign.id}`)}
        />
      )}

      {/* Deleting a campaign discards its plan and its collected links: confirm. */}
      {deleting && (
        <Modal
          title="Delete this campaign?"
          onClose={() => setDeleting(null)}
          actions={
            <>
              <button className="btn" onClick={() => setDeleting(null)}>
                Keep it
              </button>
              <button className="btn btn-danger" onClick={remove} disabled={busy}>
                {busy ? (
                  <span className="spinner" />
                ) : (
                  <>
                    <Icon name="trash" /> Delete
                  </>
                )}
              </button>
            </>
          }
        >
          <p>
            <strong>{deleting.name}</strong> and its source plan are removed. Companies and
            vacancies already written to the shared knowledge base stay — they belong to no single
            campaign — but this campaign&apos;s plan, progress and cost history do not.
          </p>
        </Modal>
      )}
    </div>
  )
}

/**
 * Create from a directive set (FR-161). The profile version is pinned at
 * creation so an old campaign stays explainable after the profile changes
 * (FR-105).
 */
function CreateCampaign({ directives, versions, onClose, onCreated }) {
  const [name, setName] = useState(
    `Search ${new Date().toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })}`,
  )
  const [directiveSetId, setDirectiveSetId] = useState(directives[0]?.id || '')
  const [versionId, setVersionId] = useState(versions[0]?.id || '')
  const [budget, setBudget] = useState('')
  const [maxPages, setMaxPages] = useState('200')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function submit() {
    setBusy(true)
    setError(null)
    try {
      const created = await api.post('/campaigns', {
        name: name.trim() || 'Untitled campaign',
        directive_set_id: directiveSetId,
        profile_version_id: versionId,
        token_budget: budget ? Number(budget) : undefined,
        caps: maxPages ? { max_pages: Number(maxPages) } : undefined,
      })
      onCreated(created)
    } catch (e) {
      setError(e)
      setBusy(false)
    }
  }

  return (
    <Modal
      title="New campaign"
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn btn-primary"
            onClick={submit}
            disabled={busy || !directiveSetId || !versionId}
          >
            {busy ? (
              <span className="spinner" />
            ) : (
              <>
                <Icon name="plus" /> Create and plan
              </>
            )}
          </button>
        </>
      }
    >
      {error && <ErrorBox error={error} />}

      <p className="small muted" style={{ marginTop: 0 }}>
        Creating a campaign collects nothing. The next screen shows the plan — every source, its
        query and its estimated cost — and waits for you.
      </p>

      <Field label="Name">
        <input type="text" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      </Field>

      <Field label="Directive set" hint="Where this campaign is allowed to look.">
        <select value={directiveSetId} onChange={(e) => setDirectiveSetId(e.target.value)}>
          {directives.map((d) => (
            <option key={d.id} value={d.id}>
              {d.name} (v{d.version})
            </option>
          ))}
        </select>
      </Field>

      {/* FR-105: the version is recorded, so a ranking stays explainable later. */}
      <Field label="Profile version" hint="Pinned now; later profile edits do not change this run.">
        <select value={versionId} onChange={(e) => setVersionId(e.target.value)}>
          {versions.map((v) => (
            <option key={v.id} value={v.id}>
              v{v.version} · {v.source_note || 'profile'} · {formatDate(v.created_at)}
            </option>
          ))}
        </select>
      </Field>

      <div className="grid grid-2">
        <Field
          label={
            <>
              Token budget
              <HelpTip term="token_budget" />
            </>
          }
          hint="Leave blank for the configured default."
        >
          <input
            type="number"
            min="0"
            step="10000"
            value={budget}
            placeholder="default"
            onChange={(e) => setBudget(e.target.value)}
          />
        </Field>

        {/* FR-186: hard ceilings are set before anything is fetched. */}
        <Field label="Maximum pages" hint="A ceiling across every source in the plan.">
          <input
            type="number"
            min="1"
            step="10"
            value={maxPages}
            onChange={(e) => setMaxPages(e.target.value)}
          />
        </Field>
      </div>
    </Modal>
  )
}
