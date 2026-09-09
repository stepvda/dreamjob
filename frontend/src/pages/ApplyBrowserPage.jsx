/**
 * The Apply Browser (FR-321..FR-325, FR-263, FR-284, NFR-206, NFR-502, RK-05).
 *
 * Master-detail, because the two questions asked here are different questions.
 * "Which of my five hundred selected jobs still needs something?" is a list,
 * and it has to page and stay fast. "Is this one right, and what exactly would
 * go out?" is five documents, and it needs the whole width.
 *
 * The screen is arranged around one fact that the product owner was explicit
 * about: the pipeline runs all the way to a finished email with the tailored
 * CV attached, and stops there. Three things follow from that, and they are
 * the three decisions worth knowing about in this file.
 *
 * 1. The guard is read from the server before either send control is pressed,
 *    and rendered as a calm banner rather than a warning. It is not this
 *    screen's opinion: `/api/apply/send-status` reports the state of a guard
 *    that lives in the mail layer and refuses at the transport, so no route
 *    through this interface — or around it — can send while it is on.
 *
 * 2. Neither send button is ever disabled. A disabled button teaches nothing;
 *    one that runs the whole path and comes back with "assembled, not sent,
 *    here is who it was addressed to and here is the file on disk" teaches
 *    exactly where you are and what would still be in the way.
 *
 * 3. Approval and dispatch stay two separate acts (FR-324), and the bulk path
 *    goes through a table of exactly who receives what before it will run.
 */

import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { FirstRun, HelpTip, ScreenIntro } from '../components/Help'
import Icon from '../components/Icon'
import WorkflowMap from '../components/WorkflowMap'
import { ErrorBox, JobProgress, Loading, useFetch } from '../components/ui'

import JobDetail from './apply/JobDetail'
import JobList from './apply/JobList'
import SendAllModal from './apply/SendAllModal'
import SendGuardBanner from './apply/SendGuardBanner'
import applyApi from './apply/api'
import { isSendable, photoBlocked } from './apply/shared'

const PAGE_SIZE = 50
/** The backend budgets 45 seconds per package when it estimates a batch. */
const SECONDS_PER_PACKAGE = 45
const POLL_MS = 5000

export default function ApplyBrowserPage() {
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState('')
  // The server's own filter keys, ANDed. Held as an array because that is
  // exactly what the endpoint takes, so nothing has to be translated.
  const [filters, setFilters] = useState([])
  const [offset, setOffset] = useState(0)
  const [selectedId, setSelectedId] = useState(null)
  // Chosen rows are held whole, keyed by id, rather than as a list of ids: the
  // confirmation modal has to name the recipient and the subject of every one
  // of them, and a row chosen on page one is no longer in `rows` by page three.
  const [chosenRows, setChosenRows] = useState({})
  const [busy, setBusy] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [sendResult, setSendResult] = useState(null)
  const [sendError, setSendError] = useState(null)
  const [plan, setPlan] = useState(null)
  const [batch, setBatch] = useState(null)
  const [batchError, setBatchError] = useState(null)
  const [gen, setGen] = useState(null)

  // Typing in a 1,600-row corpus should not issue a request per keystroke.
  useEffect(() => {
    const timer = setTimeout(() => {
      setSearch(query)
      setOffset(0)
    }, 300)
    return () => clearTimeout(timer)
  }, [query])

  const guard = useFetch(() => applyApi.sendStatus(), [])

  const filterKey = filters.join(',')
  const list = useFetch(
    () =>
      applyApi.browse({
        q: search || undefined,
        filters,
        limit: PAGE_SIZE,
        offset,
      }),
    [search, filterKey, offset],
  )

  // Reference data the screen needs but must never block on.
  const statics = useFetch(async () => {
    const [templates, profile] = await Promise.all([
      applyApi.templates().catch(() => null),
      applyApi.profile().catch(() => null),
    ])
    return { templates, profile }
  }, [])

  // Orientation, not data the screen depends on: never block on it.
  const journey = useFetch(() => api.get('/overview/journey').catch(() => null), [])

  const rows = list.data?.rows || []
  const total = list.data?.total ?? 0
  const facets = list.data?.facets || {}

  // Landing on the screen should show something rather than an invitation to
  // click. The first row of the page is as good a default as any.
  useEffect(() => {
    if (!selectedId && rows.length > 0) setSelectedId(rows[0].opportunity_id)
  }, [rows, selectedId])

  const row = rows.find((r) => r.opportunity_id === selectedId) || null

  const detail = useFetch(async () => {
    if (!selectedId) return null
    try {
      return await applyApi.detail(selectedId)
    } catch (error) {
      // 404 is the honest answer for "nothing generated yet", and the detail
      // pane has its own empty state for it.
      if (error.status === 404) return null
      throw error
    }
  }, [selectedId])

  /* --- Generation, watched by its output (NFR-502) ------------------------ */

  const generatedSoFar = gen
    ? rows.filter((r) => gen.ids.includes(r.opportunity_id) && r.package_id).length
    : 0

  useEffect(() => {
    if (!gen) return undefined
    const deadline =
      gen.startedAt + Math.max(90, gen.ids.length * SECONDS_PER_PACKAGE * 1.5) * 1000
    const timer = setInterval(() => {
      if (Date.now() > deadline) setGen(null)
      else {
        list.reload()
        if (selectedId && gen.ids.includes(selectedId)) detail.reload()
      }
    }, POLL_MS)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gen?.startedAt, gen?.ids.length, selectedId])

  useEffect(() => {
    if (gen && generatedSoFar >= gen.expected) setGen(null)
  }, [gen, generatedSoFar])

  /* --- Photograph state (FR-106) ------------------------------------------ */

  const photo = useMemo(() => {
    const profile = statics.data?.profile
    if (!profile) return { known: false, included: false, reason: '' }
    if (photoBlocked(profile.do_not_disclose))
      return {
        known: true,
        included: false,
        reason:
          'You marked the photograph “do not disclose”, so it is left out of every generated document.',
      }
    if (!profile.photo_path)
      return {
        known: true,
        included: false,
        reason: 'Your profile has no photograph. Upload a CV that contains one to add it.',
      }
    return {
      known: true,
      included: true,
      reason: 'Taken from your profile. Both templates place it beside the name block.',
    }
  }, [statics.data?.profile])

  /* --- Actions ------------------------------------------------------------ */

  async function run(key, fn) {
    setBusy(key)
    setActionError(null)
    try {
      await fn()
    } catch (error) {
      setActionError(error)
    } finally {
      setBusy(null)
    }
  }

  function startGeneration(ids) {
    return run('gen', async () => {
      const res = await applyApi.generate({ opportunity_ids: ids, selected_only: false })
      setGen({ ids, expected: res.count ?? ids.length, startedAt: Date.now() })
    })
  }

  function reloadBoth() {
    list.reload()
    detail.reload()
    guard.reload()
  }

  const packageId = detail.data?.package?.id

  const actions = {
    onGenerate: () => startGeneration([selectedId]),
    onSave: (body) =>
      run('save', async () => {
        await applyApi.editEmail(selectedId, body)
        reloadBoth()
      }),
    onRegenerate: (body) =>
      run('regen', async () => {
        await applyApi.regenerate(selectedId, body)
        reloadBoth()
      }),
    onTemplate: (template) =>
      run('tpl', async () => {
        await applyApi.retemplate(packageId, template)
        reloadBoth()
      }),
    onRecheck: () =>
      run('check', async () => {
        await applyApi.recheck(packageId)
        reloadBoth()
      }),
    onApprove: () =>
      run('approve', async () => {
        await applyApi.approve(packageId, {})
        reloadBoth()
      }),
    onSend: () =>
      run('send', async () => {
        setSendResult(null)
        setSendError(null)
        try {
          setSendResult(await applyApi.sendOne(selectedId))
        } catch (error) {
          // A refusal is the answer, not a failure of the screen.
          setSendError(error)
        }
        reloadBoth()
      }),
    onDismissSend: () => {
      setSendResult(null)
      setSendError(null)
    },
    onReload: reloadBoth,
  }

  /* --- Bulk (FR-324) ------------------------------------------------------ */

  const checked = Object.keys(chosenRows)
  const chosen = Object.values(chosenRows)
  const chosenSendable = chosen.filter(isSendable)

  function toggleChosen(row) {
    setChosenRows((current) => {
      const next = { ...current }
      if (next[row.opportunity_id]) delete next[row.opportunity_id]
      else next[row.opportunity_id] = row
      return next
    })
  }

  function openPlan() {
    setBatch(null)
    setBatchError(null)
    setPlan({
      rows: chosenSendable,
      excluded: chosen.filter((r) => !isSendable(r)),
    })
  }

  async function confirmPlan() {
    setBusy('sendall')
    setBatchError(null)
    try {
      const result = await applyApi.sendAll({
        package_ids: plan.rows.map((r) => r.package_id),
        limit: Math.max(1, plan.rows.length),
      })
      setBatch(result)
      setChosenRows({})
      reloadBoth()
    } catch (error) {
      setBatchError(error)
    } finally {
      setBusy(null)
    }
  }

  /* --- Render -------------------------------------------------------------- */

  const untouched = !list.loading && total === 0 && !search && filters.length === 0

  return (
    <div className="content-wide">
      <WorkflowMap journey={journey.data?.journey || {}} compact current="dispatch" />

      <ScreenIntro pathname="/apply" />

      <SendGuardBanner
        status={guard.data}
        loading={guard.loading}
        error={guard.error}
        onRetry={guard.reload}
      />

      {actionError && <ErrorBox error={actionError} onRetry={() => setActionError(null)} />}
      {list.error && <ErrorBox error={list.error} onRetry={list.reload} />}

      {gen && (
        <div style={{ marginBottom: 14 }}>
          <JobProgress
            job={{
              kind: 'Generating application packages',
              status: 'running',
              progress_done: generatedSoFar,
              progress_total: gen.expected,
              estimated_seconds: Math.max(
                0,
                (gen.expected - generatedSoFar) * SECONDS_PER_PACKAGE,
              ),
            }}
          />
          <p className="small muted" style={{ margin: '6px 0 0' }}>
            Each package is four documents, so this takes a while. It continues on the server if
            you leave the screen; rows fill in as they finish, and only the ones on this page are
            counted here.
          </p>
        </div>
      )}

      {untouched ? (
        <FirstRun
          pathname="/apply"
          title="Nothing is selected to apply for yet"
          action={
            <Link className="btn btn-primary" to="/opportunities">
              Go to the ranked opportunities
            </Link>
          }
        >
          This screen works on the jobs you selected in the ranked list. Select a few there and
          they appear here, ready to have their four documents generated.
        </FirstRun>
      ) : (
        <>
          <div className="row row-wrap" style={{ marginBottom: 12, gap: 8 }}>
            <button
              className="btn"
              disabled={busy === 'gen' || rows.every((r) => r.package_id)}
              onClick={() =>
                startGeneration(
                  rows.filter((r) => !r.package_id).map((r) => r.opportunity_id),
                )
              }
              title="Generate the four documents for every job on this page that has none"
            >
              <Icon name="sparkle" /> Generate what is missing on this page
            </button>

            <button
              className="btn"
              disabled={busy === 'contacts'}
              onClick={() =>
                run('contacts', async () => {
                  await applyApi.discoverContacts({
                    opportunity_ids: rows
                      .filter((r) => !r.contact_email)
                      .map((r) => r.opportunity_id),
                    selected_only: false,
                  })
                })
              }
              title="Find who to write to at the companies on this page"
            >
              <Icon name="search" /> Find contacts for this page
            </button>

            <div className="spacer" />

            <button
              className="btn btn-phase"
              disabled={chosen.length === 0}
              onClick={openPlan}
              title={
                chosen.length === 0
                  ? 'Choose jobs on the left first'
                  : 'Show exactly who would receive what'
              }
            >
              <Icon name="send" /> Send all with attachment
              {chosen.length > 0 ? ` (${chosen.length})` : ''}
            </button>
            <HelpTip term="bulk_approval_summary" />
          </div>

          <div className="apl-layout">
            {list.loading && rows.length === 0 ? (
              <div className="card apl-side">
                <Loading rows={8} />
              </div>
            ) : (
              <JobList
                rows={rows}
                total={total}
                facets={facets}
                loading={list.loading}
                query={query}
                onQuery={setQuery}
                filters={filters}
                onFilters={(next) => {
                  setFilters(next)
                  setOffset(0)
                }}
                selectedId={selectedId}
                onSelect={(id) => {
                  setSelectedId(id)
                  setSendResult(null)
                  setSendError(null)
                }}
                checked={checked}
                onToggle={toggleChosen}
                onCheckAll={(sendable) =>
                  setChosenRows((current) => ({
                    ...current,
                    ...Object.fromEntries(sendable.map((r) => [r.opportunity_id, r])),
                  }))
                }
                onClearChecked={() => setChosenRows({})}
                limit={PAGE_SIZE}
                offset={offset}
                onPage={setOffset}
              />
            )}

            <JobDetail
              row={row}
              detail={detail.data}
              loading={detail.loading}
              error={detail.error}
              templates={statics.data?.templates}
              photo={photo}
              busy={busy}
              sendResult={sendResult}
              sendError={sendError}
              {...actions}
            />
          </div>
        </>
      )}

      {plan && (
        <SendAllModal
          rows={plan.rows}
          excluded={plan.excluded}
          guard={guard.data}
          busy={busy === 'sendall'}
          result={batch}
          error={batchError}
          onClose={() => {
            setPlan(null)
            setBatch(null)
            setBatchError(null)
          }}
          onConfirm={confirmPlan}
        />
      )}
    </div>
  )
}
