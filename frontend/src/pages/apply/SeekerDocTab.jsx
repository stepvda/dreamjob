/**
 * The briefing (FR-329) and the motivation document (FR-330).
 *
 * One component for both, because the only thing that differs is the subject
 * matter and the single most important thing about them is identical: they are
 * prepared for the job seeker and are never attached to anything. That claim
 * is not a promise this screen makes on its own — the composer refuses these
 * two by path on the way to a message, so a CV column mistakenly pointed at
 * one of them is refused rather than sent — and the banner says so in those
 * terms, because "we would never" is worth less than "here is what stops it".
 *
 * The briefing is the document to reread before an interview, so it can be
 * refreshed on its own long after the application went out; the motivation
 * document is the argument you make to yourself before you make it to them.
 */

import { Caution, HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'

import DocumentPreview from './DocumentPreview'

const COPY = {
  briefing: {
    title: 'Company and job briefing',
    intro:
      'Everything worth knowing about this company and this role before you talk to them: what they do, how they are doing, who you would be joining, and what the posting is really asking for.',
    when: 'Reread it the morning of an interview. It can be regenerated on its own at any time, including long after the application went out, because the company will have moved on.',
    icon: 'companies',
  },
  motivation: {
    title: 'Motivation and fit',
    intro:
      'The case for why you fit this role, argued from your own profile — the evidence, the gaps, and the honest version of both.',
    when: 'It is the argument you make to yourself before you make it to them. The email borrows its best line; the document keeps the reasoning.',
    icon: 'target',
  },
}

export default function SeekerDocTab({
  kind,
  detail,
  busy,
  previewUrl,
  onRegenerate,
  onDownload,
}) {
  const copy = COPY[kind]
  const documents = detail.documents || {}
  const available = !!documents[kind]?.available
  const editable = detail.state !== 'sent' || kind === 'briefing'

  return (
    <div className="col" style={{ gap: 14 }}>
      <Caution title="For you, never sent.">
        This document is not attached to the email and cannot be. Only the tailored CV is
        attachable; the code that builds an outgoing message refuses these two by path, so there
        is no configuration or mistake that turns this into something a recipient receives.
        <HelpTip term="seeker_only_document" />
      </Caution>

      <p className="section-intro" style={{ marginTop: 0 }}>
        <Icon name={copy.icon} /> {copy.intro}
      </p>

      <DocumentPreview url={previewUrl} label={copy.title} available={available} />

      <div className="row row-wrap">
        <button className="btn" disabled={!available} onClick={() => onDownload(kind, 'pdf')}>
          <Icon name="download" /> Download PDF
        </button>
        {editable && (
          <button
            className="btn btn-ghost"
            disabled={busy === 'regen'}
            onClick={() => onRegenerate({ parts: [kind] })}
          >
            {busy === 'regen' ? <span className="spinner" /> : `Regenerate the ${kind}`}
          </button>
        )}
      </div>

      <p className="small muted">{copy.when}</p>
    </div>
  )
}
