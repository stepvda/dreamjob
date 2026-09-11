/**
 * Profile intake and editing (FR-101..109, FR-441, FR-442).
 *
 * Everything the system later says about the job seeker is grounded here, so
 * this screen is deliberately a workspace rather than a form: the retained
 * originals, the merge queue, the structured sections, the normalised skills,
 * the evidence behind them, the personas that slant them, and the fields that
 * must never leave the building.
 *
 * The shell owns only what more than one tab needs — the profile version, the
 * conflict queue and the CR-410 consent — and hands each tab a reload callback
 * so a save on one tab is visible on the others without a page refresh.
 *
 * First-run disclosure: the landing tab is Documents, because the two uploads
 * are the only thing a new seeker can usefully do here. The refinement tabs
 * are one click behind "More profile detail" until a version exists; the
 * conflict queue stays in the primary row and raises a blocker banner whenever
 * something is still contested, because that is the one thing that can make
 * every downstream answer wrong.
 */

import { useState } from 'react'

import { api, ApiError } from '../api/client'
import { FirstRun, ScreenIntro } from '../components/Help'
import Icon from '../components/Icon'
import WorkflowMap from '../components/WorkflowMap'
import ConsentNotice from '../components/ConsentNotice'
import { ErrorBox, Loading, Stat, useFetch } from '../components/ui'
import DreamJobTab from './profile/DreamJobTab'
import ConflictsTab from './profile/ConflictsTab'
import DocumentsTab from './profile/DocumentsTab'
import EvidenceTab from './profile/EvidenceTab'
import PersonasTab from './profile/PersonasTab'
import PrivacyTab from './profile/PrivacyTab'
import ProfileTabs from './profile/ProfileTabs'
import SectionsTab from './profile/SectionsTab'
import SkillsTab from './profile/SkillsTab'

/**
 * A job seeker with no profile yet is the normal first case, not an error:
 * GET /api/profile/ answers 404 until the first document lands (FR-102).
 */
async function loadProfile() {
  try {
    return await api.get('/profile/')
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null
    throw err
  }
}

export default function ProfilePage() {
  const [tab, setTab] = useState('documents')
  // null = "leave it to the default": collapsed on the first run, revealed once
  // a version exists. A click pins the choice so the seeker can collapse again.
  const [detailOpen, setDetailOpen] = useState(null)

  const journey = useFetch(() => api.get('/overview/journey'), [])
  const profile = useFetch(loadProfile, [])
  const conflicts = useFetch(() => api.get('/profile/conflicts'), [])
  const consent = useFetch(() => api.get('/auth/consent'), [])

  const unresolved = (conflicts.data || []).filter(
    (c) => !c.resolution || c.resolution === 'unresolved',
  ).length

  const hasProfile = Boolean(profile.data)
  const showDetail = detailOpen ?? hasProfile

  /** A new profile version arrived — from an upload, a save, or a restore. */
  function adopt(version) {
    if (version) profile.setData(version)
    conflicts.reload()
    journey.reload()
  }

  const llm = consent.data?.llm_transfer

  /* The icon is the tab's second cue: colour says which phase you are in, the
     glyph says which part of the profile this is. */
  const primaryTabs = [
    {
      key: 'documents',
      label: (
        <>
          <Icon name="document" /> Documents
        </>
      ),
    },
    {
      key: 'conflicts',
      label: (
        <>
          <Icon name="warning" /> Conflicts
        </>
      ),
      count: unresolved || undefined,
    },
    {
      key: 'dream_job',
      label: (
        <>
          <Icon name="dream" /> Dream job
        </>
      ),
    },
  ]

  /* The refinement tabs. They only make sense once there is a profile to
     refine, so they are disclosed a click away rather than shown up front. */
  const detailTabs = [
    {
      key: 'sections',
      label: (
        <>
          <Icon name="edit" /> Sections
        </>
      ),
    },
    {
      key: 'skills',
      label: (
        <>
          <Icon name="sparkle" /> Skills
        </>
      ),
    },
    {
      key: 'evidence',
      label: (
        <>
          <Icon name="link" /> Evidence
        </>
      ),
    },
    {
      key: 'personas',
      label: (
        <>
          <Icon name="contacts" /> Personas
        </>
      ),
    },
    {
      key: 'privacy',
      label: (
        <>
          <Icon name="lock" /> Privacy
        </>
      ),
      count: profile.data?.do_not_disclose?.length || undefined,
    },
  ]

  return (
    <div className="col" style={{ gap: 16 }}>
      <WorkflowMap compact current="profile" journey={journey.data?.journey || {}} />

      <ScreenIntro pathname="/profile" />

      {/* CR-410: profile text reaches a model outside the EU only after the
          job seeker has said so. The wording, the POST and the once-only
          behaviour live in one shared component; the consent fetch here keeps
          the rest of the screen able to see the decision (NFR-104). */}
      {llm && <ConsentNotice consent={llm} onGranted={consent.reload} />}

      {/* FR-103: an unresolved conflict is a blocker. It is surfaced here, above
          the tab body, so it is visible whatever tab the seeker is on, and the
          Conflicts tab it points to is always in the primary row. */}
      {unresolved > 0 && (
        <div className="alert alert-danger" style={{ alignItems: 'center' }}>
          <Icon name="warning" />
          <div style={{ flex: 1 }}>
            <strong>
              {unresolved} unresolved conflict{unresolved === 1 ? '' : 's'} still block your
              profile.
            </strong>{' '}
            Your LinkedIn export and CV disagree; until you decide, the system keeps a value it
            knows is contested — and a tailored CV can repeat it to an employer.
          </div>
          <button className="btn btn-sm" onClick={() => setTab('conflicts')}>
            Resolve now
          </button>
        </div>
      )}

      {profile.loading && <Loading rows={4} />}
      {profile.error && <ErrorBox error={profile.error} onRetry={profile.reload} />}

      {!profile.loading && !profile.error && !profile.data && (
        <FirstRun
          pathname="/profile"
          action={
            <button className="btn btn-primary" onClick={() => setTab('documents')}>
              <Icon name="upload" />
              Start with your LinkedIn export
            </button>
          }
        />
      )}

      {!profile.loading && !profile.error && (
        <>
          {/* The three numbers that decide whether the profile can be trusted
              downstream: which version is current, what is still contested, and
              what was read with a shaky hand. */}
          {profile.data && (
            <div className="grid grid-3 phase-1">
              <Stat
                icon="document"
                value={profile.data.version}
                label={`version · saved from ${profile.data.source_note || 'manual entry'}`}
                phase="phase-1"
              />
              <Stat
                icon="warning"
                value={unresolved}
                label={`unresolved conflict${unresolved === 1 ? '' : 's'}`}
                phase="phase-1"
              />
              <Stat
                icon="help"
                value={profile.data.low_confidence_fields?.length || 0}
                label="fields read with low confidence"
                phase="phase-1"
              />
            </div>
          )}

          <div>
            <ProfileTabs
              primary={primaryTabs}
              detail={detailTabs}
              active={tab}
              onChange={setTab}
              open={showDetail}
              onToggle={() => setDetailOpen(!showDetail)}
            />

            {tab === 'documents' && (
              <DocumentsTab profile={profile.data} onNewVersion={adopt} />
            )}
            {tab === 'sections' && (
              <SectionsTab profile={profile.data} onNewVersion={adopt} />
            )}
            {tab === 'conflicts' && (
              <ConflictsTab
                conflicts={conflicts.data}
                loading={conflicts.loading}
                error={conflicts.error}
                reload={conflicts.reload}
                hasProfile={Boolean(profile.data)}
                onNewVersion={adopt}
              />
            )}
            {/* Single dream-job entry: this tab summarises what is saved and
                links to the one editor on /dream-job rather than embedding a
                second copy of it. */}
            {tab === 'dream_job' && <DreamJobTab />}
            {tab === 'skills' && <SkillsTab profile={profile.data} />}
            {tab === 'evidence' && <EvidenceTab profile={profile.data} />}
            {tab === 'personas' && <PersonasTab />}
            {tab === 'privacy' && (
              <PrivacyTab profile={profile.data} onFlagsChanged={profile.reload} />
            )}
          </div>
        </>
      )}
    </div>
  )
}
