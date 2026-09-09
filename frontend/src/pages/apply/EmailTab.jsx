/**
 * The email: the one artefact a person writes back into (FR-323, FR-324).
 *
 * It is the AI-written brief motivation letter that refers to the CV, and the
 * only one of the four documents that leaves this machine as text rather than
 * as an attachment. So it is editable in place, and the panel says out loud
 * what editing costs: an approved package that is edited is a draft again and
 * the factual-consistency check runs over the new words. Discovering that
 * after pressing Save is how people lose an approval they thought they had.
 *
 * The regenerate control takes an instruction because "make it shorter" is a
 * different request from "start again" — the same package is rewritten, its
 * history keeps what you asked for, and nothing else in the package moves
 * unless you say so.
 */

import { useEffect, useState } from 'react'

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge } from '../../components/ui'

export default function EmailTab({ detail, busy, onSave, onRegenerate }) {
  const email = detail.email || {}
  const contact = detail.contact
  const speculative = detail.opportunity?.kind === 'speculative'

  const [subject, setSubject] = useState(email.subject || '')
  const [body, setBody] = useState(email.body || '')
  const [instruction, setInstruction] = useState('')

  // A new selection replaces the draft; an edit in progress on another job is
  // not something to carry across, and silently keeping it would be worse.
  useEffect(() => {
    setSubject(email.subject || '')
    setBody(email.body || '')
    setInstruction('')
  }, [detail.opportunity?.id, email.subject, email.body])

  const dirty = subject !== (email.subject || '') || body !== (email.body || '')
  const sent = detail.state === 'sent'

  if (!email.body && !email.subject) {
    return (
      <div className="empty" style={{ padding: '32px 20px' }}>
        <h3>No email has been written for this job yet</h3>
        <p>
          Generate the package and the letter appears here, written for this company and this role
          and referring to the CV that goes with it.
        </p>
      </div>
    )
  }

  return (
    <div className="col" style={{ gap: 14 }}>
      <p className="section-intro" style={{ marginTop: 0 }}>
        A short motivation letter that introduces you and points at the attached CV. It is the
        message the recipient reads; the CV is what they open next.
      </p>

      <div className="apl-meta">
        <span className="muted">
          To
          <HelpTip term="reachability" />
        </span>
        <span>
          {contact?.email ? (
            <>
              <strong>{contact.name || contact.email}</strong>
              {contact.role && <span className="muted">· {contact.role}</span>}
              <span className="mono small">{contact.email}</span>
              {contact.email_validation && <Badge>{contact.email_validation}</Badge>}
              {contact.objected && <Badge tone="danger">objected — never written to again</Badge>}
              {contact.email_source_method && (
                <span className="tiny muted">
                  address found by {String(contact.email_source_method).replace(/_/g, ' ')}
                  {contact.is_generic_mailbox ? ', a shared mailbox rather than a person' : ''}
                </span>
              )}
            </>
          ) : (
            <>
              <span className="muted">
                {contact?.reachability === 'unreachable'
                  ? 'Nobody: the whole ladder was walked and no address survived validation.'
                  : 'Nobody yet — contact discovery has not run for this company.'}
              </span>
              {contact?.unreachable_reason && (
                <span className="tiny muted">{contact.unreachable_reason}</span>
              )}
            </>
          )}
        </span>

        {speculative && (
          <>
            <span className="muted">
              Speculative
              <HelpTip term="speculative_opening" />
            </span>
            <span>
              <Badge tone="speculative">written as a spontaneous application</Badge>
              <span className="tiny muted">
                A sentence implying an advertised vacancy exists blocks dispatch (FR-323).
              </span>
            </span>
          </>
        )}
      </div>

      <div className="field">
        <label htmlFor="apply-subject">Subject</label>
        <input
          id="apply-subject"
          type="text"
          value={subject}
          disabled={sent || busy === 'save'}
          onChange={(e) => setSubject(e.target.value)}
        />
      </div>

      <div className="field">
        <label htmlFor="apply-body">
          Body
          <HelpTip term="application_package" />
        </label>
        <textarea
          id="apply-body"
          className="apl-editor"
          value={body}
          disabled={sent || busy === 'save'}
          onChange={(e) => setBody(e.target.value)}
        />
        <span className="hint">
          Plain text, on purpose. No HTML, no images, no tracking — those are the features a spam
          filter weighs, and a cold application in a junk folder has failed however well it reads.
        </span>
      </div>

      <div className="row row-wrap">
        <button
          className="btn btn-primary"
          disabled={!dirty || sent || busy === 'save'}
          onClick={() => onSave({ subject, body })}
        >
          {busy === 'save' ? <span className="spinner" /> : 'Save my edits'}
        </button>
        {dirty && (
          <button
            className="btn btn-ghost"
            disabled={busy === 'save'}
            onClick={() => {
              setSubject(email.subject || '')
              setBody(email.body || '')
            }}
          >
            Discard my edits
          </button>
        )}
        <span className="small muted">
          {sent
            ? 'This one has been sent, so its text is now a record rather than a draft.'
            : 'Saving re-opens an approved package as a draft and re-runs the checks over the new text.'}
        </span>
      </div>

      {!sent && (
        <div className="apl-regen">
          <div className="row" style={{ marginBottom: 6 }}>
            <Icon name="sparkle" />
            <strong>Ask for a different letter</strong>
            <HelpTip term="regeneration_instruction" />
          </div>
          <div className="field">
            <textarea
              rows={2}
              value={instruction}
              placeholder="Shorter. Lead with the platform work. Less formal."
              disabled={busy === 'regen'}
              onChange={(e) => setInstruction(e.target.value)}
            />
            <span className="hint">
              Optional. Without one the letter is simply written again from the same facts.
            </span>
          </div>
          <div className="row row-wrap">
            <button
              className="btn btn-phase"
              disabled={busy === 'regen'}
              onClick={() => onRegenerate({ instruction, parts: ['email'] })}
            >
              {busy === 'regen' ? <span className="spinner" /> : 'Rewrite the email'}
            </button>
            <button
              className="btn btn-ghost"
              disabled={busy === 'regen'}
              onClick={() =>
                onRegenerate({ instruction, parts: ['cv', 'briefing', 'motivation', 'email'] })
              }
            >
              Rewrite all four documents
            </button>
            <span className="small muted">
              The same package is rewritten in place, and what you asked for is kept in its
              history.
            </span>
          </div>
        </div>
      )}
    </div>
  )
}
