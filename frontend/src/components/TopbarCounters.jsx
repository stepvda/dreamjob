/**
 * The shell's corpus counters: Companies, Jobs and Contacts, always left of
 * the username on every screen.
 *
 * They exist so background collection is visible while the user works
 * somewhere else: the numbers are polled from `/api/overview/counters` and
 * grow as the corpus does. Nothing is rendered before the first successful
 * load, and nothing after a 401 - a signed-out or unloaded header must not
 * show zeros it cannot vouch for (FR-361, NFR-502).
 */

import { Fragment } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import Icon from './Icon'
import { usePolling } from './ui'

const POLL_MS = 15000

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

/**
 * 1234 -> "1.2k", 57345 -> "57.3k", 1200000 -> "1.2M". One decimal at most so
 * the chip stays narrow; the exact number lives in the tooltip. The 999950
 * threshold keeps 999,999 from rounding up to "1000k" instead of "1M".
 */
export function formatCompact(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return '–'
  const abs = Math.abs(n)
  const scaled = (x) => {
    const rounded = Math.round(x * 10) / 10
    return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1)
  }
  if (abs < 1000) return String(Math.round(n))
  if (abs < 999950) return `${scaled(n / 1000)}k`
  return `${scaled(n / 1e6)}M`
}

const EXACT = new Intl.NumberFormat('en-GB')

export default function TopbarCounters() {
  const { data, error } = usePolling(() => api.get('/overview/counters'), POLL_MS)

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
            <span className="topbar-counter-value">{formatCompact(data[chip.key])}</span>
          </Link>
        </Fragment>
      ))}
    </div>
  )
}
