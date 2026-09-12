/**
 * The shell's corpus counters: Companies, Jobs and Contacts, always left of
 * the username on every screen.
 *
 * They exist so background collection is visible while the user works
 * somewhere else: the numbers are polled from `/api/overview/counters` and
 * grow as the corpus does. They are shown exactly (thousands separators, no
 * rounding) so a single new record is visible, and polled fresh so the
 * increase appears promptly. Nothing is rendered before the first successful
 * load, and nothing after a 401 - a signed-out or unloaded header must not
 * show zeros it cannot vouch for (FR-361, NFR-502).
 */

import { Fragment } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import Icon from './Icon'
import { usePolling } from './ui'

/** Short enough that a new record shows up while the user watches; the
 *  endpoint's own statement is ~1 ms, so `fresh=1` is cheap. */
const POLL_MS = 10000

/** The three chips, each with the wording that defines its number. */
const CHIPS = [
  {
    key: 'companies',
    label: 'Companies',
    icon: 'companies',
    to: '/companies',
    title: (n) => `${n} companies in the shared knowledge base`,
  },
  {
    key: 'jobs',
    label: 'Jobs',
    icon: 'vacancy',
    to: '/companies',
    title: (n) => `${n} advertised vacancies in the shared corpus - browse them under Companies`,
  },
  {
    key: 'contacts',
    label: 'Contacts',
    icon: 'contacts',
    to: '/contacts',
    title: (n) => `${n} contacts visible to you`,
  },
]

/** Exact, e.g. 1234 -> "1,234", 57345 -> "57,345". No k/M rounding: the
 *  point of the counters is to watch individual records land. */
const EXACT = new Intl.NumberFormat('en-GB')

export default function TopbarCounters() {
  const { data, error } = usePolling(() => api.get('/overview/counters?fresh=1'), POLL_MS)

  // Signed out, or still loading the first payload: stay out of the header.
  if (error?.status === 401 || !data) return null

  return (
    <div className="topbar-counters" aria-label="Corpus counters">
      {CHIPS.map((chip, i) => (
        <Fragment key={chip.key}>
          {i > 0 && <span className="topbar-counters-sep" aria-hidden="true" />}
          <Link
            className="topbar-counter"
            to={chip.to}
            title={chip.title(EXACT.format(data[chip.key] ?? 0))}
          >
            <Icon name={chip.icon} />
            <span className="topbar-counter-label">{chip.label}</span>
            <span className="topbar-counter-value">
              {EXACT.format(data[chip.key] ?? 0)}
            </span>
          </Link>
        </Fragment>
      ))}
    </div>
  )
}
