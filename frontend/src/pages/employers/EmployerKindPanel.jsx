/**
 * "Who is hiring" on a company screen (FR-341, FR-344, NFR-402, CR-405).
 *
 * The same verdict the badge carries, at the size a company page can give it:
 * the state, the evidence in full with its quotes and dates, the one control
 * that answers it, and - when the answer is "we could not tell" - the cheapest
 * next rung, offered as a button rather than described.
 *
 * `cannot_tell` is a work item, not a dead end (`Agency_Research_Design.md`
 * section 7.3): "no website is known" wants a website, "the site needs a
 * browser" wants a person to look, "two registered companies share this name"
 * wants somebody to say which. Each reason gets its own next move, and the
 * ones that will not change on their own are not offered a "try again" button
 * that would only fail identically.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import EmployerKindBadge, {
  EmployerCorrectionModal,
  EmployerEvidence,
  REASON_NEXT_STEP,
  ROLE_DIRECT,
  employerRole,
  isUnresearched,
  reasonWords,
  serviceModelWords,
} from '../../components/EmployerKindBadge'
import { ErrorBox, Loading, SectionCard, formatDate, useFetch } from '../../components/ui'

/** Reasons a re-run would answer differently. The rest need a person. */
const RETRYABLE = new Set(['not_researched', 'unreachable', 'no_verifiable_evidence', 'llm_failed'])

export default function EmployerKindPanel({ companyId, companyName, phase = 'phase-3' }) {
  const [correcting, setCorrecting] = useState(false)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState(null)

  const { data, error, loading, reload, setData } = useFetch(
    () => (companyId ? api.get(`/employers/${companyId}/kind`) : null),
    [companyId],
  )

  if (!companyId) return null

  if (loading) {
    return (
      <SectionCard icon="companies" title="Who is hiring" phase={phase}>
        <Loading rows={3} />
      </SectionCard>
    )
  }

  if (error) {
    return (
      <SectionCard icon="companies" title="Who is hiring" phase={phase}>
        <ErrorBox error={error} onRetry={reload} />
      </SectionCard>
    )
  }

  const tag = data || {}
  const role = employerRole(tag)
  const unresearched = isUnresearched(tag)
  const retryable = unresearched || RETRYABLE.has(tag.reason)
  const model = serviceModelWords(tag.service_model)

  async function resolveNow() {
    setBusy(true)
    setActionError(null)
    try {
      const payload = await api.post(`/employers/${companyId}/resolve`, { force: !unresearched })
      if (payload?.verdict) setData(payload.verdict)
      else reload()
    } catch (err) {
      setActionError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <SectionCard
      icon="companies"
      title="Who is hiring"
      phase={phase}
      actions={
        <EmployerKindBadge
          tag={tag}
          companyId={companyId}
          companyName={companyName}
          align="right"
          interactive={false}
        />
      }
    >
      <p className="section-intro" style={{ marginTop: 0 }}>
        {role === ROLE_DIRECT
          ? 'This organisation hires for its own work, so everything else on this screen is about the company you would work for.'
          : 'Whether the organisation on a posting is the employer decides what the rest of the product may say about it.'}
        <HelpTip term="interim_agency" />
      </p>

      <div className="row row-wrap small muted" style={{ gap: 10, marginBottom: 10 }}>
        {model && <span>{model}</span>}
        {tag.vacancy_count != null && <span>· {tag.vacancy_count} postings</span>}
        {tag.established_at && <span>· established {formatDate(tag.established_at)}</span>}
        {tag.expires_at && <span>· re-checked after {formatDate(tag.expires_at)}</span>}
        {tag.attempts != null && tag.attempts > 1 && <span>· {tag.attempts} attempts</span>}
      </div>

      <div className="col small" style={{ gap: 6 }}>
        <EmployerEvidence
          tag={tag}
          companyId={companyId}
          companyName={companyName}
          compact={false}
        />
      </div>

      {tag.kind === 'cannot_tell' && (
        <div className="alert alert-info" style={{ marginTop: 12 }}>
          <div>
            <strong>Employer type not verified.</strong>{' '}
            {reasonWords(tag.reason) || 'the evidence did not settle it'}. What would settle it:{' '}
            {tag.next_step?.label || REASON_NEXT_STEP[tag.reason] || 'research this employer again'}.
            {!retryable && (
              <>
                {' '}
                Re-running the same read would fail the same way, so it is not offered as a button:
                this one needs a person.
              </>
            )}
          </div>
        </div>
      )}

      {actionError && <ErrorBox error={actionError} />}

      <div className="row row-wrap" style={{ gap: 8, marginTop: 14 }}>
        {retryable && (
          <button className="btn btn-sm" disabled={busy} onClick={resolveNow}>
            {busy ? <span className="spinner" /> : unresearched ? 'Research this employer' : 'Research again'}
          </button>
        )}
        <button className="btn btn-sm" onClick={() => setCorrecting(true)}>
          This is wrong
        </button>
      </div>

      {tag.advisory && (
        <p className="small muted" style={{ marginTop: 12, marginBottom: 0 }}>
          {tag.advisory}
        </p>
      )}

      {correcting && (
        <EmployerCorrectionModal
          tag={tag}
          companyId={companyId}
          companyName={companyName}
          onClose={() => setCorrecting(false)}
          onSaved={(payload) => {
            setCorrecting(false)
            if (payload?.verdict) setData(payload.verdict)
            else reload()
          }}
        />
      )}
    </SectionCard>
  )
}
