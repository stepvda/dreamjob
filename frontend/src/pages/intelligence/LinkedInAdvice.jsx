/**
 * FR-443 - LinkedIn profile suggestions, as editable text.
 *
 * The requirement is suggestions, not automation, and the screen has to make
 * that impossible to miss: Dream Job never signs in to LinkedIn and never
 * changes a profile, so the only outward action available here is your own
 * clipboard. That is stated in a <Caution> above the suggestions rather than
 * in a footnote (CR-401).
 *
 * One thing to know about the data: GET /api/intelligence/linkedin returns the
 * stored row, where `skills` is `{list, in_demand_not_held}` and the corpus is
 * `corpus_summary`; POST returns Advice.as_dict(), where `skills` is a plain
 * list and the corpus is `corpus`. `normalise` flattens both into one shape so
 * the rendering does not have to know which call it came from.
 */

import { useEffect, useMemo, useRef, useState } from 'react'

import { Caution, HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, Empty, formatDate, formatPercent } from '../../components/ui'

const LANGUAGES = [
  { value: 'en', label: 'English' },
  { value: 'nl', label: 'Nederlands' },
  { value: 'fr', label: 'Français' },
]

/** The stored row and the compute response disagree; make them agree here. */
export function normalise(advice) {
  if (!advice) return null
  const skills = Array.isArray(advice.skills)
    ? { list: advice.skills, in_demand_not_held: [] }
    : advice.skills || {}
  return {
    ...advice,
    skillList: skills.list || [],
    skillsInDemand: skills.in_demand_not_held || [],
    headlines: advice.headlines || [],
    keywords: advice.keywords || [],
    featured: advice.featured || [],
    notes: advice.notes || [],
    corpus: advice.corpus || advice.corpus_summary || null,
  }
}

/** The whole suggestion set as one block of text, which is what gets stored. */
function assemble(advice) {
  if (!advice) return ''
  const parts = []
  if (advice.headlines.length) {
    parts.push(`HEADLINE\n${advice.headlines[0].text}`)
  }
  if (advice.about) parts.push(`ABOUT\n${advice.about}`)
  if (advice.skillList.length) {
    parts.push(`SKILLS\n${advice.skillList.map((s) => `- ${s.label || s.skill}`).join('\n')}`)
  }
  if (advice.keywords.length) {
    parts.push(`KEYWORDS\n${advice.keywords.map((k) => k.term).join(', ')}`)
  }
  if (advice.featured.length) {
    parts.push(
      `FEATURED\n${advice.featured.map((f) => `- ${f.title}${f.url ? ` — ${f.url}` : ''}`).join('\n')}`,
    )
  }
  return parts.join('\n\n')
}

function CopyButton({ text, label = 'Copy', small = true }) {
  const [done, setDone] = useState(false)
  const timer = useRef(null)

  useEffect(() => () => clearTimeout(timer.current), [])

  async function copy() {
    const value = String(text ?? '')
    try {
      await navigator.clipboard.writeText(value)
    } catch {
      /* Older browsers, and any context where the clipboard API is refused. */
      const area = document.createElement('textarea')
      area.value = value
      area.setAttribute('readonly', '')
      area.style.position = 'absolute'
      area.style.left = '-9999px'
      document.body.appendChild(area)
      area.select()
      try {
        document.execCommand('copy')
      } catch {
        /* Nothing more to try; the text is on screen and selectable. */
      }
      area.remove()
    }
    setDone(true)
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setDone(false), 1600)
  }

  return (
    <button className={`btn btn-ghost${small ? ' btn-sm' : ''}`} onClick={copy} disabled={!text}>
      <Icon name={done ? 'check' : 'copy'} /> {done ? 'Copied' : label}
    </button>
  )
}

export default function LinkedInAdvice({
  advice: raw,
  note,
  discretionMode,
  onGenerate,
  onSaveEdit,
  generating,
  saving,
  savedAt,
  language,
  setLanguage,
  onOpenGaps,
  hasCampaign,
}) {
  const advice = useMemo(() => normalise(raw), [raw])
  const assembled = useMemo(() => assemble(advice), [advice])

  const [draft, setDraft] = useState('')
  const [dirty, setDirty] = useState(false)

  /* Reseed when a different advice row arrives; keep the seeker's own edit if
     one is stored, because FR-443 says the draft is theirs. */
  useEffect(() => {
    setDraft(advice?.edited_text || assembled)
    setDirty(false)
  }, [advice?.id, advice?.edited_text, assembled])

  function append(text) {
    setDraft((d) => (d ? `${d}\n\n${text}` : text))
    setDirty(true)
  }

  return (
    <>
      {/* CR-401 / FR-443: the boundary of what this feature does, before it. */}
      <Caution title="Suggestions only — nothing here touches LinkedIn">
        {note ||
          'Dream Job never signs in to LinkedIn and never edits a profile. Everything below is text to copy and change in your own words.'}
      </Caution>

      {/* FR-385: applying any of this is a visible signal to a current employer. */}
      {discretionMode && (
        <Caution title="Discretion mode is on">
          Editing your headline, your About section or your skills notifies your network, and the
          “open to work” banner is visible to recruiters at your own employer. Keep this as a
          draft until you are ready — no “open to work” suggestion is made while discretion mode
          is on.
        </Caution>
      )}

      <div className="card">
        <div className="card-header">
          <Icon name="sparkle" />
          <h3>Profile suggestions</h3>
          <div className="spacer" />
          <select
            value={language}
            onChange={(e) => setLanguage(e.target.value)}
            className="dir-inline-select"
            aria-label="Language of the suggestions"
          >
            {LANGUAGES.map((l) => (
              <option key={l.value} value={l.value}>
                {l.label}
              </option>
            ))}
          </select>
          <button className="btn btn-primary btn-sm" disabled={generating || !hasCampaign} onClick={onGenerate}>
            {generating ? <span className="spinner" /> : advice ? 'Regenerate' : 'Generate suggestions'}
          </button>
        </div>

        {advice?.corpus && (
          <p className="small muted" style={{ margin: 0, lineHeight: 1.6 }}>
            Grounded in {advice.corpus.postings} posting
            {advice.corpus.postings === 1 ? '' : 's'} collected for this campaign
            <HelpTip term="corpus_keyword" />
            {advice.corpus.method ? ` — ${advice.corpus.method}.` : '.'}
          </p>
        )}

        {(advice?.notes || []).length > 0 && (
          <ul className="help-tips" style={{ marginTop: 10 }}>
            {advice.notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        )}

        {advice && (
          <div className="row row-wrap small muted" style={{ marginTop: 10 }}>
            {advice.created_at && <span>Generated {formatDate(advice.created_at)}</span>}
            {advice.generated_by && <Badge>{advice.generated_by}</Badge>}
            {advice.edited_at && <Badge tone="ok">You edited this {formatDate(advice.edited_at)}</Badge>}
          </div>
        )}
      </div>

      {!advice ? (
        <Empty title="No suggestions generated yet">
          {hasCampaign
            ? 'The keyword advice is counted across the vacancies this campaign actually collected, so it says what recruiters in your market type — not what generic profile advice says.'
            : 'The advice is grounded in a campaign’s collected vacancies. Create and run a campaign first.'}
        </Empty>
      ) : (
        <>
          {/* FR-443: editable text, with the copy control that is the only way
              any of it ever reaches LinkedIn. */}
          <div className="card">
            <div className="card-header">
              <Icon name="edit" />
              <h3>Your draft</h3>
              <div className="spacer" />
              <CopyButton text={draft} label="Copy all" />
              <button
                className="btn btn-sm"
                disabled={saving || !dirty || !advice.id}
                onClick={() => onSaveEdit(draft).then(() => setDirty(false))}
              >
                {saving ? <span className="spinner" /> : 'Save my edits'}
              </button>
            </div>
            <textarea
              className="statement-editor"
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value)
                setDirty(true)
              }}
              aria-label="Your LinkedIn draft"
            />
            <div className="row small muted" style={{ marginTop: 6 }}>
              <span>
                Saved on your account only. Copy it into LinkedIn yourself when you are ready.
              </span>
              <div className="spacer" />
              {savedAt && <span>Saved {formatDate(savedAt)}</span>}
              {dirty && <Badge tone="warn">Unsaved</Badge>}
            </div>
          </div>

          <div className="card">
            <div className="card-header">
              <h3>Headline</h3>
              <HelpTip title="What a headline claims">
                A headline naming a role you do not hold reads as a claim about your job title.
                Each option says what it is claiming so you can choose deliberately.
              </HelpTip>
            </div>
            {advice.headlines.length === 0 && <p className="muted small">None derived.</p>}
            {advice.headlines.map((h, i) => (
              <div className="entry-card" key={i}>
                <div className="row row-wrap">
                  <strong style={{ flex: 1, minWidth: 220 }}>{h.text}</strong>
                  <CopyButton text={h.text} />
                  <button className="btn btn-sm btn-ghost" onClick={() => append(`HEADLINE\n${h.text}`)}>
                    Add to draft
                  </button>
                </div>
                <div className="tiny muted" style={{ marginTop: 4 }}>
                  Based on {h.basis}. Claims: {h.claims}
                </div>
              </div>
            ))}
          </div>

          <div className="card">
            <div className="card-header">
              <h3>About</h3>
              <div className="spacer" />
              <CopyButton text={advice.about} />
              <button
                className="btn btn-sm btn-ghost"
                disabled={!advice.about}
                onClick={() => append(`ABOUT\n${advice.about}`)}
              >
                Add to draft
              </button>
            </div>
            {advice.about ? (
              <p className="qa-answer" style={{ marginBottom: 0 }}>
                {advice.about}
              </p>
            ) : (
              <p className="muted small">
                Nothing to draft from yet — the About section is assembled from your composite
                profile and your dream-job statement.
              </p>
            )}
          </div>

          <div className="card">
            <div className="card-header">
              <h3>
                Skills to list
                <HelpTip term="corpus_keyword" />
              </h3>
              <div className="spacer" />
              <CopyButton text={advice.skillList.map((s) => s.label || s.skill).join('\n')} />
            </div>
            {advice.skillList.length === 0 && (
              <p className="muted small">No skill in this corpus is both in demand and evidenced.</p>
            )}
            <div className="chips">
              {advice.skillList.map((s) => (
                <span className="chip on" key={s.skill} title={s.reason}>
                  {s.label || s.skill}
                  <span className="tiny muted" style={{ marginLeft: 5 }}>
                    {s.postings}
                  </span>
                </span>
              ))}
            </div>

            {advice.skillsInDemand.length > 0 && (
              <div className="alert alert-warn" style={{ marginTop: 12 }}>
                <div>
                  <strong>Do not list these yet.</strong> They are in demand in this corpus but
                  your profile does not evidence them — listing a skill you cannot demonstrate
                  fails at the first interview question. They belong to the gap analysis.
                  <div className="chips" style={{ marginTop: 8 }}>
                    {advice.skillsInDemand.map((s) => (
                      <span className="chip" key={s.skill}>
                        {s.label || s.skill}
                      </span>
                    ))}
                  </div>
                  <button className="btn btn-sm" style={{ marginTop: 10 }} onClick={onOpenGaps}>
                    Open the gap analysis
                  </button>
                </div>
              </div>
            )}
          </div>

          <div className="card">
            <div className="card-header">
              <h3>
                Keywords
                <HelpTip term="corpus_keyword" />
              </h3>
              <div className="spacer" />
              <CopyButton text={advice.keywords.map((k) => k.term).join(', ')} />
            </div>
            {advice.keywords.length === 0 ? (
              <p className="muted small">
                No term appears often enough across the collected postings to recommend. That is
                an empty answer rather than a generic one.
              </p>
            ) : (
              <div className="chips">
                {advice.keywords.map((k) => (
                  <span className={`chip${k.held ? ' on' : ''}`} key={k.term}>
                    {k.term}
                    <span className="tiny muted" style={{ marginLeft: 5 }}>
                      {formatPercent(k.share, 0)}
                    </span>
                  </span>
                ))}
              </div>
            )}
          </div>

          <div className="card">
            <div className="card-header">
              <h3>Featured content</h3>
              <div className="spacer" />
              <CopyButton
                text={advice.featured
                  .map((f) => `${f.title}${f.url ? ` — ${f.url}` : ''}`)
                  .join('\n')}
              />
            </div>
            {advice.featured.length === 0 ? (
              <p className="muted small">
                Nothing to feature yet. Evidence items with a link — a repository, a talk, a
                publication — are what this section is drawn from.
              </p>
            ) : (
              advice.featured.map((f, i) => (
                <div className="dir-listrow" key={i} style={i ? { marginTop: 8 } : undefined}>
                  <Icon name={f.url ? 'link' : 'document'} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="small">{f.title}</div>
                    <div className="tiny muted">{f.reason}</div>
                  </div>
                  {f.url && (
                    <a className="btn btn-sm btn-ghost" href={f.url} target="_blank" rel="noreferrer">
                      <Icon name="external" /> Open
                    </a>
                  )}
                </div>
              ))
            )}
          </div>
        </>
      )}
    </>
  )
}
