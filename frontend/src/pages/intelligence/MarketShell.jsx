/**
 * The shared shell around both "read the market" screens (NFR-305, CR-405).
 *
 * /insights ("What works") and /intelligence ("Dream-job intelligence") answer
 * the same underlying question - *what is the market telling me, and what do I
 * do with it?* - from two angles. They now share one shell: the same journey
 * strip, the same screen-intro mechanism (each route keeps its own help copy,
 * and therefore its own title), a cross-link between the two readings, and one
 * shared advisory-only notice.
 *
 * The shell owns only the furniture the two routes have in common. Each route
 * keeps its own panels and its own default landing tab, so /insights still
 * opens on the reply-rate figures and /intelligence still opens on the gap
 * analysis. Neither route is a dead end: the cross-link at the top moves
 * between them without changing the URL contract or the router.
 */

import { Link } from 'react-router-dom'

import { ScreenIntro } from '../../components/Help'
import WorkflowMap from '../../components/WorkflowMap'
import { AdvisoryNotice } from './panels'

/**
 * The two routes that share this shell. `current` is the WorkflowMap stage the
 * route highlights; `pathname` is handed to ScreenIntro so the route keeps the
 * help copy it had before, and the topbar keeps its own title (App.jsx is
 * untouched).
 */
export const MARKET_ROUTES = [
  {
    key: 'insights',
    to: '/insights',
    pathname: '/insights',
    label: 'What works',
    current: 'learning',
    blurb: 'Outcome rates across your applications, and where to redirect.',
  },
  {
    key: 'intelligence',
    to: '/intelligence',
    pathname: '/intelligence',
    label: 'Dream-job intelligence',
    current: 'scoring',
    blurb: 'Gaps, stepping stones, market fit and profile advice against your dream job.',
  },
]

const ROUTE_BY_KEY = Object.fromEntries(MARKET_ROUTES.map((r) => [r.key, r]))

/** The cross-link between the two readings, so neither is a dead end. */
function MarketNav({ active }) {
  return (
    <div className="row row-wrap" style={{ gap: 8, alignItems: 'center', marginBottom: 16 }}>
      <span className="tiny muted">Reading the market:</span>
      <div className="chips">
        {MARKET_ROUTES.map((r) => (
          <Link
            key={r.key}
            to={r.to}
            title={r.blurb}
            className={`chip clickable${active === r.key ? ' on' : ''}`}
          >
            {r.label}
          </Link>
        ))}
      </div>
      <span className="tiny muted">
        — the outcome figures and the dream-job gaps are two views of the same question.
      </span>
    </div>
  )
}

export default function MarketShell({ route, journey, discretionMode, children }) {
  const meta = ROUTE_BY_KEY[route] || MARKET_ROUTES[0]

  return (
    <div className="content-wide">
      <WorkflowMap journey={journey || {}} compact current={meta.current} />
      <ScreenIntro pathname={meta.pathname} />
      <MarketNav active={meta.key} />
      <AdvisoryNotice route={meta.key} discretionMode={discretionMode} />
      {children}
    </div>
  )
}
