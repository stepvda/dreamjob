/**
 * The AI call log, its retention sweep, and the data-subject rights
 * (FR-364, FR-108, NFR-301).
 *
 * Two things belong together here even though they are different requirements:
 * both are about text the product holds about a person. The call log is the
 * operator's window into why something was written the way it was; the
 * retention sweep and erasure are the promises that neither the log nor the
 * account is kept forever. Both are irreversible, so both confirm.
 *
 * A deliberate limit of the API is made visible rather than papered over: the
 * export and erasure routes act on the *signed-in* account only. There is no
 * administrator route that erases somebody else, which is the correct shape
 * for a data-subject right, and the accounts table says so.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { Caution, HelpTip } from '../../components/Help'
import {
  Badge,
  ErrorBox,
  Field,
  Loading,
  Modal,
  SectionCard,
  useFetch,
} from '../../components/ui'
import CallLog from './CallLog'
import DataRights from './DataRights'
import { num, stamp } from './format'

export default function DataTab() {
  return (
    <div className="stack">
      <RetentionCard />
      <CallLog />
      <DataRights />
    </div>
  )
}

/* --- Retention and redaction (FR-364) -------------------------------------- */

function RetentionCard() {
  const state = useFetch(() => api.get('/admin/llm-calls-retention'), [])
  const [days, setDays] = useState('')
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [result, setResult] = useState(null)

  if (state.loading) return <Loading rows={3} />
  if (state.error) return <ErrorBox error={state.error} onRetry={state.reload} />

  const s = state.data
  const effectiveDays = days === '' ? s.retention_days : Number(days)

  async function saveRetention() {
    setBusy(true)
    setError(null)
    try {
      await api.put('/admin/llm-config', { log_retention_days: Number(days) })
      setDays('')
      state.reload()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  async function sweep() {
    setBusy(true)
    setError(null)
    try {
      setResult(await api.post('/admin/llm-calls/redact', { older_than_days: effectiveDays }))
      setConfirm(false)
      state.reload()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <SectionCard
      icon="trash"
      title={
        <>
          Retention and redaction
          <HelpTip term="log_retention" />
        </>
      }
      phase="phase-0"
      actions={
        s.pending_redaction > 0 ? (
          <Badge tone="warn">{num(s.pending_redaction)} past the cutoff</Badge>
        ) : (
          <Badge tone="ok">Nothing overdue</Badge>
        )
      }
    >
      {error && <ErrorBox error={error} />}

      <p className="small muted" style={{ marginTop: 0 }}>
        {num(s.total_calls)} calls are logged. Text older than{' '}
        <strong>{stamp(s.cutoff)}</strong> is due to be nulled; the tokens, cost, model and
        the record each call was about are kept, because the cost reports need them and
        they are not personal data.
      </p>

      <div className="row row-wrap">
        <Field label="Retention, days" hint="Between 1 and 3650.">
          <input
            type="number"
            min="1"
            max="3650"
            value={days === '' ? s.retention_days : days}
            onChange={(e) => setDays(e.target.value)}
            style={{ width: 120 }}
          />
        </Field>
        <button
          className="btn btn-sm"
          disabled={busy || days === '' || Number(days) === s.retention_days}
          onClick={saveRetention}
        >
          Save retention period
        </button>
        <div className="spacer" />
        <button
          className="btn btn-sm btn-danger"
          disabled={busy || !s.pending_redaction}
          onClick={() => setConfirm(true)}
        >
          Run the sweep now
        </button>
      </div>

      {result && (
        <div className="alert alert-ok">
          <div>
            Redacted {num(result.redacted)} of {num(result.pending)} calls older than{' '}
            {stamp(result.cutoff)} ({result.retention_days} days).
          </div>
        </div>
      )}

      {confirm && (
        <Modal
          title="Run the redaction sweep"
          onClose={() => setConfirm(false)}
          actions={
            <>
              <button className="btn" onClick={() => setConfirm(false)}>
                Cancel
              </button>
              <button className="btn btn-danger" disabled={busy} onClick={sweep}>
                {busy ? <span className="spinner" /> : 'Redact permanently'}
              </button>
            </>
          }
        >
          <Caution title="This destroys text rather than archiving it">
            {num(s.pending_redaction)} calls older than {effectiveDays} days will have their
            prompt and response text set to null. There is no copy and no undo. Counters,
            model and the linked record survive, so the dashboards and cost reports are
            unaffected.
          </Caution>
        </Modal>
      )}
    </SectionCard>
  )
}
