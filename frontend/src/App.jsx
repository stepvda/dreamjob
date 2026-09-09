/**
 * Application shell and routing (CR-407).
 *
 * The navigation mirrors the pipeline in section 2.3 of the specification -
 * profile, then directives, then campaign, then results, then applying, then
 * what happens afterwards - because that ordering is the mental model the
 * whole product is built on.
 */

import { Suspense, lazy, useEffect, useState } from 'react'
import {
  BrowserRouter,
  NavLink,
  Navigate,
  Route,
  Routes,
  useLocation,
} from 'react-router-dom'

import { api, setUnauthorizedHandler } from './api/client'
import { HelpButton, HelpPanel } from './components/Help'
import Icon from './components/Icon'
import { SessionContext } from './session'
import SignIn from './pages/SignIn'

// Route-level code splitting (NFR-101). The shell and the screen you asked
// for load; the other twenty do not. Without this every visitor downloads
// the company profile, the PDF previews and the whole administration area
// before the profile page can paint.
const OverviewPage = lazy(() => import('./pages/OverviewPage'))
const ContactsPage = lazy(() => import('./pages/ContactsPage'))
const ProfilePage = lazy(() => import('./pages/ProfilePage'))
const CompositePage = lazy(() => import('./pages/CompositePage'))
const DreamJobPage = lazy(() => import('./pages/DreamJobPage'))
const DirectivesPage = lazy(() => import('./pages/DirectivesPage'))
const CampaignsPage = lazy(() => import('./pages/CampaignsPage'))
const CampaignDetailPage = lazy(() => import('./pages/CampaignDetailPage'))
const BrowserPage = lazy(() => import('./pages/BrowserPage'))
const OpportunitiesPage = lazy(() => import('./pages/OpportunitiesPage'))
const OpportunityDetailPage = lazy(() => import('./pages/OpportunityDetailPage'))
const CompaniesPage = lazy(() => import('./pages/CompaniesPage'))
const CompanyDetailPage = lazy(() => import('./pages/CompanyDetailPage'))
const ApplicationsPage = lazy(() => import('./pages/ApplicationsPage'))
const ApplyBrowserPage = lazy(() => import('./pages/ApplyBrowserPage'))
const PipelinePage = lazy(() => import('./pages/PipelinePage'))
const ResponsesPage = lazy(() => import('./pages/ResponsesPage'))
const InsightsPage = lazy(() => import('./pages/InsightsPage'))
const MonitoringPage = lazy(() => import('./pages/MonitoringPage'))
const IntelligencePage = lazy(() => import('./pages/IntelligencePage'))
const NetworkingPage = lazy(() => import('./pages/NetworkingPage'))
const MailSettingsPage = lazy(() => import('./pages/MailSettingsPage'))
const AdminPage = lazy(() => import('./pages/AdminPage'))


// Each group carries a point on the spectrum. The hue is the phase, so a
// violet screen is always about your profile and an amber one is always about
// applying - see styles/theme.css.
const NAV = [
  {
    label: 'Overview',
    phase: 'phase-0',
    items: [{ to: '/overview', icon: 'overview', label: 'Where I am' }],
  },
  {
    label: 'Profile',
    phase: 'phase-1',
    items: [
      { to: '/profile', icon: 'profile', label: 'Profile' },
      { to: '/composite', icon: 'composite', label: 'Composite profile' },
      { to: '/dream-job', icon: 'dream', label: 'Dream job' },
    ],
  },
  {
    label: 'Plan',
    phase: 'phase-2',
    items: [
      { to: '/directives', icon: 'directives', label: 'Directives' },
      { to: '/campaigns', icon: 'campaign', label: 'Campaigns' },
      { to: '/browser', icon: 'browser', label: 'Browser session' },
    ],
  },
  {
    label: 'Discover',
    phase: 'phase-3',
    items: [
      { to: '/opportunities', icon: 'opportunities', label: 'Opportunities' },
      { to: '/companies', icon: 'companies', label: 'Companies' },
      { to: '/intelligence', icon: 'intelligence', label: 'Dream-job gap' },
    ],
  },
  {
    label: 'Apply',
    phase: 'phase-4',
    items: [
      { to: '/apply', icon: 'send', label: 'Apply browser' },
      { to: '/contacts', icon: 'contacts', label: 'Contacts' },
      { to: '/applications', icon: 'applications', label: 'Applications' },
      { to: '/networking', icon: 'networking', label: 'Networking' },
    ],
  },
  {
    label: 'Follow up',
    phase: 'phase-5',
    items: [
      { to: '/pipeline', icon: 'pipeline', label: 'Pipeline' },
      { to: '/responses', icon: 'responses', label: 'Responses' },
      { to: '/insights', icon: 'insights', label: 'What works' },
    ],
  },
  {
    label: 'System',
    phase: 'phase-0',
    items: [
      { to: '/monitoring', icon: 'monitoring', label: 'Monitoring' },
      { to: '/mail', icon: 'mailsetup', label: 'Mail setup' },
      { to: '/admin', icon: 'admin', label: 'Administration' },
    ],
  },
]

// Which phase hue a route belongs to, for the header and page furniture.
const ROUTE_PHASE = Object.fromEntries(
  NAV.flatMap((g) => g.items.map((i) => [i.to, g.phase])),
)

const ROUTE_ICON = Object.fromEntries(
  NAV.flatMap((g) => g.items.map((i) => [i.to, i.icon])),
)

const TITLES = {
  '/overview': 'Where I am',
  '/responses': 'Responses received',
  '/insights': 'What works, and where to redirect',
  '/profile': 'Profile',
  '/composite': 'Composite profile',
  '/dream-job': 'Dream job',
  '/directives': 'Search directives',
  '/campaigns': 'Campaigns',
  '/browser': 'Browser session',
  '/opportunities': 'Opportunities',
  '/companies': 'Companies',
  '/intelligence': 'Dream-job intelligence',
  '/apply': 'Apply browser',
  '/contacts': 'Hiring contacts',
  '/applications': 'Applications',
  '/networking': 'Networking and export',
  '/pipeline': 'Application pipeline',
  '/monitoring': 'Monitoring',
  '/mail': 'Mail setup',
  '/admin': 'Administration',
}

function Shell({ session, onSignOut }) {
  const location = useLocation()
  const [helpOpen, setHelpOpen] = useState(false)

  // Opening a different screen closes the drawer: help is about where you are.
  useEffect(() => setHelpOpen(false), [location.pathname])

  const base = '/' + location.pathname.split('/')[1]
  const title = TITLES[base] || 'Dream Job'
  const phase = ROUTE_PHASE[base] || 'phase-0'

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <div className="row" style={{ gap: 8, alignItems: 'center' }}>
            <img src="/icon-48.png" alt="" width={22} height={22} />
            <h1>Dream Job</h1>
          </div>
          <p>{session.display_name}</p>
        </div>
        {NAV.map((group) => (
          <nav className={`nav-group ${group.phase}`} key={group.label}>
            <div className="nav-label">{group.label}</div>
            {group.items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) => 'nav-item' + (isActive ? ' active' : '')}
              >
                <Icon name={item.icon} />
                <span>{item.label}</span>
              </NavLink>
            ))}
          </nav>
        ))}
      </aside>

      <div className={`main ${phase}`}>
        <header className="topbar">
          <h2>
            <Icon name={ROUTE_ICON[base] || 'overview'} />
            {title}
          </h2>
          <div className="spacer" />
          <span className="small muted">{session.email}</span>
          <HelpButton onOpen={() => setHelpOpen(true)} />
          <button className="btn btn-sm btn-ghost" onClick={onSignOut}>
            Sign out
          </button>
        </header>

        {session.discretion_mode && (
          <div className="discretion-bar">
            <Icon name="lock" />
            <span>
              Discretion mode is active — the current employer, its group entities and
              exposing contacts are excluded from every search, ranking and message.
            </span>
          </div>
        )}

        <div className="content">
          <Suspense
            fallback={
              <div className="empty" style={{ paddingTop: 80 }}>
                <span className="spinner" />
              </div>
            }
          >
            <Routes>
              <Route path="/" element={<Navigate to="/overview" replace />} />
              <Route path="/overview" element={<OverviewPage />} />
              <Route path="/profile" element={<ProfilePage />} />
              <Route path="/composite" element={<CompositePage />} />
              <Route path="/dream-job" element={<DreamJobPage />} />
              <Route path="/directives" element={<DirectivesPage />} />
              <Route path="/campaigns" element={<CampaignsPage />} />
              <Route path="/campaigns/:id" element={<CampaignDetailPage />} />
              <Route path="/browser" element={<BrowserPage />} />
              <Route path="/opportunities" element={<OpportunitiesPage />} />
              <Route path="/opportunities/:id" element={<OpportunityDetailPage />} />
              <Route path="/companies" element={<CompaniesPage />} />
              <Route path="/companies/:id" element={<CompanyDetailPage />} />
              <Route path="/apply" element={<ApplyBrowserPage />} />
              <Route path="/contacts" element={<ContactsPage />} />
              <Route path="/applications" element={<ApplicationsPage />} />
              <Route path="/pipeline" element={<PipelinePage />} />
              <Route path="/responses" element={<ResponsesPage />} />
              <Route path="/insights" element={<InsightsPage />} />
              <Route path="/monitoring" element={<MonitoringPage />} />
              <Route path="/intelligence" element={<IntelligencePage />} />
              <Route path="/networking" element={<NetworkingPage />} />
              <Route path="/mail" element={<MailSettingsPage />} />
              <Route path="/admin" element={<AdminPage />} />
              <Route path="*" element={<div className="empty"><h3>Not found</h3></div>} />
            </Routes>
          </Suspense>
        </div>
      </div>

      <HelpPanel
        pathname={location.pathname}
        open={helpOpen}
        onClose={() => setHelpOpen(false)}
      />
    </div>
  )
}

export default function App() {
  const [session, setSession] = useState(undefined) // undefined = still checking

  useEffect(() => {
    setUnauthorizedHandler(() => setSession(null))
    api
      .get('/auth/me')
      .then(setSession)
      .catch(() => setSession(null))
  }, [])

  async function signOut() {
    try {
      await api.post('/auth/logout')
    } finally {
      setSession(null)
    }
  }

  if (session === undefined) {
    return (
      <div className="empty" style={{ paddingTop: 120 }}>
        <span className="spinner" />
      </div>
    )
  }

  if (session === null) {
    return <SignIn onSignedIn={setSession} />
  }

  return (
    <SessionContext.Provider value={{ session, setSession }}>
      <BrowserRouter>
        <Shell session={session} onSignOut={signOut} />
      </BrowserRouter>
    </SessionContext.Provider>
  )
}
