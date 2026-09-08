/**
 * The two dialogues behind the source catalogue (FR-363, IR-101).
 *
 * Kept out of SourcesTab so the table stays readable, and because these are
 * where the specification's weight actually sits: one records a legal
 * judgement, the other bounds what a source may cost.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { Caution, HelpTip } from '../../components/Help'
import {
  Badge,
  ErrorBox,
  Field,
  Modal,
  SectionCard,
  formatDate,
} from '../../components/ui'
import { eur, num, parseJson } from './format'

export const TOS_TONE = { permitted: 'ok', restricted: 'warn', prohibited: 'danger' }

/**
 * Deliberate by construction: the terms are shown in full, the administrator
 * has to tick a statement written in the first person and type the adapter key
 * before the button is live, and the note they leave is stored in the audit
 * entry. It still does not enable the source — that is a separate click on the
 * table, which is what the backend's own comment asks for.
 */
export function AcknowledgeModal({ row, onClose, onDone }) {
  const [accepted, setAccepted] = useState(false)
  const [typed, setTyped] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const ready = accepted && typed.trim() === row.adapter_key

  async function submit() {
    setBusy(true)
    setError(null)
    try {
      await api.post(`/admin/sources/${row.adapter_key}/acknowledge`, {
        accepted: true,
        note: note.trim() || null,
      })
      onDone()
    } catch (err) {
      setError(err)
      setBusy(false)
    }
  }

  return (
    <Modal
      title={`Terms for ${row.display_name}`}
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-danger" disabled={!ready || busy} onClick={submit}>
            {busy ? <span className="spinner" /> : 'Record my acknowledgement'}
          </button>
        </>
      }
    >
      {error && <ErrorBox error={error} />}

      <Caution title={`This source is marked “${row.tos_status}”`}>
        {row.legal_notes || 'No notes were recorded against this adapter.'}
      </Caution>

      <p className="small" style={{ lineHeight: 1.6 }}>
        Acknowledging does not switch the source on. It records that an administrator read
        the terms and accepted the risk on behalf of this installation; enabling it is a
        separate decision on the table afterwards, and withdrawing the acknowledgement
        disables it again.
      </p>

      <label className="checkline" style={{ marginBottom: 12 }}>
        <input
          type="checkbox"
          checked={accepted}
          onChange={(e) => setAccepted(e.target.checked)}
        />
        <span>
          I have read these terms and I accept, on behalf of this installation, the risk of
          using {row.display_name} automatically — including that the account used may be
          restricted.
        </span>
      </label>

      <Field
        label={`Type ${row.adapter_key} to confirm`}
        hint="Typed out in full, so this is not something a stray click can do."
      >
        <input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder={row.adapter_key} />
      </Field>

      <Field label="Note for the audit trail" hint="Optional. Who decided, and on what basis.">
        <textarea rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
      </Field>
    </Modal>
  )
}

/* --- Per-source caps and pacing (FR-363) ----------------------------------- */

export function SourceModal({ row, onClose, onSaved }) {
  const caps = row.caps || {}
  const [form, setForm] = useState({
    rate_limit_rps: row.rate_limit_rps ?? '',
    cost_per_call_eur: row.cost_per_call_eur ?? '',
    max_pages: caps.max_pages ?? '',
    max_records: caps.max_records ?? '',
    max_seconds: caps.max_seconds ?? '',
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [revoking, setRevoking] = useState(false)

  const capabilities = parseJson(row.query_capabilities, {}) || {}
  const industries = parseJson(row.coverage_industries, []) || []

  function set(k, v) {
    setForm((f) => ({ ...f, [k]: v }))
  }

  async function save() {
    setBusy(true)
    setError(null)
    try {
      // The backend ignores nulls, so an emptied field means "leave it alone"
      // rather than "clear it" — there is no clear route on this endpoint.
      const payload = {}
      for (const [k, v] of Object.entries(form)) {
        if (v !== '' && v != null) payload[k] = Number(v)
      }
      await api.patch(`/admin/sources/${row.adapter_key}`, payload)
      onSaved()
    } catch (err) {
      setError(err)
      setBusy(false)
    }
  }

  /** IR-101: withdrawing the acknowledgement disables the source again. */
  async function revoke() {
    setBusy(true)
    setError(null)
    try {
      await api.del(`/admin/sources/${row.adapter_key}/acknowledge`)
      onSaved()
    } catch (err) {
      setError(err)
      setBusy(false)
    }
  }

  return (
    <Modal
      title={row.display_name}
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={onClose}>
            Close
          </button>
          <button className="btn btn-primary" disabled={busy} onClick={save}>
            {busy ? <span className="spinner" /> : 'Save'}
          </button>
        </>
      }
    >
      {error && <ErrorBox error={error} />}

      <div className="row row-wrap" style={{ marginBottom: 14 }}>
        <Badge tone={TOS_TONE[row.tos_status]}>{row.tos_status}</Badge>
        <Badge>{row.source_type}</Badge>
        <Badge>{row.access_method}</Badge>
        {row.last_success_at && (
          <span className="small muted">last succeeded {formatDate(row.last_success_at)}</span>
        )}
      </div>

      {row.legal_notes && (
        <p className="small muted" style={{ lineHeight: 1.6 }}>
          {row.legal_notes}
        </p>
      )}

      <SectionCard title="Pacing" edge={false}>
        <div className="grid grid-2">
          <Field label="Requests per second" hint="Between 0 and 50.">
            <input
              type="number"
              step="0.1"
              value={form.rate_limit_rps}
              onChange={(e) => set('rate_limit_rps', e.target.value)}
            />
          </Field>
          <Field label="Cost per call, €" hint="Used in the campaign plan's cost estimate.">
            <input
              type="number"
              step="0.001"
              value={form.cost_per_call_eur}
              onChange={(e) => set('cost_per_call_eur', e.target.value)}
            />
          </Field>
        </div>
      </SectionCard>

      <SectionCard
        title={
          <>
            Caps for one run
            <HelpTip term="source_cap" />
          </>
        }
        edge={false}
      >
        <div className="grid grid-3">
          <Field label="Max pages">
            <input
              type="number"
              value={form.max_pages}
              onChange={(e) => set('max_pages', e.target.value)}
            />
          </Field>
          <Field label="Max records">
            <input
              type="number"
              value={form.max_records}
              onChange={(e) => set('max_records', e.target.value)}
            />
          </Field>
          <Field label="Max seconds">
            <input
              type="number"
              value={form.max_seconds}
              onChange={(e) => set('max_seconds', e.target.value)}
            />
          </Field>
        </div>
        <p className="tiny muted" style={{ margin: 0 }}>
          Leaving a field empty leaves the current cap in place; this endpoint has no way
          to clear one.
        </p>
      </SectionCard>

      <SectionCard title="What this adapter can be asked for" edge={false}>
        <div className="chips">
          {Object.entries(capabilities).map(([k, v]) => (
            <span key={k} className={`chip${v === true ? ' on' : ''}`}>
              {k.replace(/_/g, ' ')}
              {typeof v === 'number' ? `: ${num(v)}` : ''}
            </span>
          ))}
          {!Object.keys(capabilities).length && (
            <span className="small muted">The adapter declares no query capabilities.</span>
          )}
        </div>
        {industries.length > 0 && (
          <p className="small muted" style={{ marginBottom: 0 }}>
            Industries: {industries.join(', ')}
          </p>
        )}
        <p className="tiny muted" style={{ margin: '6px 0 0' }}>
          Estimated cost per call as configured: {eur(row.cost_per_call_eur)}.
        </p>
      </SectionCard>

      {row.acknowledged_at && (
        <SectionCard title="Terms acknowledgement" edge={false}>
          <p className="small" style={{ marginTop: 0 }}>
            Acknowledged {formatDate(row.acknowledged_at)}. Withdrawing it disables the
            source immediately and is itself recorded in the audit trail.
          </p>
          {revoking ? (
            <div className="row">
              <span className="small">Withdraw the acknowledgement and disable this source?</span>
              <div className="spacer" />
              <button className="btn btn-sm" onClick={() => setRevoking(false)}>
                Keep it
              </button>
              <button className="btn btn-sm btn-danger" disabled={busy} onClick={revoke}>
                Withdraw
              </button>
            </div>
          ) : (
            <button className="btn btn-sm btn-danger" onClick={() => setRevoking(true)}>
              Withdraw acknowledgement
            </button>
          )}
        </SectionCard>
      )}
    </Modal>
  )
}
