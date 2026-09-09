/**
 * Who is actually hiring: the badge, its evidence, and the seeker's answer to it.
 *
 * Requirements: FR-143 (the intermediary axis), FR-263 (the precedent - a thing
 * that is not what it looks like carries its own mark in every view), FR-282
 * (explainable), FR-341/FR-344 (a shared knowledge-base fact, a private
 * correction), NFR-205 (website text is untrusted), NFR-402 (every verdict
 * carries its evidence), CR-405 (advisory).
 *
 * Roughly one corpus row in five is posted by a staffing or recruitment agency
 * on behalf of an employer it does not name (`docs/Interim_Agencies_Proposal.md`
 * section 1: 17-20% overall, 33-37% of the Belgian EURES slice, 80-92% of
 * Actiris). Everything the product does well - the company profile, five years
 * of accounts, ability to pay, the values match, the letter about why you want
 * to work *there* - is then computed against the wrong organisation and looks
 * authoritative while being wrong. This is the mark that stops that happening
 * silently.
 *
 * Three states, and the reasoning behind each:
 *
 *   agency / board  the posting's author is not the employer. Hatched outline,
 *                   no hue: the absence of a colour is the point, because there
 *                   is no employer here to say anything about. Deliberately
 *                   *not* the speculative violet - a speculative opening is a
 *                   role nobody advertised, an agency posting is a real,
 *                   advertised vacancy with an unnamed employer, and conflating
 *                   two different axes on one colour would assert something
 *                   false (FR-263).
 *   unverified      we looked and could not tell, or nobody has looked yet.
 *                   Its own quiet state, never folded into "employer": the
 *                   sixty-vacancy agency whose site could not be read and the
 *                   direct employer nobody has researched are both here, and
 *                   guessing either way is the failure this whole feature
 *                   exists to prevent.
 *   direct          no badge. The normal case does not shout.
 *
 * `tag == null` renders nothing: nothing is known, so nothing is claimed. A
 * list row for a company with no verdict should carry the not-researched tag
 * the API already synthesises (`employer_role: 'unverified'`, `reason:
 * 'not_researched'`) rather than being left blank, which would read as
 * "employer".
 *
 * Clicking the badge opens the evidence: the rung, the verbatim quote, the
 * page it came from, the registered activity codes and the date (NFR-402). A
 * job seeker who thinks the product is wrong about their prospective employer
 * has to be able to see the reasoning in one click, and to answer it - the
 * "This is wrong" control is one more click from there.
 */

import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { Field, Modal, formatDate } from './ui'
import Icon from './Icon'

/* --- Vocabulary ------------------------------------------------------------
 *
 * The server sends localised strings for the badge and the disclosure note
 * (`employer_product.py`, proposal section 4.8) and English keys for
 * everything structural. These tables turn the keys into words; they are
 * exported because the employer screens under `pages/employers/` render the
 * same vocabulary and there should be exactly one of it.
 */

export const ROLE_DIRECT = 'direct'
export const ROLE_AGENCY = 'agency'
export const ROLE_BOARD = 'board'
export const ROLE_UNVERIFIED = 'unverified'

/** The fallback label per role, used when the server sends no localised one. */
export const ROLE_LABELS = {
  [ROLE_AGENCY]: 'Via an agency — employer not disclosed',
  [ROLE_BOARD]: 'Via a job board — employer not disclosed',
  [ROLE_UNVERIFIED]: 'Employer type not verified',
}

/** What the badge means, in one sentence, at the top of the popover. */
export const ROLE_MEANING = {
  [ROLE_AGENCY]:
    'This vacancy was posted by a staffing or recruitment agency for an employer it does not name. The job is real; the employer is unknown to us until the recruiter names it.',
  [ROLE_BOARD]:
    'This vacancy reached us through a job board rather than from the employer. The job is real; the employer is not the organisation that posted it.',
  [ROLE_UNVERIFIED]:
    'We have not established whether the organisation on this posting is the employer or an intermediary. It is shown as its own state rather than assumed either way.',
  [ROLE_DIRECT]:
    'The organisation on this posting is the employer: everything we say about the company is about the company you would work for.',
}

export const RUNG_WORDS = {
  kb: 'already on record',
  signals: 'the pattern of this employer’s own postings',
  registry: 'the company register',
  eures: 'how EURES files this employer',
  website: 'the company’s own website',
  manual: 'a person, confirmed for everyone',
}

export const METHOD_WORDS = {
  knowledge_base: 'read from the knowledge base',
  registry_nace: 'registered activity codes',
  website_llm: 'the company’s own pages, read and quoted',
  signals: 'measured over this employer’s postings',
  manual: 'entered by a person',
}

export const TIER_WORDS = {
  certain: 'certain',
  probable: 'probably an agency',
  possible: 'possible — not acted on either way',
  unknown: 'no signal either way',
  direct_likely: 'a direct employer, on the evidence',
}

export const SERVICE_MODEL_WORDS = {
  temp_agency: 'temporary employment agency',
  recruitment_selection: 'recruitment and selection',
  job_board: 'job board',
  payrolling: 'payrolling',
  consultancy_or_outsourcing: 'consultancy or outsourcing',
  product: 'makes a product',
  services: 'delivers services',
  public_body: 'public body',
  unknown: null,
}

/**
 * Why there is no answer, in words rather than in the column's value. Every
 * one of these is a different next move, which is what makes `cannot_tell` a
 * work item instead of a dead end (`Agency_Research_Design.md` section 7).
 */
export const REASON_WORDS = {
  not_researched: 'nobody has researched this employer yet',
  no_domain: 'no website is known for this employer',
  unreachable: 'the site did not answer',
  bot_wall: 'the site blocks automated reading',
  js_rendered: 'the site needs a browser to show any text',
  js_rendered_or_empty: 'the site needs a browser to show any text',
  parked: 'that domain is parked',
  robots: 'the site asks not to be read automatically',
  off_domain_redirect: 'that domain now belongs to somebody else',
  namesake_collision: 'another company shares this name',
  registry_ambiguous: 'two registered companies share this name',
  ambiguous_self_description: 'the site says both things',
  no_verifiable_evidence: 'nothing quotable was found',
  llm_failed: 'the classifier failed',
}

/**
 * What would settle it, per reason. The server's ladder carries the same map
 * in English only, on the argument that the wording belongs with the rest of
 * the presentation strings - so this is where it lives, and the server's
 * `next_step.label` is preferred whenever it sends one.
 */
export const REASON_NEXT_STEP = {
  not_researched: 'walk the ladder — the register answers in about four seconds',
  no_domain: 'add this employer’s website',
  unreachable: 'nothing to do: the site will be tried again',
  bot_wall: 'check the site in a browser',
  js_rendered: 'check the site in a browser',
  js_rendered_or_empty: 'check the site in a browser',
  parked: 'add the real website',
  robots: 'check the site in a browser',
  off_domain_redirect: 'add the real website',
  namesake_collision: 'say which company this is',
  registry_ambiguous: 'say which registered company this is',
  ambiguous_self_description: 'read the quotes and decide',
  no_verifiable_evidence: 'research again, or say what you know',
  llm_failed: 'research again',
}

/** The signal ids the detector fires, in words (proposal section 2.2). */
export const SIGNAL_WORDS = {
  R1: 'Company register',
  R2: 'How EURES files it',
  R3: 'Contract type offered',
  R4: 'Name',
  R4b: 'Name (ambiguous word)',
  T1: 'Speaks of a client',
  'T1′': 'Speaks of a client',
  T2: 'Anonymous voice',
  T3: 'No shared self-description',
  D1: 'Names itself in its adverts',
  D2: 'First-person employer voice',
  D3: 'Anti-agency disclaimer',
  D4: 'Public or non-profit body',
  D5: 'Posts on its own ATS board',
  D6: 'Registered, with no staffing code',
  website: 'The company’s own website',
  correction: 'A person',
}

/* --- The badge's own two tokens --------------------------------------------
 *
 * The design system's colours live in `styles/theme.css`, which this slice does
 * not own, so the two states the badge adds define their tokens here in the
 * same light/dark shape the theme uses. They are achromatic on purpose: every
 * hue in the product already means a phase, and violet already means
 * "speculative". What is missing on an agency row is the employer, so the badge
 * is drawn as an absence - a hatched outline - rather than as another colour.
 */

const STYLE_ID = 'employer-kind-style'

const CSS = `
.ek-anchor { position: relative; display: inline-flex; vertical-align: middle; }

.ek-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 7px;
  font-size: 11px;
  font-weight: 600;
  font-family: inherit;
  line-height: 1.55;
  border-radius: 4px;
  white-space: nowrap;
  color: var(--ink);
  background:
    repeating-linear-gradient(135deg,
      transparent 0 3px,
      var(--surface-sunk) 3px 7px);
  border: 1px solid var(--ink-4);
}

button.ek-badge { cursor: pointer; }
button.ek-badge:hover { border-color: var(--ink-3); }
.ek-badge:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }

/* Not a warning colour: an unverified employer is information, not a problem
   (Agency_Research_Design.md section 7.1). */
.ek-badge-unverified {
  background: transparent;
  border: 1px dashed var(--line-strong);
  color: var(--ink-3);
  font-weight: 550;
}
button.ek-badge-unverified:hover { border-color: var(--ink-4); color: var(--ink-2); }

.ek-pop {
  width: 360px;
  max-width: min(360px, calc(100vw - 32px));
  max-height: 60vh;
  overflow-y: auto;
  gap: 10px;
}

.ek-pop-head { display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }

/* Phrasing-level headings: the popover body is rendered inside a <span>, as
   the help tip's is, so every element in it stays phrasing content. */
.ek-h5 {
  display: block;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--ink-4);
  font-weight: 650;
}
.ek-sec { display: flex; flex-direction: column; gap: 6px; }
.ek-sec + .ek-sec { border-top: 1px solid var(--line); padding-top: 9px; }

.ek-item { display: flex; flex-direction: column; gap: 3px; }
.ek-item-head { display: flex; gap: 6px; align-items: baseline; }
.ek-item-detail { color: var(--ink); }

/* A quote is a fragment of a page the product did not write (NFR-205): it is
   shown as evidence, set apart so it cannot be misread as our own words. */
.ek-quote {
  margin: 0;
  padding: 5px 9px;
  border-left: 2px solid var(--line-strong);
  background: var(--surface-2);
  color: var(--ink-2);
  font-style: italic;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.ek-source { display: inline-flex; align-items: center; gap: 3px; font-size: 11.5px; }
.ek-meta { font-size: 11.5px; color: var(--ink-3); }
.ek-codes { display: flex; flex-wrap: wrap; gap: 4px; }
.ek-code {
  font-family: var(--mono);
  font-size: 11px;
  padding: 0 5px;
  border-radius: 3px;
  background: var(--surface-sunk);
  color: var(--ink-2);
  border: 1px solid var(--line);
}
.ek-pop-foot { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
`

function ensureStyles() {
  if (typeof document === 'undefined' || document.getElementById(STYLE_ID)) return
  const el = document.createElement('style')
  el.id = STYLE_ID
  el.textContent = CSS
  document.head.appendChild(el)
}

/* --- Reading a tag --------------------------------------------------------- */

/** The role, whatever shape the tag arrived in (list join or full payload). */
export function employerRole(tag) {
  if (!tag) return null
  const role = tag.employer_role || tag.role
  if (role) return role
  // A list row may carry only `kind`; derive the axis the same way the server
  // does rather than inventing a third mapping.
  if (tag.kind === 'agency') return tag.service_model === 'job_board' ? ROLE_BOARD : ROLE_AGENCY
  if (tag.kind === 'employer') return ROLE_DIRECT
  return ROLE_UNVERIFIED
}

export function isIntermediary(tag) {
  const role = employerRole(tag)
  return role === ROLE_AGENCY || role === ROLE_BOARD
}

/** True when nobody has walked the ladder for this company yet. */
export function isUnresearched(tag) {
  if (!tag) return true
  if (tag.researched === false) return true
  return tag.reason === 'not_researched'
}

export function reasonWords(reason) {
  if (!reason) return null
  return REASON_WORDS[reason] || String(reason).replace(/_/g, ' ')
}

export function serviceModelWords(model) {
  if (!model) return null
  return SERVICE_MODEL_WORDS[model] ?? String(model).replace(/_/g, ' ')
}

/**
 * The registered activity codes named in the register evidence.
 *
 * They are read back out of the evidence text rather than stored twice: the
 * register rung writes them into the sentence it quotes ("KBO NACE-BEL 2025
 * 78.200 (VAT) - temporary employment agency activities"), and a code the
 * evidence does not mention is a code we cannot show.
 */
export function naceCodesFrom(evidence) {
  const codes = new Set()
  for (const item of evidence || []) {
    if (!item || (item.signal !== 'R1' && item.method !== 'registry_nace')) continue
    for (const match of String(item.detail || '').match(/\b\d{2}\.\d{1,3}\b/g) || []) {
      codes.add(match)
    }
  }
  return [...codes]
}

function pct(value) {
  if (value == null || Number.isNaN(Number(value))) return null
  const n = Number(value)
  if (n < 0 || n > 1) return String(n)
  return `${Math.round(n * 100)}%`
}

function hostOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, '')
  } catch {
    return url
  }
}

/* --- The badge ------------------------------------------------------------- */

/**
 * The mark itself. Renders nothing for a direct employer and nothing at all
 * when there is no tag to render.
 *
 * `tag` may be either the full `/api/employers/{id}/kind` payload or the thin
 * join a list row carries (kind, employer_role, confidence, service_model,
 * tier, reason, summary). When the thin one is clicked and a `companyId` is
 * known, the full verdict with its evidence is fetched then - a list of fifty
 * rows should not pay for fifty evidence lists nobody opened.
 */
export default function EmployerKindBadge({
  tag,
  companyId,
  companyName,
  align = 'left',
  interactive = true,
  onChange,
}) {
  ensureStyles()
  const [open, setOpen] = useState(false)
  const [correcting, setCorrecting] = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const onDown = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false)
    }
    const onKey = (e) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const role = employerRole(tag)
  if (!tag || !role || role === ROLE_DIRECT) return null

  const label = tag.badge || ROLE_LABELS[role] || ROLE_LABELS[ROLE_UNVERIFIED]
  const unverified = role === ROLE_UNVERIFIED
  const id = companyId || tag.company_id
  const name = companyName || tag.company_name

  const className = `ek-badge${unverified ? ' ek-badge-unverified' : ''}`
  const icon = <Icon name={unverified ? 'help' : role === ROLE_BOARD ? 'browser' : 'link'} size={11} />

  // Where the badge is a label rather than a control - beside a heading that
  // already shows the evidence below it - it is not a disabled button, which
  // would read as "you may not press this" instead of "there is nothing here
  // to press".
  if (!interactive) {
    return (
      <span className="ek-anchor">
        <span className={className} title={label}>
          {icon}
          {label}
        </span>
      </span>
    )
  }

  return (
    <>
      <span className="ek-anchor" ref={ref}>
        <button
          type="button"
          className={className}
          onClick={(e) => {
            e.preventDefault()
            e.stopPropagation()
            setOpen((v) => !v)
          }}
          aria-expanded={open}
          aria-label={`${label}. Show why.`}
          title="Why does it say this?"
        >
          {icon}
          {label}
        </button>
        {open && (
          <span className={`helptip-pop ek-pop helptip-${align}`} role="dialog" aria-label={label}>
            <EmployerEvidence
              tag={tag}
              companyId={id}
              companyName={name}
              onCorrect={() => {
                setOpen(false)
                setCorrecting(true)
              }}
            />
          </span>
        )}
      </span>
      {correcting && (
        <EmployerCorrectionModal
          tag={tag}
          companyId={id}
          companyName={name}
          onClose={() => setCorrecting(false)}
          onSaved={(payload) => {
            setCorrecting(false)
            onChange?.(payload?.verdict || null)
          }}
        />
      )}
    </>
  )
}

/* --- The evidence ---------------------------------------------------------- */

/**
 * Why the verdict says what it says (NFR-402).
 *
 * The rung, the verbatim sentence, the page it is on, the registered activity
 * codes and the date it was established - and, when the answer is "we could
 * not tell", the reason and the cheapest next rung. Used inside the badge's
 * popover and, in full, on the employer panel.
 */
export function EmployerEvidence({ tag, companyId, companyName, onCorrect, compact = true }) {
  ensureStyles()
  const [full, setFull] = useState(tag?.evidence ? tag : null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState(null)
  const asked = useRef(null)

  const id = companyId || tag?.company_id
  const hasEvidence = Boolean(tag?.evidence || full)

  useEffect(() => {
    // A thin list-row tag carries no evidence. Fetch it the first time somebody
    // actually asks why, never before - and once per company, so a failed read
    // does not turn into a retry loop behind an open popover.
    if (hasEvidence || !id || asked.current === id) return undefined
    asked.current = id
    let live = true
    setLoading(true)
    api
      .get(`/employers/${id}/kind`)
      .then((data) => live && setFull(data))
      .catch((err) => live && setLoadError(err))
      .finally(() => live && setLoading(false))
    return () => {
      live = false
    }
  }, [id, hasEvidence])

  const view = full || tag || {}
  const role = employerRole(view)
  const evidence = view.evidence || []
  const codes = naceCodesFrom(evidence)
  const unresearched = isUnresearched(view)
  const anomalies = view.anomalies || []
  const correction = view.correction

  return (
    <>
      <span className="ek-pop-head">
        <strong>{view.badge || ROLE_LABELS[role] || 'Employer type'}</strong>
        {view.confidence != null && (
          <span className="ek-meta">confidence {pct(view.confidence)}</span>
        )}
      </span>

      <span className="ek-meta" style={{ lineHeight: 1.5 }}>
        {ROLE_MEANING[role]}
      </span>

      {view.summary && <span className="ek-item-detail">{view.summary}</span>}

      {(view.rung || view.method || view.established_at || view.tier) && (
        <span className="ek-sec">
          <span className="ek-h5">How this was established</span>
          <span className="ek-meta">
            {[
              view.rung ? RUNG_WORDS[view.rung] || view.rung : null,
              view.method ? METHOD_WORDS[view.method] || null : null,
              view.tier ? TIER_WORDS[view.tier] || view.tier : null,
              view.established_at ? `established ${formatDate(view.established_at)}` : null,
              !compact && view.expires_at ? `re-checked after ${formatDate(view.expires_at)}` : null,
            ]
              .filter(Boolean)
              .join(' · ')}
          </span>
        </span>
      )}

      {loading && <span className="ek-meta">Reading the evidence…</span>}
      {loadError && (
        <span className="ek-meta">
          The evidence could not be loaded ({loadError.message}). The verdict above is what the
          list was given.
        </span>
      )}

      {evidence.length > 0 && (
        <span className="ek-sec">
          <span className="ek-h5">Why we say this</span>
          {evidence.slice(0, compact ? 4 : 12).map((item, i) => (
            <span className="ek-item" key={i}>
              <span className="ek-item-head">
                {item.signal && (
                  <span className="ek-meta">{SIGNAL_WORDS[item.signal] || item.signal}</span>
                )}
                {item.measured != null && <span className="ek-meta">· {pct(item.measured)}</span>}
              </span>
              <span className="ek-item-detail">{item.detail}</span>
              {item.quote && <q className="ek-quote">{item.quote}</q>}
              {item.url && (
                <a
                  className="ek-source"
                  href={item.url}
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  <Icon name="external" size={11} />
                  {hostOf(item.url)}
                </a>
              )}
              {item.established_at && (
                <span className="ek-meta">read {formatDate(item.established_at)}</span>
              )}
            </span>
          ))}
          <span className="ek-meta">
            Quotes are copied from pages we did not write. They are shown to you as evidence and
            are never followed as instructions (NFR-205).
          </span>
        </span>
      )}

      {codes.length > 0 && (
        <span className="ek-sec">
          <span className="ek-h5">Registered activity codes</span>
          <span className="ek-codes">
            {codes.map((code) => (
              <span className="ek-code" key={code}>
                {code}
              </span>
            ))}
          </span>
        </span>
      )}

      {anomalies.length > 0 && (
        <span className="ek-sec">
          <span className="ek-h5">What the page tried</span>
          {anomalies.map((a, i) => (
            <span className="ek-meta" key={i}>
              {a}
            </span>
          ))}
          <span className="ek-meta">
            That is what the page attempted, not what the verdict says. The verdict above was
            reached from the quoted evidence and is unchanged.
          </span>
        </span>
      )}

      {view.conflict && (
        <span className="ek-sec">
          <span className="ek-h5">Disagreement</span>
          <span className="ek-meta">{view.conflict}</span>
        </span>
      )}

      {(role === ROLE_UNVERIFIED || unresearched) && (
        <span className="ek-sec">
          <span className="ek-h5">Why there is no answer</span>
          <span className="ek-item-detail">
            {reasonWords(view.reason) || 'the evidence did not settle it either way'}.
          </span>
          {(view.next_step?.label || REASON_NEXT_STEP[view.reason]) && (
            <span className="ek-meta">
              Cheapest next step: {view.next_step?.label || REASON_NEXT_STEP[view.reason]}.
            </span>
          )}
        </span>
      )}

      {correction && (
        <span className="ek-sec">
          <span className="ek-h5">{correction.is_mine ? 'Your correction' : 'A confirmed correction'}</span>
          <span className="ek-item-detail">
            {correction.kind === ROLE_AGENCY ? 'Marked as an agency' : 'Marked as a direct employer'}
            {correction.created_at ? ` on ${formatDate(correction.created_at)}` : ''}: {correction.note}
          </span>
          <span className="ek-meta">
            {correction.scope === 'shared'
              ? 'Confirmed, so it applies for everyone on this installation.'
              : 'It applies to your lists only, until an operator confirms it or a second job seeker independently says the same thing.'}
          </span>
        </span>
      )}

      <span className="ek-pop-foot">
        {onCorrect && (
          <button type="button" className="btn btn-sm" onClick={onCorrect}>
            This is wrong
          </button>
        )}
        {id && (
          <Link className="btn btn-sm btn-ghost" to={`/companies/${id}`}>
            {companyName || view.company_name || 'Open the employer'}
          </Link>
        )}
      </span>
    </>
  )
}

/* --- "This is wrong" -------------------------------------------------------
 *
 * The precedent is the rejection modal on the opportunity screen (FR-285): the
 * reason is required, because an unexplained correction is not evidence. What
 * this one adds is saying plainly what it does - and does not do - before it is
 * saved, since "correcting" something that looks like a fact usually implies
 * correcting it for everybody.
 */

export function EmployerCorrectionModal({ tag, companyId, companyName, onClose, onSaved }) {
  const current = employerRole(tag)
  const [kind, setKind] = useState(current === ROLE_AGENCY || current === ROLE_BOARD ? 'employer' : 'agency')
  const [note, setNote] = useState('')
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [result, setResult] = useState(null)

  const id = companyId || tag?.company_id
  const mine = tag?.correction?.is_mine

  async function save() {
    setBusy(true)
    setError(null)
    try {
      const payload = await api.post(`/employers/${id}/correct`, {
        kind,
        note: note.trim(),
        evidence_url: url.trim() || null,
      })
      setResult(payload)
      onSaved?.(payload)
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  async function withdraw() {
    setBusy(true)
    setError(null)
    try {
      const payload = await api.del(`/employers/${id}/correct`)
      onSaved?.(payload)
      onClose?.()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  if (result) {
    return (
      <Modal title="Recorded" onClose={onClose} actions={<button className="btn" onClick={onClose}>Close</button>}>
        <p className="section-intro">{result.note}</p>
        {result.conflicts_with_registry && (
          <div className="alert alert-warn">
            <div>
              <strong>The register says otherwise.</strong> Neither answer is applied over the
              other until an operator has looked at it: a register is occasionally stale and a
              correction is occasionally wrong, and neither should win silently.
            </div>
          </div>
        )}
        <p className="small muted">
          It applies to {result.applies_to === 'everyone' ? 'everyone on this installation' : 'your lists'} from now.
        </p>
      </Modal>
    )
  }

  return (
    <Modal
      title={`This is wrong: ${companyName || tag?.company_name || 'this employer'}`}
      onClose={onClose}
      actions={
        <>
          {mine && (
            <button className="btn btn-danger" disabled={busy} onClick={withdraw}>
              Withdraw my correction
            </button>
          )}
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={busy || note.trim().length < 3} onClick={save}>
            {busy ? <span className="spinner" /> : 'Record the correction'}
          </button>
        </>
      }
    >
      <p className="section-intro">
        What this does: it takes effect for you immediately — your ranked list, your scoring and
        anything generated from now on read your correction instead of the verdict. It does not
        change what anyone else sees. It is recorded as evidence for review, and becomes shared
        when an operator confirms it or when a second job seeker independently says the same
        thing.
      </p>

      <Field label="Which is it?">
        <div className="row row-wrap" style={{ gap: 14 }}>
          <label className="checkline">
            <input
              type="radio"
              name="employer-kind"
              checked={kind === 'agency'}
              onChange={() => setKind('agency')}
            />
            An agency: it posts jobs for employers it does not name
          </label>
          <label className="checkline">
            <input
              type="radio"
              name="employer-kind"
              checked={kind === 'employer'}
              onChange={() => setKind('employer')}
            />
            The employer: it hires for its own work
          </label>
        </div>
      </Field>

      <Field
        label="How do you know?"
        hint="Required. A sentence is enough — it is what an operator reads beside the machine's own quote."
      >
        <textarea
          value={note}
          autoFocus
          onChange={(e) => setNote(e.target.value)}
          placeholder="I worked there; they hire for themselves. / Their site says 'vind personeel'."
        />
      </Field>

      <Field label="A page that shows it (optional)" hint="Becomes an evidence item beside the machine's own.">
        <input
          type="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://…"
        />
      </Field>

      {error && (
        <div className="alert alert-danger">
          <div>{error.message}</div>
        </div>
      )}
    </Modal>
  )
}
