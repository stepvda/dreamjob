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
 */

import { useState } from 'react'

import { api, ApiError } from '../api/client'
import { Caution, FirstRun, ScreenIntro } from '../components/Help'
import Icon from '../components/Icon'
import WorkflowMap from '../components/WorkflowMap'
import { ErrorBox, Loading, Stat, Tabs, useFetch } from '../components/ui'
import DreamJobPage from './DreamJobPage'
import ConflictsTab from './profile/ConflictsTab'
import DocumentsTab from './profile/DocumentsTab'
import EvidenceTab from './profile/EvidenceTab'
import PersonasTab from './profile/PersonasTab'
import PrivacyTab from './profile/PrivacyTab'
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

  const journey = useFetch(() => api.get('/overview/journey'), [])
  const profile = useFetch(loadProfile, [])
  const conflicts = useFetch(() => api.get('/profile/conflicts'), [])
  const consent = useFetch(() => api.get('/auth/consent'), [])

  const unresolved = (conflicts.data || []).filter(
    (c) => !c.resolution || c.resolution === 'unresolved',
  ).length

  /** A new profile version arrived — from an upload, a save, or a restore. */
  function adopt(version) {
    if (version) profile.setData(version)
    conflicts.reload()
    journey.reload()
  }

  const llm = consent.data?.llm_transfer

  async function grantTransfer() {
    await api.post('/auth/consent', { kind: 'llm_transfer', granted: true })
    consent.reload()
  }

  /* The icon is the tab's second cue: colour says which phase you are in, the
     glyph says which part of the profile this is. */
  const tabs = [
    {
      key: 'documents',
      label: (
        <>
          <Icon name="document" /> Documents
        </>
      ),
    },
    {
      key: 'sections',
      label: (
        <>
          <Icon name="edit" /> Sections
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
          job seeker has said so. Without the consent, extraction stays
          deterministic rather than failing (NFR-104). */}
      {llm && (
        <Caution
          title="Your profile leaves the EU only if you allow it"
          acknowledge="I consent to this transfer"
          onAcknowledge={grantTransfer}
          acknowledged={Boolean(llm.granted)}
        >
          {llm.text} Until you consent, documents are read by the deterministic parser alone —
          which works, but reads an unusual layout less well.
        </Caution>
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
            <Tabs tabs={tabs} active={tab} onChange={setTab} />

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
            {/* The same editor Advanced offers. The dream-job statement is the
                one field the search cannot proceed without, so it belongs on
                the screen a new job seeker is already working in rather than
                only behind a separate nav item. */}
            {tab === 'dream_job' && <DreamJobPage />}
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
