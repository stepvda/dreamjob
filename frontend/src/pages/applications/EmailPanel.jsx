/**
 * The introduction email: the only artefact of FR-321 whose text is editable
 * here, and the only one a recipient ever reads.
 *
 * FR-323 is the reason this panel opens with a notice rather than a field. An
 * email for a speculative opening is a spontaneous application; it must not
 * claim a vacancy exists, and the backend re-reads the body after every edit
 * for phrases that do. Those phrases are shown verbatim, because "your email
 * failed a check" is useless and "you wrote 'your advertised role'" is not.
 *
 * The draft lives in the parent so that switching to the Checks tab and back
 * does not throw away what the reader was in the middle of writing.
 */

import { Caution, HelpTip } from '../../components/Help'
import { Field } from '../../components/ui'

import { isSpeculative } from './shared'

export default function EmailPanel({ pkg, editable, busy, draft, onSave, onRegenerate }) {
  const { subject, setSubject, body, setBody, instructions, setInstructions } = draft
  const assertions = pkg.generation?.email?.vacancy_assertions || []
  const dirty = subject !== (pkg.email_subject || '') || body !== (pkg.email_body || '')

  return (
    <div className="col" style={{ gap: 14 }}>
      {/* FR-323: a spontaneous application must not imply a vacancy exists. */}
      {isSpeculative(pkg) && (
        <Caution title="This is written as a spontaneous application">
          No vacancy has been advertised for this role. The email introduces you and asks whether
          such a role could exist; it does not refer to an opening, and it must not be edited into
          one.
          <HelpTip term="speculative_opening" />
        </Caution>
      )}

      {assertions.length > 0 && (
        <div className="alert alert-danger">
          <div>
            <strong>This text claims a vacancy exists.</strong> FR-323 blocks dispatch until these
            phrases are gone:
            <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
              {assertions.map((phrase, i) => (
                <li key={i}>“{phrase}”</li>
              ))}
            </ul>
          </div>
        </div>
      )}

      <Field label="Subject">
        <input
          type="text"
          value={subject}
          disabled={!editable}
          onChange={(e) => setSubject(e.target.value)}
        />
      </Field>

      <Field
        label="Body"
        hint="Editing re-opens the package as a draft and re-runs the checks over your text — an approval you already gave is withdrawn."
      >
        <textarea
          className="apl-editor"
          value={body}
          disabled={!editable}
          onChange={(e) => setBody(e.target.value)}
        />
      </Field>

      <div className="row row-wrap">
        <button className="btn btn-primary" disabled={!dirty || busy === 'save'} onClick={onSave}>
          {busy === 'save' ? <span className="spinner" /> : 'Save the email'}
        </button>
        {dirty && (
          <button
            className="btn btn-ghost"
            onClick={() => {
              setSubject(pkg.email_subject || '')
              setBody(pkg.email_body || '')
            }}
          >
            Undo my edits
          </button>
        )}
        <div className="spacer" />
        {/* FR-321: what is attached, stated where the message is written. */}
        <span className="small muted">
          Attached: {pkg.attachments?.join(', ') || 'nothing yet'}
          <HelpTip term="seeker_only_document" />
        </span>
      </div>

      {editable && (
        <div className="apl-regen">
          <Field
            label={
              <>
                Regenerate with an instruction
                <HelpTip term="regeneration_instruction" />
              </>
            }
            hint="“Shorter.” “Lead with the platform work.” “Less formal.” The instruction steers the writing; it cannot add facts your profile does not contain."
          >
            <textarea
              rows={2}
              value={instructions}
              placeholder="Say what to change."
              onChange={(e) => setInstructions(e.target.value)}
            />
          </Field>
          <div className="row row-wrap">
            <button className="btn" disabled={busy === 'regen'} onClick={() => onRegenerate(['email'])}>
              {busy === 'regen' ? <span className="spinner" /> : 'Rewrite the email'}
            </button>
            <button
              className="btn"
              disabled={busy === 'regen'}
              onClick={() => onRegenerate(['cv', 'briefing', 'motivation', 'email'])}
            >
              Rewrite all four documents
            </button>
            <span className="small muted">
              Rewriting the CV or the email withdraws an approval you already gave.
            </span>
          </div>
        </div>
      )}
    </div>
  )
}
