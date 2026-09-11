/**
 * Discretion mode (FR-385).
 *
 * Searching while employed is the case where a mistake costs a job rather than
 * an opportunity, so this section is deliberately separate from the five
 * directive groups and deliberately explicit about what it does and does not
 * guarantee.
 *
 * The editor keeps the stored representation exactly: the current employer is
 * not a field of its own — it lives in `discretion_excluded_companies` with
 * reason `current_employer`. That is what the main directive save persists, so
 * discretion travels with the rest of the set and no second write path can
 * leave a set half-saved.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { Caution, HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, Field } from '../../components/ui'
import { Advanced, CompanyList } from './controls'

export default function DiscretionCard({ value, onChange, vocab, savedId }) {
  const all = value.discretion_excluded_companies || []
  const employer = all.find((c) => c.reason === 'current_employer') || null
  const others = all.filter((c) => c.reason !== 'current_employer')
  const contacts = value.discretion_excluded_contacts || []
  const on = Boolean(value.discretion_mode)

  function setEmployer(patch) {
    const next = { ...(employer || { name: '', domain: null }), ...patch }
    if (!next.name?.trim()) {
      onChange({ discretion_excluded_companies: others })
      return
    }
    onChange({
      discretion_excluded_companies: [
        { ...next, reason: 'current_employer', match_group_entities: true },
        ...others,
      ],
    })
  }

  function setOthers(list) {
    onChange({
      discretion_excluded_companies: employer ? [employer, ...list] : list,
    })
  }

  return (
    <div className="card dir-discretion">
      <div className="card-header">
        <span className="row" style={{ color: 'var(--warn)' }}>
          <Icon name="lock" />
        </span>
        <h3>Discretion mode</h3>
        <HelpTip term="discretion_mode" />
        <div className="spacer" />
        {on ? (
          <Badge tone="warn">
            <Icon name="lock" /> active
          </Badge>
        ) : (
          <Badge>off</Badge>
        )}
      </div>

      <label className="checkline" style={{ marginBottom: 12 }}>
        <input
          type="checkbox"
          checked={on}
          onChange={(e) => onChange({ discretion_mode: e.target.checked })}
        />
        <strong>I am searching while employed. Keep this search away from my employer.</strong>
      </label>

      {/* FR-385: exclusion is name- and domain-based matching, which is strong
          but not a guarantee. Saying so here is the honest form of the feature. */}
      <Caution title="What this does, and what it cannot do">
        Every company you name below — and, where you leave “Group” ticked, anything the system
        can relate to it by name or shared domain — is excluded from collection, ranking,
        contact lookup and generated content. Contacts likely to expose the search are excluded
        too, and no action that signals job hunting is taken on LinkedIn. It cannot control what
        a recruiter you contact chooses to say, and a group company under an unrelated trading
        name will not be caught unless you name it.
      </Caution>

      <div style={{ opacity: on ? 1 : 0.55, pointerEvents: on ? 'auto' : 'none', marginTop: 14 }}>
        <Field
          label={
            <>
              Current employer
              <HelpTip term="group_entity" />
            </>
          }
          hint="Named first because everything else keys off it: its group entities and its recruiters are excluded with it."
        >
          <div className="row">
            <input
              type="text"
              value={employer?.name || ''}
              placeholder="Employer name"
              onChange={(e) => setEmployer({ name: e.target.value })}
            />
            <input
              type="text"
              value={employer?.domain || ''}
              placeholder="domain (optional, catches subsidiaries)"
              style={{ maxWidth: 260 }}
              onChange={(e) => setEmployer({ domain: e.target.value || null })}
            />
          </div>
        </Field>

        <Field
          label="Other companies to keep out"
          hint="Group entities, sister companies, sensitive clients, a former employer you would rather not hear from."
        >
          <CompanyList
            value={others}
            onChange={setOthers}
            reasons={vocab?.groups?.company_exclusion_reason}
            placeholder="Company to exclude"
          />
        </Field>

        <Field
          label="Contacts who must never be approached"
          hint="Current colleagues, your employer's recruiters, anyone who would mention it."
        >
          <ContactList
            value={contacts}
            onChange={(x) => onChange({ discretion_excluded_contacts: x })}
            reasons={vocab?.groups?.contact_exclusion_reason}
          />
        </Field>

        <Advanced label="Advanced discretion tools">
          <ExclusionCheck savedId={savedId} />
        </Advanced>
      </div>
    </div>
  )
}

/* --- Excluded contacts (FR-385) -------------------------------------------- */

function ContactList({ value = [], onChange, reasons }) {
  const [draft, setDraft] = useState({ full_name: '', company_name: '', email: '' })

  function add() {
    if (!draft.full_name.trim() && !draft.email.trim()) return
    onChange([
      ...value,
      {
        full_name: draft.full_name.trim() || null,
        email: draft.email.trim() || null,
        linkedin_url: null,
        company_name: draft.company_name.trim() || null,
        reason: 'current_colleague',
        note: null,
      },
    ])
    setDraft({ full_name: '', company_name: '', email: '' })
  }

  return (
    <div className="col" style={{ gap: 8 }}>
      {value.map((c, i) => (
        <div className="dir-listrow" key={`${c.full_name || c.email}-${i}`}>
          <div className="col" style={{ gap: 2, minWidth: 0 }}>
            <strong className="small">{c.full_name || c.email}</strong>
            <span className="tiny muted">
              {[c.company_name, c.full_name && c.email ? c.email : null].filter(Boolean).join(' · ') || '—'}
            </span>
          </div>
          <div className="spacer" />
          <select
            className="dir-inline-select"
            value={c.reason || 'other'}
            onChange={(e) =>
              onChange(value.map((x, j) => (j === i ? { ...x, reason: e.target.value } : x)))
            }
          >
            {(reasons || []).map((o) => (
              <option key={o.key} value={o.key}>
                {o.label}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            aria-label={`Remove ${c.full_name || c.email}`}
            onClick={() => onChange(value.filter((_, j) => j !== i))}
          >
            <Icon name="x" />
          </button>
        </div>
      ))}
      <div className="row">
        <input
          type="text"
          value={draft.full_name}
          placeholder="Full name"
          onChange={(e) => setDraft({ ...draft, full_name: e.target.value })}
        />
        <input
          type="text"
          value={draft.company_name}
          placeholder="Company"
          style={{ maxWidth: 180 }}
          onChange={(e) => setDraft({ ...draft, company_name: e.target.value })}
        />
        <input
          type="email"
          value={draft.email}
          placeholder="e-mail (optional)"
          style={{ maxWidth: 200 }}
          onChange={(e) => setDraft({ ...draft, email: e.target.value })}
        />
        <button
          type="button"
          className="btn btn-sm"
          onClick={add}
          disabled={!draft.full_name.trim() && !draft.email.trim()}
        >
          <Icon name="plus" /> Add
        </button>
      </div>
    </div>
  )
}

/* --- "Would this company be excluded?" (FR-385) ---------------------------- */

/**
 * The rules catch group entities by name prefix and shared domain, which is
 * exactly the part a user cannot verify by reading their own list. This asks
 * the backend to judge one company and explain the verdict, before a campaign
 * runs rather than after.
 */
function ExclusionCheck({ savedId }) {
  const [name, setName] = useState('')
  const [domain, setDomain] = useState('')
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function run() {
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      const res = await api.post(`/directives/${savedId}/discretion/check`, {
        company: { name: name.trim(), domain: domain.trim() || null },
      })
      setResult(res)
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Field
      label="Check a company against these rules"
      hint={
        savedId
          ? 'Try the name of a sister company you are unsure about.'
          : 'Available once the directive set is saved.'
      }
    >
      <div className="row">
        <input
          type="text"
          value={name}
          placeholder="Company name"
          disabled={!savedId}
          onChange={(e) => setName(e.target.value)}
        />
        <input
          type="text"
          value={domain}
          placeholder="domain"
          style={{ maxWidth: 180 }}
          disabled={!savedId}
          onChange={(e) => setDomain(e.target.value)}
        />
        <button
          type="button"
          className="btn btn-sm"
          disabled={!savedId || !name.trim() || busy}
          onClick={run}
        >
          {busy ? (
            <span className="spinner" />
          ) : (
            <>
              <Icon name="search" /> Check
            </>
          )}
        </button>
      </div>
      {error && <span className="small" style={{ color: 'var(--danger)' }}>{error.message}</span>}
      {result?.company && (
        <div className="row" style={{ marginTop: 6 }}>
          {result.company.excluded ? (
            <Badge tone="ok">
              <Icon name="lock" /> excluded — {result.company.reason}
            </Badge>
          ) : (
            <Badge tone="warn">
              <Icon name="warning" /> not excluded — this campaign could reach it
            </Badge>
          )}
        </div>
      )}
    </Field>
  )
}
