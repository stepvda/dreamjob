/**
 * The tailored CV — the one generated document that is actually attached.
 *
 * Everything on this tab is arranged around that single fact, because it is
 * the fact people get wrong. The briefing and the motivation document sit two
 * tabs away and look equally finished; only this one reaches a recipient. So
 * the panel says so at the top, the preview is the real PDF rather than a
 * description of it, and the download buttons are named for the file they
 * produce.
 *
 * The two controls do different amounts of work and are labelled accordingly.
 * Changing the template re-renders the same facts in another layout — no model
 * call, no new wording, nothing for the consistency check to reconsider.
 * Changing the language rewrites all four documents, which withdraws an
 * approval. Presenting them as two similar dropdowns without saying that is
 * how someone loses an afternoon's review to a stray click.
 */

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge } from '../../components/ui'

import DocumentPreview from './DocumentPreview'
import { LANGUAGES } from './shared'

export default function CvTab({
  detail,
  templates,
  photo,
  busy,
  previewUrl,
  onTemplate,
  onLanguage,
  onRegenerate,
  onDownload,
}) {
  const pkg = detail.package || {}
  const documents = detail.documents || {}
  const cv = pkg.generation?.cv || {}
  const cvTemplates = templates?.cv || []
  const editable = detail.state !== 'sent'
  const current = cvTemplates.find((t) => t.key === pkg.cv_template)

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="alert alert-info">
        <div>
          <strong>
            <Icon name="link" /> This is the file that gets attached.
          </strong>
          <div style={{ marginTop: 4 }}>
            The email carries exactly one document and it is this one, as a PDF. Nothing else on
            this screen is ever attached to anything.
          </div>
        </div>
      </div>

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
          {busy === 'tpl' && <span className="spinner" />}
          <span className="small muted">
            {current?.description || 'The same facts, laid out differently. No model call.'}
          </span>
        </span>

        <span className="muted">Language</span>
        <span>
          <select
            className="dir-inline-select"
            value={pkg.language || detail.email?.language || 'en'}
            disabled={!editable || busy === 'regen'}
            onChange={(e) => onLanguage(e.target.value)}
          >
            {LANGUAGES.map((l) => (
              <option key={l.value} value={l.value}>
                {l.label}
              </option>
            ))}
          </select>
          <span className="small muted">
            Changing this rewrites all four documents and withdraws any approval.
          </span>
        </span>

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

      <DocumentPreview
        url={previewUrl}
        label="The tailored CV"
        available={!!documents.cv_pdf?.available}
      />

      <div className="row row-wrap">
        <button
          className="btn"
          disabled={!documents.cv_pdf?.available}
          onClick={() => onDownload('cv_pdf', 'pdf')}
        >
          <Icon name="download" /> Download PDF
        </button>
        <button
          className="btn"
          disabled={!documents.cv_docx?.available}
          onClick={() => onDownload('cv_docx', 'docx')}
        >
          <Icon name="download" /> Download DOCX
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
          The PDF is what is attached; the DOCX is for you, if a form asks for one.
        </span>
      </div>
    </div>
  )
}
