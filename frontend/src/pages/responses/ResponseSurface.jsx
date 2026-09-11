/**
 * The single response-recording/correction surface (FR-326, FR-422, FR-425).
 *
 * There used to be two: the "Responses received" screen had its own recording
 * form and its own correction control, and the pipeline board had a separate
 * reply sheet that could only re-run the classifier. A person correcting a
 * response therefore met a different control depending on which route they
 * started from.
 *
 * Both routes now render this module. It is presentational on purpose: the
 * page and the sheet each own their fetches and state, and this is what they
 * both draw. "Records a response" is `RecordForm`, "corrects a response" is
 * `CorrectOutcome` inside the same `ResponseRow` table the Responses screen
 * has always shown.
 *
 * Keeping the board as the visual stage view is the point of the split: the
 * board still shows the five stages, and this surface is what opens from it.
 */

import { HelpTip } from '../../components/Help'
import { Empty, ErrorBox, Loading } from '../../components/ui'
import { AwaitingPicker, RecordForm } from './RecordResponse'
import ResponseRow from './ResponseRow'

export const RECORD_BLURB =
  'Anything automatic detection cannot see — a call, a LinkedIn message, an ATS ' +
  'portal, or any reply to mail sent through Resend.'

/**
 * The recording half of the surface. On the Responses screen the picking list
 * is the live one from `GET /learning/responses/awaiting`; opened from a card
 * the application is already chosen, so `target` arrives set and the picker is
 * skipped.
 */
export function ResponseRecorder({
  awaiting,
  target,
  onPick,
  onCancel,
  onRecorded,
  description = RECORD_BLURB,
  cancelLabel,
}) {
  return (
    <div className="card">
      <div className="card-header">
        <h3>Record a response</h3>
        <div className="spacer" />
        <span className="small muted">{description}</span>
      </div>

      {target ? (
        <RecordForm
          target={target}
          onCancel={onCancel}
          onRecorded={onRecorded}
          cancelLabel={cancelLabel}
        />
      ) : (
        <>
          {awaiting?.loading && <Loading rows={3} />}
          {awaiting?.error && <ErrorBox error={awaiting.error} onRetry={awaiting.reload} />}
          {!awaiting?.loading &&
            !awaiting?.error &&
            ((awaiting?.data || []).length ? (
              <AwaitingPicker rows={awaiting.data} onPick={onPick} />
            ) : (
              <Empty title="Every sent application has a response recorded">
                Nothing is waiting. When the next application goes out it appears here, and a
                response that arrives anywhere but a polled mailbox is recorded from this list.
              </Empty>
            ))}
        </>
      )}
    </div>
  )
}

/**
 * The correction half: exactly the rows the Responses screen lists, so the
 * sheet opened from a card corrects a response with the same control and the
 * same wording. Nothing here is specific to one route.
 */
export function ResponseCorrections({
  responses = [],
  modelReadings = {},
  correcting,
  onCorrect,
  onAnother,
  title = 'Responses recorded for this application',
  emptyText = 'No response has been recorded against this application yet. Recording one here is the same form the Responses screen uses.',
}) {
  return (
    <div className="card" style={{ marginTop: 14 }}>
      <div className="card-header">
        <h3>{title}</h3>
        <HelpTip term="stated_outcome" />
        <div className="spacer" />
        {responses.length > 0 && <span className="small muted">{responses.length} responses</span>}
      </div>

      {responses.length === 0 ? (
        <p className="small muted" style={{ margin: 0 }}>
          {emptyText}
        </p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>
                  Arrived
                  <HelpTip term="response_channel" />
                </th>
                <th>Company and role</th>
                <th>
                  What it was
                  <HelpTip term="stated_outcome" />
                </th>
                <th>Correct it</th>
              </tr>
            </thead>
            <tbody>
              {responses.map((row) => (
                <ResponseRow
                  key={row.id}
                  row={row}
                  modelReading={modelReadings[row.manual_response_id]}
                  busy={correcting === row.manual_response_id}
                  onCorrect={onCorrect}
                  onAnother={onAnother}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export default ResponseRecorder
