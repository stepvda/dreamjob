/**
 * The tailored CV: the one generated document that is actually attached.
 *
 * The API stores the CV as DOCX and PDF and returns no text for it, so what
 * can be previewed here is what is knowable *about* the document - the layout
 * it was rendered in, the language, whether a model tailored it or it came
 * straight from the profile, what the generator refused to write, and whether
 * the photograph went in (FR-106). The words themselves are in the file, and
 * the panel says so rather than implying it has shown them.
 *
 * Changing the template re-renders the same facts and costs nothing. Changing
 * the language is a regeneration of all four documents, and is labelled as one.
 */

import { HelpTip } from '../../components/Help'
import { Badge } from '../../components/ui'

import { LANGUAGES } from './shared'

export default function CvPanel({
  pkg,
  templates,
  photo,
  editable,
  busy,
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
              onRegenerate(['cv', 'briefing', 'motivation', 'email'], { language: e.target.value })
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
          onClick={() => onDownload('cv_pdf', 'CV', 'pdf')}
        >
          Download PDF
        </button>
        <button
          className="btn"
          disabled={!pkg.documents?.cv_docx}
          onClick={() => onDownload('cv_docx', 'CV', 'docx')}
        >
          Download DOCX
        </button>
        {editable && (
          <button className="btn btn-ghost" disabled={busy === 'regen'} onClick={() => onRegenerate(['cv'])}>
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
