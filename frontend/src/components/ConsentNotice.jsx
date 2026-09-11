/**
 * The single CR-410 acknowledgement.
 *
 * Profile data only reaches a model outside the EU after the job seeker has
 * said so, and there is exactly one such decision (`llm_transfer`). Both the
 * profile screen and the composite screen render this component rather than
 * each owning a copy of the wording and its own POST, so the text the seeker
 * agrees to, the request that records it, and the once-only behaviour all live
 * in one place.
 *
 * It prompts only while the consent is absent. Once granted the notice stays
 * visible - the acknowledgement remains reachable and the decision is not
 * hidden - but carries no button and posts nothing.
 */

import { useState } from 'react'

import { api } from '../api/client'
import { Caution } from './Help'
import { formatDate } from './ui'

// The wording the backend stores for `llm_transfer` (CR-410). It is only used
// before the initial GET /auth/consent has returned, or if that call failed;
// once loaded the authoritative `consent.text` is shown instead.
const FALLBACK_TEXT =
  'Profile, campaign and vacancy text is sent to the DeepSeek API, which processes it ' +
  'outside the European Union, to extract, score and generate content. Fields marked ' +
  '"do not disclose" (FR-106) and special categories of personal data (FR-127) are ' +
  'removed before any prompt is sent.'

// One backend consent, so one request. If two surfaces ever mount this notice
// together, the second joins the decision already in flight rather than
// recording a second one.
let inFlight = null

function recordTransfer(detail) {
  if (!inFlight) {
    inFlight = api
      .post('/auth/consent', { kind: 'llm_transfer', granted: true, detail })
      .finally(() => {
        inFlight = null
      })
  }
  return inFlight
}

export default function ConsentNotice({ consent, detail, onGranted, style }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  // The POST has recorded the decision the moment it resolves, even if the
  // owning screen has not finished re-fetching. Holding that result here keeps
  // the prompt gone and the confirmation visible without a second click.
  const [recorded, setRecorded] = useState(null)
  const granted = Boolean(consent?.granted) || Boolean(recorded?.granted)
  const decidedAt = consent?.decided_at || recorded?.decided_at

  async function acknowledge() {
    setBusy(true)
    setError(null)
    try {
      const row = await recordTransfer(detail)
      setRecorded({ granted: Boolean(row?.granted ?? true), decided_at: row?.granted_at })
      // Re-fetch in the owning screen so its own consent state, and any other
      // surface that reads it (enrichment settings), sees the decision too.
      await onGranted?.()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={style}>
      <Caution
        title="Your profile is sent outside the European Union"
        acknowledge={busy ? 'Recording your consent…' : 'I consent to this transfer'}
        acknowledged={granted}
        onAcknowledge={acknowledge}
      >
        {consent?.text || FALLBACK_TEXT} Until you consent, documents are read by the
        deterministic parser alone — which works, but reads an unusual layout less well.
        {error && (
          <div className="small" style={{ marginTop: 4 }}>
            We could not record that: {error}
          </div>
        )}
        {granted && decidedAt && (
          <div className="small muted" style={{ marginTop: 4 }}>
            Recorded {formatDate(decidedAt)}. You can withdraw it on the
            administration screen.
          </div>
        )}
      </Caution>
    </div>
  )
}
