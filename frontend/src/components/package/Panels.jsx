/**
 * The canonical review panels of an application package.
 *
 * One set, used by both the Applications screen and the Apply Browser. The
 * order of the tabs and the words on each panel are the same on both routes,
 * because they are the same five questions: what leaves, what it points at,
 * the two documents that are yours alone, and the checks that decide whether
 * any of it may go.
 *
 * The panels are presentation only. They are driven by the flat package
 * preview the API returns (`/applications/` and `/applications/{id}`, and the
 * Apply Browser's `detail.package`), and every action is a callback the host
 * screen supplies, so a route chooses its own endpoints without either panel
 * knowing which route it is on.
 */

import { Caution, HelpTip } from '../Help'
import Icon from '../Icon'
import { Badge, Empty, Field, formatDate } from '../ui'

import { ConsistencyBadge, LeakBadge, PdfViewer } from './shared'
import {
  LANGUAGES,
  NEVER_SENT_LABEL,
  SEVERITY_TONE,
  hardBlockers,
  isSpeculative,
  softBlockers,
} from '../packageStatus'

/* --- The introduction email (FR-323, FR-324) ------------------------------ */

/**
 * The introduction email: the only artefact whose text is editable, and the
 * only one a recipient ever reads.
 *
 * FR-323 is the reason this panel opens with a notice rather than a field. An
 * email for a speculative opening is a spontaneous application; it must not
 * claim a vacancy exists, and the backend re-reads the body after every edit
 * for phrases that do. Those phrases are shown verbatim, because "your email
 * failed a check" is useless and "you wrote 'your advertised role'" is not.
 *
 * The draft lives in the host so that switching to the Checks tab and back does
 * not throw away what the reader was in the middle of writing.
 */
export function EmailPanel({ pkg, editable, busy, draft, onSave, onRegenerate, advisories }) {
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

      {Array.isArray(advisories) && advisories.length > 0 && (
        <div className="alert alert-warn" role="status">
          <div>
            <strong>About this recipient</strong>
            <ul style={{ margin: '6px 0 0 18px' }}>
              {advisories.map((a) => (
                <li key={a.kind}>{a.detail}</li>
              ))}
            </ul>
          </div>
        </div>
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
            <button
              className="btn"
              disabled={busy === 'regen'}
              onClick={() => onRegenerate({ parts: ['email'], instructions })}
            >
              {busy === 'regen' ? <span className="spinner" /> : 'Rewrite the email'}
            </button>
            <button
              className="btn"
              disabled={busy === 'regen'}
              onClick={() =>
                onRegenerate({ parts: ['cv', 'briefing', 'motivation', 'email'], instructions })
              }
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

/* --- The tailored CV (FR-322, FR-331) ------------------------------------- */

/**
 * The tailored CV: the one generated document that is actually attached.
 *
 * Everything here is arranged around that single fact. The panel says so at the
 * top, the preview is the real PDF rather than a description of it, and the
 * download buttons are named for the file they produce.
 */
export function CvPanel({
  pkg,
  templates,
  photo,
  editable,
  busy,
  previewUrl,
  onTemplate,
  onRegenerate,
  onDownload,
}) {
  const cv = pkg.generation?.cv || {}
  const cvTemplates = templates?.cv || []

  return (
    <div className="col" style={{ gap: 14 }}>
      <p className="section-intro" style={{ marginTop: 0 }}>
        The only document attached to the email. It is your profile, tailored to this role — never
        more than your profile contains.
      </p>

      <PdfViewer url={previewUrl('cv_pdf')} label="CV" />

      <div className="apl-meta">
        <span className="muted">
          Template
          <HelpTip term="cv_template" />
        </span>
        <span>
          <select
            className="dir-inline-select"
            value={pkg.cv_template || ''}
            disabled={!editable || busy === 'tpl'}
            onChange={(e) => onTemplate(e.target.value)}
          >
            {cvTemplates.map((t) => (
              <option key={t.key} value={t.key}>
                {t.display_name}
              </option>
            ))}
          </select>
          <span className="small muted">
            {cvTemplates.find((t) => t.key === pkg.cv_template)?.description}
          </span>
        </span>

        <span className="muted">Language</span>
        <span>
          <select
            className="dir-inline-select"
            value={pkg.language || 'en'}
            disabled={!editable || busy === 'regen'}
            onChange={(e) =>
              onRegenerate({
                parts: ['cv', 'briefing', 'motivation', 'email'],
                language: e.target.value,
              })
            }
          >
            {LANGUAGES.map((l) => (
              <option key={l.value} value={l.value}>
                {l.label}
              </option>
            ))}
          </select>
          <span className="small muted">Changing this rewrites all four documents.</span>
        </span>

        {/* FR-106: the photograph is the field job seekers most often suppress. */}
        <span className="muted">
          Photograph
          <HelpTip term="do_not_disclose" />
        </span>
        <span>
          {photo.known ? (
            <Badge tone={photo.included ? 'info' : undefined}>
              {photo.included ? 'included in the CV' : 'not included'}
            </Badge>
          ) : (
            <span className="muted">unknown — your profile could not be read</span>
          )}
          <span className="small muted">{photo.reason}</span>
        </span>

        <span className="muted">Tailoring</span>
        <span>
          {cv.tailored_by_llm ? 'Rewritten for this role' : 'Straight from the profile'}
          {cv.dropped_positions?.length > 0 && (
            <span className="small muted">
              · {cv.dropped_positions.length} positions left out of this version
            </span>
          )}
        </span>
      </div>

      {cv.notes?.length > 0 && (
        <div className="alert alert-info">
          <div>
            <strong>What the generator would not write.</strong>
            <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
              {cv.notes.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          </div>
        </div>
      )}

      <div className="row row-wrap">
        <button
          className="btn"
          disabled={!pkg.documents?.cv_pdf}
          onClick={() => onDownload('cv_pdf', 'pdf')}
        >
          Download PDF
        </button>
        <button
          className="btn"
          disabled={!pkg.documents?.cv_docx}
          onClick={() => onDownload('cv_docx', 'docx')}
        >
          Download DOCX
        </button>
        {editable && (
          <button
            className="btn btn-ghost"
            disabled={busy === 'regen'}
            onClick={() => onRegenerate({ parts: ['cv'] })}
          >
            {busy === 'regen' ? <span className="spinner" /> : 'Regenerate the CV'}
          </button>
        )}
        <span className="small muted">
          The wording of the CV is only in the file. Open it before you approve.
        </span>
      </div>
    </div>
  )
}

/* --- The seeker-only documents (FR-329, FR-330) --------------------------- */

/**
 * The briefing (FR-329) and the motivation document (FR-330).
 *
 * One component for both, because the single most important thing about them is
 * identical: they are prepared for the job seeker and are never attached to
 * anything. On the Apply route, one of them can still be regenerated with an
 * instruction supplied by the other two.
 */
export function SeekerOnlyPanel({
  pkg,
  kind,
  title,
  purpose,
  extra,
  action,
  previewUrl,
  onDownload,
}) {
  const result = pkg.generation?.[kind] || {}
  const available = !!pkg.documents?.[kind]
  const named = (pkg.never_sent || []).map((k) => NEVER_SENT_LABEL[k] || k)
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

      <PdfViewer url={previewUrl(kind)} label={title || 'document'} />

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
        <button className="btn" disabled={!available} onClick={() => onDownload(kind, 'pdf')}>
          {available ? 'Download PDF' : 'Not generated'}
        </button>
        {action}
      </div>
    </div>
  )
}

/* --- The checks (FR-322, NFR-206) ----------------------------------------- */

/**
 * The Checks tab: the factual-consistency report (FR-322) and the leak scan
 * (NFR-206).
 *
 * This is the tab that decides whether anything may be sent, so it is written
 * to be read rather than skimmed: every claim the checker looked at, whether
 * the profile supports it, and what it was matched against. A `high` finding
 * fails the document, and the approval control on the header says so.
 */
export function ChecksPanel({ pkg, onRecheck, busy }) {
  const report = pkg.consistency_report || {}
  const findings = report.findings || []
  const leaks = report.leaks || []
  const summary = report.summary || {}
  const hard = hardBlockers(pkg)
  const soft = softBlockers(pkg)

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="row row-wrap">
        <span className="small muted">
          Factual consistency
          <HelpTip term="factual_consistency" />
        </span>
        <ConsistencyBadge status={pkg.consistency_status} />
        <span className="small muted" style={{ marginLeft: 10 }}>
          Leak scan
          <HelpTip term="leak_scan" />
        </span>
        <LeakBadge status={pkg.leak_scan_status} />
        <div className="spacer" />
        <button className="btn btn-sm" onClick={onRecheck} disabled={busy}>
          {busy ? <span className="spinner" /> : 'Re-run the checks'}
        </button>
      </div>

      {/* FR-322: a failed check blocks approval, and the reason is named here.
          Only a draft can be approved, so a decided package says nothing here. */}
      {pkg.status === 'draft' && hard.length > 0 && (
        <Caution title="This application cannot be approved">
          <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
            {hard.map((b, i) => (
              <li key={i}>{b.detail}</li>
            ))}
          </ul>
          Nothing on this list can be overridden. Correct the text, or regenerate, and re-run the
          checks.
        </Caution>
      )}

      {pkg.status === 'draft' && hard.length === 0 && soft.length > 0 && (
        <Caution title="Approval needs a recorded reason">
          {soft.map((b) => b.detail).join(' ')} You may still approve it, but FR-322 asks you to
          write down why the claim is defensible; the reason is kept in the audit trail with your
          name against it.
          <HelpTip term="approval_override" />
        </Caution>
      )}

      <div className="row row-wrap small muted">
        <span>{report.claims_checked ?? 0} claims checked</span>
        {report.parts?.length > 0 && <span>· across {report.parts.join(', ')}</span>}
        <span>· {summary.high ?? 0} blocking</span>
        <span>· {summary.medium ?? 0} to look at</span>
        <span>· {summary.low ?? 0} minor</span>
        {report.judge_ran ? (
          <span>· a reviewing model read it as a second signal</span>
        ) : (
          <span>· deterministic checks only</span>
        )}
        {report.judge_error && <Badge tone="warn">judge: {report.judge_error}</Badge>}
      </div>

      <FindingTable
        title="Claims in the documents"
        caption="Each claim the checker isolated, and what in your profile it was matched against."
        rows={findings}
        emptyTitle="Every claim traced back to your profile"
        emptyBody="Employers, titles, schools, dates, skills and figures in the generated documents were all found in the profile version this package was built from."
      />

      <FindingTable
        title="Leak scan"
        caption="Content in the documents that is not traceable to material you are entitled to (NFR-206)."
        rows={leaks}
        emptyTitle="Nothing unattributable"
        emptyBody="Every name, address, domain and figure in the generated documents comes from your own profile, or from the company and vacancy this application is for."
      />
    </div>
  )
}

function FindingTable({ title, caption, rows, emptyTitle, emptyBody }) {
  return (
    <section>
      <div className="row" style={{ marginBottom: 6 }}>
        <strong>{title}</strong>
      </div>
      <p className="small muted" style={{ margin: '0 0 8px' }}>
        {caption}
      </p>
      {rows.length === 0 ? (
        <Empty title={emptyTitle}>{emptyBody}</Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Severity</th>
                <th>Claim</th>
                <th>Where</th>
                <th>Supported by</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((f, i) => (
                <tr key={i}>
                  <td>
                    <Badge tone={SEVERITY_TONE[f.severity]}>{f.severity}</Badge>
                  </td>
                  <td>
                    <div>{f.claim}</div>
                    <div className="small muted" style={{ marginTop: 3 }}>
                      {f.detail}
                    </div>
                  </td>
                  <td className="small nowrap">
                    {f.locus}
                    <div className="muted">{f.kind}</div>
                  </td>
                  <td className="small">
                    {f.evidence ? (
                      <span>{f.evidence}</span>
                    ) : (
                      <span className="muted">Nothing in the profile matched it</span>
                    )}
                    <div className="muted" style={{ marginTop: 3 }}>
                      {SIGNAL_LABEL[f.signal] || f.signal}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

const SIGNAL_LABEL = {
  deterministic: 'Matched against the profile',
  judge: 'Flagged by the reviewing model',
}

/* --- A generated document, previewed in place ----------------------------- */

export function DocumentPreview({ url, label, available, height = 560, actions }) {
  if (!available) {
    return (
      <div className="empty" style={{ padding: '32px 20px' }}>
        <h3>{label} has not been generated</h3>
        <p>
          Generate the package for this job and the document appears here. Nothing is invented for
          a preview.
        </p>
        {actions}
      </div>
    )
  }

  return (
    <div className="col" style={{ gap: 8 }}>
      <div
        style={{
          border: '1px solid var(--line)',
          borderRadius: 'var(--radius)',
          overflow: 'hidden',
          background: 'var(--surface-sunk)',
        }}
      >
        <iframe
          src={`${url}#view=FitH`}
          title={label}
          style={{ width: '100%', height, border: 0, display: 'block' }}
        />
      </div>
      <div className="row row-wrap small muted">
        <Icon name="eye" />
        <span>
          This is the file itself, opened in your browser's PDF viewer. If it stays blank, your
          browser is set to download PDFs rather than display them — use the download button.
        </span>
        <div className="spacer" />
        {actions}
      </div>
    </div>
  )
}
