/**
 * Who this company competes with (FR-224) and when it is worth approaching
 * (FR-225, FR-402).
 *
 * A competitor is only useful if you can see why it was proposed, so every
 * peer carries the bases that produced it and the strength that converging
 * bases add up to. Adopting one puts it on your own target list; the peer
 * itself stays shared.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import { Badge, Empty, ErrorBox, Meter, Provenance, formatDate } from '../../components/ui'

const SIGNAL_TONE = {
  funding: 'ok',
  headcount_growth: 'ok',
  new_office: 'ok',
  product_launch: 'info',
  postings: 'info',
  leadership_change: 'warn',
  reorg: 'warn',
  competitor_layoff: 'warn',
  fiscal_year_start: undefined,
}

const FLAG_TONE = {
  apply_now: 'ok',
  favourable: 'ok',
  watch: 'info',
  wait: 'warn',
  neutral: undefined,
}

const FLAG_LABEL = {
  apply_now: 'Apply now',
  favourable: 'Favourable',
  watch: 'Worth watching',
  wait: 'Wait for the window',
  neutral: 'Nothing either way',
}

/** FR-224: the basis names the evidence, not the confidence. */
const BASIS_LABEL = {
  sector: 'same sector',
  customers: 'shared customers',
  press: 'named together in the press',
  product: 'comparable product',
  directory: 'listed as peers in a directory',
  linkedin: 'peers on LinkedIn',
}

export default function CompanyMarket({ companyId, profile, competitors, onAdopted }) {
  const suggestions = competitors?.suggestions ?? profile.competitors ?? []
  const signals = profile.hiring_signals || []
  const timing = profile.timing || {}
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [adopted, setAdopted] = useState({})

  async function adopt(suggestion) {
    const linkId = (suggestion.link_ids || [])[0]
    if (!linkId) return
    setBusy(linkId)
    setError(null)
    try {
      const result = await api.post(`/companies/${companyId}/competitors/${linkId}/adopt`)
      setAdopted((a) => ({ ...a, [linkId]: result }))
      onAdopted?.()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="stack">
      <div className="card">
        <div className="card-header">
          <h3>
            When to approach them
            <HelpTip term="application_window" />
          </h3>
          <div className="spacer" />
          <Badge tone={FLAG_TONE[timing.timing_flag]}>
            {FLAG_LABEL[timing.timing_flag] || timing.timing_flag || 'not assessed'}
          </Badge>
        </div>

        <p style={{ marginTop: 0 }}>{timing.rationale || 'No dated signals to reason from yet.'}</p>

        <div className="row row-wrap small muted">
          {timing.window_start && (
            <span>
              Window {formatDate(timing.window_start)}
              {timing.window_end ? ` – ${formatDate(timing.window_end)}` : ''}
            </span>
          )}
          {timing.score != null && <span>· evidence strength {Math.round(timing.score * 100)}%</span>}
        </div>

        {(timing.drivers || []).length > 0 && (
          <div className="col" style={{ gap: 6, marginTop: 12 }}>
            {timing.drivers.map((d, i) => (
              <div key={i} className="row small">
                <Badge tone={SIGNAL_TONE[d.signal_type]}>{d.signal_type}</Badge>
                <span>{d.reason}</span>
                <div className="spacer" />
                {d.occurred_at && <span className="tiny muted">{formatDate(d.occurred_at)}</span>}
                <Provenance source={d.source_url} />
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="card">
        <div className="card-header">
          <h3>
            Hiring signals
            <HelpTip title="Hiring signal">
              A dated, sourced event that says something about whether this company is about to
              need people: funding, a new office, a product launch, a burst of postings, a
              reorganisation or a leadership change. Signals are what turns a company into a
              speculative opening, so each one keeps its date and the page it was read from
              (FR-225).
            </HelpTip>
          </h3>
          <div className="spacer" />
          <span className="badge">{signals.length}</span>
        </div>

        {signals.length === 0 ? (
          <p className="small muted" style={{ margin: 0 }}>
            No signals are on record. They are detected from the newsroom, filings and job
            postings when the profile is refreshed.
          </p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Signal</th>
                  <th>What happened</th>
                  <th>Date</th>
                  <th style={{ minWidth: 120 }}>Strength</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {signals.map((s) => (
                  <tr key={s.id}>
                    <td>
                      <Badge tone={SIGNAL_TONE[s.signal_type]}>{s.signal_type}</Badge>
                    </td>
                    <td className="small">{s.description || '–'}</td>
                    <td className="small nowrap">{formatDate(s.occurred_at || s.collected_at)}</td>
                    <td>
                      <Meter value={(s.strength || 0) * 100} width={110} />
                    </td>
                    <td>
                      <Provenance source={s.source_url} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card">
        <div className="card-header">
          <h3>
            Competitors and peers
            <HelpTip term="competitor_basis" />
          </h3>
          <div className="spacer" />
          {competitors && !competitors.meets_minimum && (
            <Badge tone="warn">fewer than the three peers FR-224 asks for</Badge>
          )}
        </div>

        {error && <ErrorBox error={error} />}

        {suggestions.length === 0 ? (
          <Empty title="No peers proposed yet">
            Competitor discovery runs with a profile refresh. It reads the sector, the customers
            named on the site and the companies the press mentions alongside this one.
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Peer</th>
                  <th>Why it was proposed</th>
                  <th style={{ minWidth: 130 }}>Strength</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {suggestions.map((s) => {
                  const linkId = (s.link_ids || [])[0]
                  const isAdopted = s.on_target_list || Boolean(adopted[linkId])
                  return (
                    <tr key={linkId || s.name}>
                      <td>
                        {s.peer_company_id ? (
                          <Link to={`/companies/${s.peer_company_id}`} style={{ fontWeight: 600 }}>
                            {s.name}
                          </Link>
                        ) : (
                          <strong>{s.name}</strong>
                        )}
                        <div className="tiny muted">
                          {[s.domain, s.country, s.size_band].filter(Boolean).join(' · ') ||
                            'not profiled yet'}
                        </div>
                      </td>
                      <td className="small">
                        <div className="chips">
                          {(s.bases || []).map((b) => (
                            <span key={b} className="chip" title={`strength ${s.basis_strengths?.[b] ?? '–'}`}>
                              {BASIS_LABEL[b] || b}
                            </span>
                          ))}
                        </div>
                        {(s.evidence || []).length > 0 && (
                          <div className="tiny muted" style={{ marginTop: 4 }}>
                            {s.evidence[0]}
                          </div>
                        )}
                      </td>
                      <td>
                        <Meter value={(s.strength || 0) * 100} width={120} />
                      </td>
                      <td className="nowrap">
                        {isAdopted ? (
                          <Badge tone="ok">on your target list</Badge>
                        ) : (
                          <button
                            className="btn btn-sm"
                            disabled={!linkId || busy === linkId}
                            onClick={() => adopt(s)}
                          >
                            Add to my list
                          </button>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}

        <p className="tiny muted" style={{ marginTop: 10 }}>
          Adding a peer puts it on your own watchlist and, if it is not yet profiled, creates the
          shared record the next campaign can profile (FR-224). Nothing about you is written to
          the shared knowledge base (FR-344).
        </p>
      </div>
    </div>
  )
}
