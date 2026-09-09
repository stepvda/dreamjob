/**
 * Administration (FR-361, FR-362, FR-363, FR-364, IR-101, NFR-701, NFR-702).
 *
 * Six surfaces, in the order an operator needs them: what the AI is
 * configured to do and what that costs, which sources may be used and on what
 * terms, what has actually run, the immutable record of who did what, the
 * server's own log files, and the data the installation is holding about
 * people.
 *
 * Each tab owns its own fetching. That is not laziness: the six draw on
 * unrelated endpoints, the audit trail and the call log are both paginated,
 * and hoisting the loading state would make every tab wait for the slowest.
 * Switching tabs unmounts the previous one, so nothing polls in the
 * background either.
 *
 * The whole screen is administrator-only — every /api/admin route depends on
 * `current_admin` — so a job seeker who reaches this URL is told why rather
 * than being shown six tabs of 403s.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { ScreenIntro } from '../components/Help'
import WorkflowMap from '../components/WorkflowMap'
import { Empty, Tabs, useFetch } from '../components/ui'
import { useSession } from '../session'
import ActivityTab from './admin/ActivityTab'
import { EmployerCoveragePanel } from './employers'
import AuditTab from './admin/AuditTab'
import DataTab from './admin/DataTab'
import LogsTab from './admin/LogsTab'
import ModelsTab from './admin/ModelsTab'
import SourcesTab from './admin/SourcesTab'

const TABS = [
  { key: 'models', label: 'Models' },
  { key: 'sources', label: 'Sources' },
  { key: 'activity', label: 'Activity' },
  { key: 'audit', label: 'Audit' },
  { key: 'logs', label: 'Logs' },
  { key: 'data', label: 'Data' },
  { key: 'employers', label: 'Employer kind' },
]

export default function AdminPage() {
  const { session } = useSession()
  const [tab, setTab] = useState('models')
  /* The same strip as every working screen, but with no stage marked current:
     administration sits beside the journey rather than at a point in it. */
  const journey = useFetch(() => api.get('/overview/journey'), [])

  if (!session.is_admin) {
    return (
      <div className="content-narrow">
        <ScreenIntro pathname="/admin" />
        <Empty
          title="Administrator role required"
          action={
            <Link className="btn btn-primary" to="/overview">
              Back to where I am
            </Link>
          }
        >
          Model configuration, the source catalogue, the audit trail and the AI call log
          are visible to administrators only — the call log can contain another job
          seeker&rsquo;s profile text. Your own data is yours regardless: export or erase
          it from your account, and read the consent decisions on the composite profile
          screen.
        </Empty>
      </div>
    )
  }

  return (
    <div className="content-wide">
      <WorkflowMap journey={journey.data?.journey || {}} compact />
      <ScreenIntro pathname="/admin" />

      <Tabs tabs={TABS} active={tab} onChange={setTab} />

      {tab === 'models' && <ModelsTab />}
      {tab === 'sources' && <SourcesTab />}
      {tab === 'activity' && <ActivityTab onGoToSources={() => setTab('sources')} />}
      {tab === 'audit' && <AuditTab />}
      {tab === 'logs' && <LogsTab />}
      {tab === 'data' && <DataTab />}
      {/* How much of the corpus knows who is actually hiring, by rung and by
          the reason a non-answer is a non-answer (proposal section 4.8). */}
      {tab === 'employers' && <EmployerCoveragePanel />}
    </div>
  )
}
