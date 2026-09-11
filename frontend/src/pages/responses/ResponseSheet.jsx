/**
 * The response surface, opened from the pipeline board (FR-326, FR-421, FR-422).
 *
 * The board is a visual view of the five stages; this sheet is the one place a
 * response is recorded or corrected from inside it. It draws exactly the
 * recording form and the correction rows that "Responses received" draws, so
 * neither route invents its own sheet. Scope it to one card and the application
 * is already chosen; open it from the board and it lists what is awaiting a
 * response.
 *
 * It writes through the same endpoints as the Responses screen —
 * POST /learning/responses and PATCH /learning/responses/{id} — so a response
 * recorded from the board is an ordinary one and a correction moves every rate
 * with it.
 */

import { useMemo, useState } from 'react'

import { api } from '../../api/client'
import { ErrorBox, Modal, useFetch } from '../../components/ui'
import { ResponseCorrections, ResponseRecorder } from './ResponseSurface'
import { normaliseOutcome, outcomeLabel } from './vocabulary'

export default function ResponseSheet({ scope = null, onClose, onChanged }) {
  const responses = useFetch(() => api.get('/learning/responses'))
  const awaiting = useFetch(
    () => (scope ? Promise.resolve([]) : api.get('/learning/responses/awaiting')),
  )

  const [target, setTarget] = useState(scope || null)
  const [notice, setNotice] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [correcting, setCorrecting] = useState(null)
  const [modelReadings, setModelReadings] = useState({})

  const rows = useMemo(() => {
    const all = responses.data || []
    if (!scope) return all
    if (!scope.opportunity_id) return []
    return all.filter((r) => r.opportunity_id === scope.opportunity_id)
  }, [responses.data, scope])

  function scopedTarget(row) {
    return {
      dispatch_id: row.dispatch_id,
      opportunity_id: row.opportunity_id,
      company_name: row.company_name,
      opportunity_title: row.opportunity_title,
      opportunity_kind: row.opportunity_kind,
      subject: row.subject,
    }
  }

  function recorded(result) {
    setActionError(null)
    setNotice(
      result.stage_moved_to
        ? `Recorded. The application moved to “${result.stage_moved_to}” on the pipeline board.`
        : 'Recorded.',
    )
    const stated = normaliseOutcome(result.stated_outcome)
    const model = normaliseOutcome(result.classification?.classification)
    if (stated && model && stated !== model) {
      setNotice(
        `Recorded as “${outcomeLabel(stated)}”. The model read the same text as “${outcomeLabel(
          model,
        )}”; yours is what was kept.`,
      )
    }
    if (result.manual_response_id && result.classification) {
      setModelReadings((m) => ({ ...m, [result.manual_response_id]: result.classification }))
    }
    responses.reload()
    awaiting.reload()
    onChanged?.()
  }

  async function correct(row, stated) {
    setActionError(null)
    setCorrecting(row.manual_response_id)
    try {
      await api.patch(`/learning/responses/${row.manual_response_id}`, { stated_outcome: stated })
      setModelReadings((m) => {
        const next = { ...m }
        delete next[row.manual_response_id]
        return next
      })
      setNotice(
        `Corrected to “${outcomeLabel(stated)}”. Every rate this response is counted in has moved with it.`,
      )
      responses.reload()
      onChanged?.()
    } catch (err) {
      setActionError(err)
    } finally {
      setCorrecting(null)
    }
  }

  return (
    <Modal
      wide
      title={scope ? 'Record or correct a response' : 'Record or correct a response — choose an application'}
      onClose={onClose}
      actions={
        <button className="btn btn-ghost" onClick={onClose}>
          Close
        </button>
      }
    >
      {actionError && <ErrorBox error={actionError} />}
      {notice && (
        <div className="alert alert-ok">
          <div style={{ flex: 1 }}>{notice}</div>
          <button className="btn btn-sm btn-ghost" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </div>
      )}

      <ResponseRecorder
        awaiting={awaiting}
        target={target}
        onPick={setTarget}
        onCancel={scope ? onClose : () => setTarget(null)}
        onRecorded={recorded}
        cancelLabel={scope ? 'Close' : undefined}
        description={
          scope
            ? `Recording against ${scope.opportunity_title || scope.company_name || 'this application'}. The board stays the stage view; this is the response surface.`
            : 'Anything automatic detection cannot see — a call, a LinkedIn message, an ATS portal, or any reply to mail sent through Resend.'
        }
      />

      <ResponseCorrections
        responses={rows}
        modelReadings={modelReadings}
        correcting={correcting}
        onCorrect={correct}
        onAnother={(row) => setTarget(scopedTarget(row))}
        title={scope ? 'Responses recorded for this application' : 'Responses already recorded'}
      />
    </Modal>
  )
}
