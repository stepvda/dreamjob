/**
 * The briefing (FR-329) and the motivation document (FR-330), which share one
 * panel because they share the property that matters most about them.
 *
 * FR-321 makes both job-seeker material: they are never attached to the
 * introduction email and no recipient ever sees them. The backend enforces it
 * - the function that builds an attachment list can only return CV paths - but
 * a reader who is not told will assume a generated document goes out with the
 * message, so the notice comes before the content rather than after it.
 */

import { Caution, HelpTip } from '../../components/Help'
import { formatDate } from '../../components/ui'

import { NEVER_SENT_LABEL } from './shared'

export default function SeekerOnlyPanel({
  title,
  purpose,
  result,
  available,
  neverSent,
  onDownload,
  action,
  extra,
}) {
  const named = (neverSent || []).map((k) => NEVER_SENT_LABEL[k] || k)
  const usedLlm = result.briefing_used_llm || result.motivation_used_llm

  return (
    <div className="col" style={{ gap: 14 }}>
      {/* FR-321: stated first, because it is the assumption a reader arrives with. */}
      <Caution title="This document is never sent">
        {title} is prepared for you alone. It is not attached to the introduction email and no
        recipient ever sees it — only the tailored CV is ever attached.
        {named.length > 0 && <> Held for you on this package: {named.join(' and ')}.</>}
        <HelpTip term="seeker_only_document" />
      </Caution>

      <p className="section-intro" style={{ marginTop: 0 }}>
        {purpose}
      </p>

      <div className="apl-meta">
        <span className="muted">Template</span>
        <span>{result.briefing_template || result.motivation_template || '–'}</span>
        <span className="muted">Written</span>
        <span>
          {formatDate(result.generated_at)}
          <span className="small muted">
            ·{' '}
            {usedLlm
              ? 'written by the model from the collected data'
              : 'assembled from the collected data without the model'}
          </span>
        </span>
      </div>

      {extra}

      {result.notes?.length > 0 && (
        <ul className="small muted" style={{ margin: 0, paddingLeft: 18 }}>
          {result.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}

      <div className="row row-wrap">
        <button className="btn" disabled={!available} onClick={onDownload}>
          {available ? 'Download PDF' : 'Not generated'}
        </button>
        {action}
      </div>
    </div>
  )
}
