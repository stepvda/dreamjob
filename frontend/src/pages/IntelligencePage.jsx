/**
 * Dream-job intelligence (FR-381..384, FR-443, FR-444).
 *
 * Every part of this screen answers one question from a different angle: *the
 * market is not offering my dream job — now what?*
 *
 *   1. The gaps      (FR-381) what stands in the way, what closes it, what it
 *                             costs, and the opportunities it cost points on.
 *   2. The routes    (FR-382) when nothing clears the dream-job threshold, two
 *                             or three sequences of roles that lead there.
 *   3. The spread             how much of what the market offers clears that
 *                             threshold at all — the condition behind (2).
 *   4. The values    (FR-384) companies whose own material contradicts what
 *                             you said you wanted.
 *   5. The profile   (FR-443) what to change on LinkedIn — as text you copy,
 *                             because nothing here ever touches LinkedIn.
 *
 * Two shape differences between the stored reads and the compute writes run
 * through the whole file and are normalised here rather than in five places:
 * GET returns the stored row (`analysis`, `paths`, the advice row), POST
 * returns the freshly computed report, which additionally carries `market`,
 * `counts` and `warnings`. The stored read is the source of truth for what is
 * on screen; the compute response is kept only for its warnings.
 *
 * NFR-305 governs all of it: nothing here acts on its own. Tagging the ranked
 * list is an explicit request (FR-284), and the LinkedIn advice is a draft on
 * your own account until you copy it somewhere yourself.
 */

import { useCallback, useMemo, useState } from 'react'

import { api } from '../api/client'
import { ScreenIntro } from '../components/Help'
import WorkflowMap from '../components/WorkflowMap'
import { ErrorBox, Loading, Tabs, useFetch } from '../components/ui'
import FitDistribution from './intelligence/FitDistribution'
import GapAnalysis from './intelligence/GapAnalysis'
import LinkedInAdvice from './intelligence/LinkedInAdvice'
import SteppingStones from './intelligence/SteppingStones'
import ValuesConflicts from './intelligence/ValuesConflicts'
import { AdvisoryNotice, IntelligenceFirstRun, TagConfirm } from './intelligence/panels'

/** One request per company, so the comparison is bounded rather than exhaustive. */
const MAX_COMPANIES_CHECKED = 12

/** The ranked list is capped at 200 by the API; the distribution says so when it bites. */
const OPPORTUNITY_PAGE = 200

export default function IntelligencePage() {
  const [tab, setTab] = useState('gaps')
  const [actionError, setActionError] = useState(null)

  const overview = useFetch(useCallback(() => api.get('/intelligence'), []), [])
  const journey = useFetch(useCallback(() => api.get('/overview/journey'), []), [])

  /* Every route in this module defaults to the latest campaign when no id is
     given, but the ranked list does not — so the campaign is resolved once,
     here, and passed to everything. */
  const ready = !overview.loading && !overview.error
  const campaignId = overview.data?.campaign_id || null
  const qs = campaignId ? `?campaign_id=${encodeURIComponent(campaignId)}` : ''

  const gaps = useFetch(
    useCallback(
      () => (ready ? api.get(`/intelligence/gap-analysis${qs}`) : Promise.resolve(null)),
      [ready, qs],
    ),
    [ready, qs],
  )
  const stones = useFetch(
    useCallback(
      () => (ready ? api.get(`/intelligence/stepping-stones${qs}`) : Promise.resolve(null)),
      [ready, qs],
    ),
    [ready, qs],
  )
  const linkedin = useFetch(
    useCallback(
      () => (ready ? api.get(`/intelligence/linkedin${qs}`) : Promise.resolve(null)),
      [ready, qs],
    ),
    [ready, qs],
  )
  /* Sorted by dream fit rather than by overall score: with a cap of 200 that
     keeps every opportunity clearing the threshold inside the page, so the
     count above the line is exact even when the tail is cut. */
  const opportunities = useFetch(
    useCallback(
      () =>
        ready
          ? api.get(
              `/opportunities?limit=${OPPORTUNITY_PAGE}&sort=dream_fit&respect_manual_order=false${
                campaignId ? `&campaign_id=${encodeURIComponent(campaignId)}` : ''
              }`,
            )
          : Promise.resolve(null),
      [ready, campaignId],
    ),
    [ready, campaignId],
  )

  /* --- FR-381 ------------------------------------------------------------ */
  const [gapRun, setGapRun] = useState(null)
  const [gapRunning, setGapRunning] = useState(false)

  async function runGapAnalysis() {
    setActionError(null)
    setGapRunning(true)
    try {
      setGapRun(await api.post('/intelligence/gap-analysis', { campaign_id: campaignId, use_llm: true }))
      gaps.reload()
    } catch (err) {
      setActionError(err)
    } finally {
      setGapRunning(false)
    }
  }

  /* --- FR-382 ------------------------------------------------------------ */
  const [stoneRun, setStoneRun] = useState(null)
  const [stoneRunning, setStoneRunning] = useState(false)
  const [savingThreshold, setSavingThreshold] = useState(false)
  /* null means "untouched": the field then shows the stored threshold. Once the
     seeker types, '' has to stay '' rather than snapping back. */
  const [thresholdDraft, setThresholdDraft] = useState(null)
  const [tagResult, setTagResult] = useState(null)
  const [confirmTag, setConfirmTag] = useState(false)

  const threshold = stones.data?.threshold
  const thresholdValue = thresholdDraft === null ? (threshold == null ? '' : String(threshold)) : thresholdDraft

  async function runSteppingStones(force) {
    setActionError(null)
    setStoneRunning(true)
    try {
      setStoneRun(
        await api.post('/intelligence/stepping-stones', {
          campaign_id: campaignId,
          use_llm: true,
          force,
        }),
      )
      stones.reload()
    } catch (err) {
      setActionError(err)
    } finally {
      setStoneRunning(false)
    }
  }

  async function saveThreshold(value) {
    setActionError(null)
    setSavingThreshold(true)
    try {
      await api.put('/intelligence/stepping-stones/threshold', { threshold: value })
      setThresholdDraft(null)
      stones.reload()
    } catch (err) {
      setActionError(err)
    } finally {
      setSavingThreshold(false)
    }
  }

  /* FR-284: the tags are the job seeker's, so this writes only on request and
     only after the modal says what it is about to write. */
  async function applyTags() {
    setActionError(null)
    try {
      setTagResult(await api.post('/intelligence/stepping-stones/tags', { campaign_id: campaignId }))
      opportunities.reload()
    } catch (err) {
      setActionError(err)
    } finally {
      setConfirmTag(false)
    }
  }

  /* --- FR-384 ------------------------------------------------------------ */
  const [values, setValues] = useState(null)
  const [valuesLoading, setValuesLoading] = useState(false)
  const [valuesError, setValuesError] = useState(null)
  const [valuesExcluded, setValuesExcluded] = useState(0)

  const companies = useMemo(() => {
    const seen = new Map()
    for (const opp of opportunities.data?.items || []) {
      if (opp.company_id && !seen.has(opp.company_id)) {
        seen.set(opp.company_id, opp.company_name || opp.company_id)
      }
    }
    return [...seen.entries()].slice(0, MAX_COMPANIES_CHECKED).map(([id, name]) => ({ id, name }))
  }, [opportunities.data])

  async function loadValues() {
    setValuesLoading(true)
    setValuesError(null)
    try {
      const settled = await Promise.all(
        companies.map((c) =>
          api.get(`/intelligence/values-match/${c.id}${qs}`).catch(() => null),
        ),
      )
      const found = settled.filter(Boolean)
      /* FR-385: a company discretion mode excludes must not be surfaced. */
      setValuesExcluded(found.filter((m) => m.excluded_reason).length)
      setValues(found.filter((m) => !m.excluded_reason))
    } catch (err) {
      setValuesError(err)
    } finally {
      setValuesLoading(false)
    }
  }

  /* --- FR-443 ------------------------------------------------------------ */
  const [generating, setGenerating] = useState(false)
  const [savingEdit, setSavingEdit] = useState(false)
  const [savedAt, setSavedAt] = useState(null)
  const [language, setLanguage] = useState('en')

  async function generateAdvice() {
    setActionError(null)
    setGenerating(true)
    try {
      await api.post('/intelligence/linkedin', {
        campaign_id: campaignId,
        use_llm: true,
        language,
      })
      linkedin.reload()
    } catch (err) {
      setActionError(err)
    } finally {
      setGenerating(false)
    }
  }

  async function saveAdviceEdit(text) {
    const id = linkedin.data?.advice?.id
    if (!id) return
    setActionError(null)
    setSavingEdit(true)
    try {
      const updated = await api.patch(`/intelligence/linkedin/${id}`, { text })
      setSavedAt(updated?.edited_at || new Date().toISOString())
    } catch (err) {
      setActionError(err)
    } finally {
      setSavingEdit(false)
    }
  }

  /* --- Derived ----------------------------------------------------------- */

  /** Steps cite gaps by id; the labels only exist on the analysis. */
  const gapLabels = useMemo(() => {
    const out = {}
    for (const gap of gaps.data?.analysis?.gaps || []) out[gap.id] = gap.label
    return out
  }, [gaps.data])

  const discretionMode = Boolean(
    overview.data?.discretion_mode ?? linkedin.data?.discretion_mode ?? gaps.data?.discretion_mode,
  )

  const analysis = gaps.data?.analysis || null
  const paths = stones.data?.paths || []
  const advice = linkedin.data?.advice || null
  const conflicts = (values || []).filter((m) => (m.warnings || []).length > 0).length

  const loading = overview.loading || gaps.loading || stones.loading || linkedin.loading
  const nothingComputed = !analysis && paths.length === 0 && !advice

  const TABS = [
    { key: 'gaps', label: 'Gaps', count: analysis?.gaps?.length },
    { key: 'paths', label: 'Stepping stones', count: paths.length || undefined },
    { key: 'spread', label: 'Fit across the market' },
    { key: 'values', label: 'Values conflicts', count: values ? conflicts : undefined },
    { key: 'linkedin', label: 'LinkedIn profile' },
  ]

  return (
    <div className="content-wide">
      <WorkflowMap journey={journey.data?.journey || {}} compact current="scoring" />
      <ScreenIntro pathname="/intelligence" />

      <AdvisoryNotice discretionMode={discretionMode} />

      {overview.error && <ErrorBox error={overview.error} onRetry={overview.reload} />}
      {actionError && <ErrorBox error={actionError} onRetry={() => setActionError(null)} />}

      {loading && !overview.error && <Loading rows={4} />}

      {!loading && !overview.error && nothingComputed && (
        <IntelligenceFirstRun
          hasCampaign={Boolean(campaignId)}
          running={gapRunning}
          onRun={() => {
            setTab('gaps')
            runGapAnalysis()
          }}
        />
      )}

      {!loading && !overview.error && !nothingComputed && (
        <>
          <Tabs tabs={TABS} active={tab} onChange={setTab} />

          {tab === 'gaps' && (
            <>
              {gaps.error && <ErrorBox error={gaps.error} onRetry={gaps.reload} />}
              <GapAnalysis
                analysis={analysis}
                runResult={gapRun}
                onRun={runGapAnalysis}
                running={gapRunning}
                hasCampaign={Boolean(campaignId)}
              />
            </>
          )}

          {tab === 'paths' && (
            <>
              {stones.error && <ErrorBox error={stones.error} onRetry={stones.reload} />}
              <SteppingStones
                stones={stones.data}
                runResult={stoneRun}
                gapLabels={gapLabels}
                onRun={runSteppingStones}
                onSaveThreshold={saveThreshold}
                onTag={() => setConfirmTag(true)}
                running={stoneRunning}
                savingThreshold={savingThreshold}
                tagResult={tagResult}
                thresholdDraft={thresholdValue}
                setThresholdDraft={setThresholdDraft}
                hasCampaign={Boolean(campaignId)}
              />
            </>
          )}

          {tab === 'spread' && (
            <>
              {opportunities.error && (
                <ErrorBox error={opportunities.error} onRetry={opportunities.reload} />
              )}
              {opportunities.loading ? (
                <Loading rows={3} />
              ) : (
                <FitDistribution
                  items={opportunities.data?.items || []}
                  total={opportunities.data?.total}
                  threshold={stones.data?.threshold}
                  scored={stones.data?.scored}
                  unscored={stones.data?.unscored}
                  cleared={(stones.data?.destinations || []).length}
                />
              )}
            </>
          )}

          {tab === 'values' && (
            <ValuesConflicts
              matches={values}
              loading={valuesLoading}
              error={valuesError}
              onLoad={loadValues}
              checkedCount={(values || []).length + valuesExcluded}
              companyCount={companies.length}
              excludedCount={valuesExcluded}
              hasCampaign={Boolean(campaignId)}
            />
          )}

          {tab === 'linkedin' && (
            <>
              {linkedin.error && <ErrorBox error={linkedin.error} onRetry={linkedin.reload} />}
              <LinkedInAdvice
                advice={advice}
                note={linkedin.data?.note}
                discretionMode={discretionMode}
                onGenerate={generateAdvice}
                onSaveEdit={saveAdviceEdit}
                generating={generating}
                saving={savingEdit}
                savedAt={savedAt}
                language={language}
                setLanguage={setLanguage}
                onOpenGaps={() => setTab('gaps')}
                hasCampaign={Boolean(campaignId)}
              />
            </>
          )}
        </>
      )}

      {confirmTag && <TagConfirm onCancel={() => setConfirmTag(false)} onConfirm={applyTags} />}
    </div>
  )
}
