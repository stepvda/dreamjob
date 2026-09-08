/**
 * Salary negotiation brief (FR-444).
 *
 * Available from the interview stage onwards, because before that there is
 * nothing to negotiate — the backend enforces the same condition and answers
 * 409 if asked earlier.
 *
 * The figures are computed, not written: ability to pay and personnel cost per
 * head come from filed accounts, the market band from the compensation
 * estimator, the floor from your own compensation directives. The language
 * model is only ever asked to argue the numbers it is given, never to choose
 * them — which is why every argument shows the evidence it rests on and why
 * the whole brief is labelled advisory (NFR-305).
 */

import { useState } from 'react'

import { api, ApiError } from '../../api/client'
import { Caution, HelpTip } from '../../components/Help'
import {
  Badge,
  ErrorBox,
  Loading,
  Meter,
  Modal,
  formatMoney,
  formatPercent,
  useFetch,
} from '../../components/ui'
import { normaliseBrief } from './shared'

const STRENGTH_TONE = { strong: 'ok', moderate: 'info', weak: 'warn' }
const COST_TONE = { low: 'ok', medium: 'warn', high: 'danger' }

export default function NegotiationBrief({ card, onClose }) {
  const stored = useFetch(
    () =>
      api.get(`/pipeline/negotiation/${card.opportunity_id}`).catch((err) => {
        // 404 is "not generated yet", which is a state of this screen rather
        // than a failure of it.
        if (err instanceof ApiError && err.status === 404) return null
        throw err
      }),
    [card.opportunity_id],
  )

  const [generated, setGenerated] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const brief = normaliseBrief(generated || stored.data)

  async function build() {
    setBusy(true)
    setError(null)
    try {
      setGenerated(
        await api.post(`/pipeline/negotiation/${card.opportunity_id}`, {
          use_llm: true,
          force: false,
        }),
      )
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      wide
      title={`Negotiation brief — ${card.opportunity_title || 'this role'}`}
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={build} disabled={busy}>
            {busy ? <span className="spinner" /> : brief ? 'Rebuild from current figures' : 'Build the brief'}
          </button>
          <button className="btn btn-ghost" onClick={onClose}>
            Close
          </button>
        </>
      }
    >
      {stored.loading && <Loading rows={4} />}
      {stored.error && <ErrorBox error={stored.error} onRetry={stored.reload} />}
      {error && <ErrorBox error={error} />}

      {!stored.loading && !brief && (
        <p className="section-intro">
          Nothing has been prepared for this role yet. Building the brief reads the company's
          filed accounts, the compensation range for comparable roles and your own compensation
          directives, then writes the case for a figure.
        </p>
      )}

      {brief && (
        <>
          {/* NFR-305: scoring and money advice are advisory; you decide. */}
          <Caution title="Advisory, not a valuation">
            {brief.advisory ||
              'This brief is advisory. The figures are estimates from filed accounts and comparable roles, and the arguments are written from them — what you ask for is your decision.'}
          </Caution>

          <div className="card" style={{ marginTop: 14 }}>
            <div className="card-header">
              <h3>The ask</h3>
              {brief.stage && <Badge tone="accent">{brief.stage}</Badge>}
              <div className="spacer" />
              {brief.confidence != null && (
                <span className="small muted">
                  confidence {formatPercent(brief.confidence, 0)}
                </span>
              )}
            </div>
            <div className="grid grid-3">
              <div>
                <div className="stat-value">
                  {brief.suggestedAsk ||
                    `${formatMoney(brief.askMin, brief.currency)} – ${formatMoney(brief.askMax, brief.currency)}`}
                </div>
                <div className="stat-label">Suggested ask</div>
              </div>
              <div>
                <div className="stat-value">{formatMoney(brief.walkAway, brief.currency)}</div>
                <div className="stat-label">
                  Walk-away
                  <HelpTip term="walk_away" />
                </div>
              </div>
              <div>
                <div className="stat-value">
                  {formatMoney(brief.market?.min, brief.currency)} –{' '}
                  {formatMoney(brief.market?.max, brief.currency)}
                </div>
                <div className="stat-label">
                  Comparable range
                  <HelpTip term="market_range" />
                </div>
                <div className="stat-sub">
                  {(brief.market?.method || 'unknown').replace(/_/g, ' ')}
                  {brief.market?.sample_size ? ` · ${brief.market.sample_size} data points` : ''}
                </div>
              </div>
            </div>
            {brief.anchorRationale && (
              <p className="small muted" style={{ margin: '12px 0 0' }}>
                {brief.anchorRationale}
              </p>
            )}
          </div>

          <div className="card" style={{ marginTop: 14 }}>
            <div className="card-header">
              <h3>What the company can carry</h3>
            </div>
            <div className="grid grid-2">
              <div className="field">
                <label>
                  Ability to pay
                  <HelpTip term="ability_to_pay" />
                </label>
                <Meter value={brief.abilityToPay} />
                {brief.abilityRationale && (
                  <span className="hint">{brief.abilityRationale}</span>
                )}
              </div>
              <div className="field">
                <label>Personnel cost per employee</label>
                <div className="small">
                  {formatMoney(brief.personnelCostPerFte, brief.currency)}
                </div>
                {brief.financialsEstimated && (
                  <span className="hint">
                    Estimated from incomplete filings, so treat it as an order of magnitude.
                  </span>
                )}
              </div>
            </div>
          </div>

          <ArgumentList title="Arguments" items={brief.arguments} />
          <FallbackList items={brief.fallbacks} />

          {brief.ifPushed && (
            <div className="card" style={{ marginTop: 14 }}>
              <div className="card-header">
                <h3>If they ask for a number first</h3>
              </div>
              <p className="small" style={{ margin: 0 }}>
                {brief.ifPushed}
              </p>
            </div>
          )}

          {brief.risks.length > 0 && (
            <div className="card" style={{ marginTop: 14 }}>
              <div className="card-header">
                <h3>Where this is thin</h3>
              </div>
              <ul className="help-tips">
                {brief.risks.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            </div>
          )}

          {brief.gaps.length > 0 && (
            <div className="card" style={{ marginTop: 14 }}>
              <div className="card-header">
                <h3>What the evidence is missing</h3>
              </div>
              <ul className="help-tips">
                {brief.gaps.map((g, i) => (
                  <li key={i}>{g}</li>
                ))}
              </ul>
            </div>
          )}

          <div className="card" style={{ marginTop: 14 }}>
            <div className="card-header">
              <h3>PDF</h3>
            </div>
            {brief.pdfPath ? (
              <p className="small" style={{ margin: 0 }}>
                A PDF of this brief was written to <span className="mono">{brief.pdfPath}</span>.
                {' '}
                It is not downloadable through the API yet — the backend renders and stores it,
                but exposes no route that serves it, so open it from disk for now.
              </p>
            ) : (
              <p className="small muted" style={{ margin: 0 }}>
                The PDF could not be rendered for this brief. The content above is the brief;
                the PDF is only a convenience.
              </p>
            )}
          </div>
        </>
      )}
    </Modal>
  )
}

function ArgumentList({ title, items }) {
  if (!items?.length) return null
  return (
    <div className="card" style={{ marginTop: 14 }}>
      <div className="card-header">
        <h3>{title}</h3>
        <HelpTip term="negotiation_brief" />
      </div>
      {items.map((arg, i) => (
        <div key={i} style={{ marginBottom: 12 }}>
          <div className="row row-wrap" style={{ marginBottom: 3 }}>
            <strong className="small">{arg.point}</strong>
            <Badge tone={STRENGTH_TONE[arg.strength]}>{arg.strength}</Badge>
          </div>
          {arg.evidence && (
            <p className="small muted" style={{ margin: 0 }}>
              {arg.evidence}
            </p>
          )}
        </div>
      ))}
    </div>
  )
}

function FallbackList({ items }) {
  if (!items?.length) return null
  return (
    <div className="card" style={{ marginTop: 14 }}>
      <div className="card-header">
        <h3>If base salary will not move</h3>
        <HelpTip
          title="Fallback positions"
          align="right"
        >
          Things worth real money that sit in a different budget line than salary. They are
          ordered cheapest-to-the-employer first, because those are the ones most likely to be
          agreed in the room.
        </HelpTip>
      </div>
      {items.map((fb, i) => (
        <div key={i} style={{ marginBottom: 12 }}>
          <div className="row row-wrap" style={{ marginBottom: 3 }}>
            <strong className="small">{fb.ask}</strong>
            <Badge tone={COST_TONE[fb.cost_to_employer]}>
              costs them {fb.cost_to_employer}
            </Badge>
          </div>
          {fb.why && (
            <p className="small muted" style={{ margin: 0 }}>
              {fb.why}
            </p>
          )}
        </div>
      ))}
    </div>
  )
}
