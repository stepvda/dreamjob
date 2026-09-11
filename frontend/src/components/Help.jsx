/**
 * Help surfaces.
 *
 * Three levels, deliberately, because users need different things at
 * different moments:
 *
 *   <HelpTip>    a concept explained where it is used, one click away and
 *                dismissible - for "what does plausibility mean?"
 *   <HelpPanel>  what this screen is for and how to work through it, opened
 *                from the topbar or by pressing ? - for "what am I supposed
 *                to do here?"
 *   <FirstRun>   inline guidance on an empty screen, which is where a new
 *                user actually is - for "where do I start?"
 *
 * Nothing here needs a network call: help is static content shipped with the
 * application, so it works before the user has any data.
 */

import { useEffect, useRef, useState } from 'react'

import { GLOSSARY, pageHelp } from '../help/content'

/* --- Inline concept tip --------------------------------------------------- */

/**
 * A "?" affordance next to a label. Pass either a glossary `term` key or
 * explicit `title`/`children` content.
 */
export function HelpTip({ term, title, children, align = 'left' }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    if (!open) return
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

  const entry = term ? GLOSSARY[term] : null
  const heading = title || entry?.term || 'Help'
  const body = children || entry?.body

  if (!body) return null

  return (
    <span className="helptip" ref={ref}>
      {/* A span, not a button: the tip is placed inside buttons (the Home
          "Search again" control) and inside badges, and a nested <button> is
          invalid HTML that React warns about.  role/tabIndex/keydown keep it
          operable by keyboard. */}
      <span
        className="helptip-trigger"
        role="button"
        tabIndex={0}
        aria-label={`What is ${heading}?`}
        aria-expanded={open}
        onClick={(e) => {
          e.preventDefault()
          e.stopPropagation()
          setOpen((v) => !v)
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            e.stopPropagation()
            setOpen((v) => !v)
          }
        }}
      >
        ?
      </span>
      {open && (
        <span className={`helptip-pop helptip-${align}`} role="tooltip">
          <strong>{heading}</strong>
          <span>{body}</span>
        </span>
      )}
    </span>
  )
}

/* --- Per-screen guidance -------------------------------------------------- */

/**
 * The screen's own instructions, resolved from the route. Rendered as a drawer
 * so it never competes with the working area for space.
 */
export function HelpPanel({ pathname, open, onClose }) {
  const help = pageHelp(pathname)

  useEffect(() => {
    if (!open) return
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  return (
    <>
      <div className="help-scrim" onClick={onClose} />
      <aside className="help-drawer" role="dialog" aria-label="Help">
        <header className="help-drawer-head">
          <h3>{help?.title ?? 'Help'}</h3>
          <div className="spacer" />
          <button className="btn btn-sm btn-ghost" onClick={onClose} aria-label="Close help">
            ✕
          </button>
        </header>

        <div className="help-drawer-body">
          {!help && (
            <p className="muted">
              There is no specific guidance for this screen yet. Press <kbd>?</kbd> on any other
              screen, or use the “?” markers next to individual fields.
            </p>
          )}

          {help && (
            <>
              <p className="help-purpose">{help.purpose}</p>

              {help.steps?.length > 0 && (
                <section>
                  <h4>How to work through it</h4>
                  <ol className="help-steps">
                    {help.steps.map((s, i) => (
                      <li key={i}>{s}</li>
                    ))}
                  </ol>
                </section>
              )}

              {help.tips?.length > 0 && (
                <section>
                  <h4>Worth knowing</h4>
                  <ul className="help-tips">
                    {help.tips.map((t, i) => (
                      <li key={i}>{t}</li>
                    ))}
                  </ul>
                </section>
              )}

              {help.caution && (
                <div className="alert alert-warn" style={{ marginTop: 16 }}>
                  <div>
                    <strong>Before you rely on this.</strong> {help.caution}
                  </div>
                </div>
              )}
            </>
          )}

          <section className="help-glossary">
            <h4>Glossary</h4>
            <dl>
              {Object.entries(GLOSSARY).map(([k, v]) => (
                <div key={k}>
                  <dt>{v.term}</dt>
                  <dd>{v.body}</dd>
                </div>
              ))}
            </dl>
          </section>
        </div>

        <footer className="help-drawer-foot small muted">
          Press <kbd>?</kbd> anywhere to open this panel.
        </footer>
      </aside>
    </>
  )
}

/**
 * Topbar trigger. Also binds "?" globally, skipping the case where the user is
 * typing into a field.
 */
export function HelpButton({ onOpen }) {
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== '?') return
      const t = e.target
      const typing =
        t?.tagName === 'INPUT' || t?.tagName === 'TEXTAREA' || t?.isContentEditable
      if (typing) return
      e.preventDefault()
      onOpen()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onOpen])

  return (
    <button className="btn btn-sm" onClick={onOpen} title="Help for this screen (?)">
      <span aria-hidden>?</span> Help
    </button>
  )
}

/* --- Guidance in place ---------------------------------------------------- */

/**
 * A short instruction block placed at the top of a screen. Use it where the
 * screen needs a sentence of orientation that should not be hidden behind a
 * drawer - the first two or three uses of a screen are where people give up.
 */
export function ScreenIntro({ pathname, children }) {
  const help = pageHelp(pathname)
  const text = children || help?.purpose
  if (!text) return null
  return <p className="section-intro">{text}</p>
}

/**
 * Empty-state guidance. An empty screen is the moment a user most needs to be
 * told what to do, so the steps are shown in full rather than linked to.
 */
export function FirstRun({ pathname, title, action, children }) {
  const help = pageHelp(pathname)
  return (
    <div className="card first-run">
      <h3>{title ?? `Getting started with ${help?.title?.toLowerCase() ?? 'this screen'}`}</h3>
      {children ? (
        <p className="muted">{children}</p>
      ) : (
        help?.purpose && <p className="muted">{help.purpose}</p>
      )}
      {help?.steps?.length > 0 && (
        <ol className="help-steps">
          {help.steps.map((s, i) => (
            <li key={i}>{s}</li>
          ))}
        </ol>
      )}
      {action && <div style={{ marginTop: 14 }}>{action}</div>}
    </div>
  )
}

/**
 * A legal or risk notice that must be read, not skimmed - LinkedIn's terms
 * (CR-401), the non-EU transfer of profile data (CR-410). Rendering it through
 * one component keeps the wording and the acknowledgement behaviour uniform.
 */
export function Caution({ title, children, acknowledge, onAcknowledge, acknowledged }) {
  return (
    <div className={`alert ${acknowledged ? 'alert-ok' : 'alert-warn'}`}>
      <div style={{ flex: 1 }}>
        <strong>{title}</strong>
        <div style={{ marginTop: 4 }}>{children}</div>
        {acknowledge && !acknowledged && (
          <button className="btn btn-sm" style={{ marginTop: 10 }} onClick={onAcknowledge}>
            {acknowledge}
          </button>
        )}
        {acknowledged && (
          <div className="small" style={{ marginTop: 6 }}>
            Acknowledged.
          </div>
        )}
      </div>
    </div>
  )
}
