/**
 * Privacy tab — the do-not-disclose list (FR-106).
 *
 * A flag here is not a display preference. The flagged path is removed from
 * every generated CV, briefing and email, and it is stripped out of the
 * payload before anything is sent to the model provider (CR-410), so the
 * provider never sees it either. That is worth saying in plain words on the
 * screen rather than only in the glossary.
 */

import { useMemo, useState } from 'react'

import { api } from '../../api/client'
import { Caution, HelpTip } from '../../components/Help'
import { Badge, Empty, ErrorBox, Field, Loading, Modal, useFetch } from '../../components/ui'

/** The fields people most often mean, offered so nobody has to guess a path. */
const COMMON = [
  { path: 'contact.photo', label: 'Photo' },
  { path: 'contact.address', label: 'Home address' },
  { path: 'contact.phone', label: 'Phone number' },
  { path: 'contact.email', label: 'Personal email address' },
  { path: 'contact.date_of_birth', label: 'Date of birth' },
  { path: 'compensation.current', label: 'Current salary' },
]

/** Every leaf path in the profile, so the input can suggest real ones. */
function leafPaths(value, prefix = '', out = [], depth = 0) {
  if (out.length > 400 || depth > 4) return out
  if (Array.isArray(value)) {
    value.forEach((v, i) => leafPaths(v, `${prefix}.${i}`, out, depth + 1))
  } else if (value && typeof value === 'object') {
    Object.entries(value).forEach(([k, v]) => {
      if (k === '_meta') return
      leafPaths(v, prefix ? `${prefix}.${k}` : k, out, depth + 1)
    })
  } else if (prefix) {
    out.push(prefix)
  }
  return out
}

export default function PrivacyTab({ profile, onFlagsChanged }) {
  const flags = useFetch(() => api.get('/profile/disclosure-flags'), [])
  const [path, setPath] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState(null)
  const [removing, setRemoving] = useState(null)

  const suggestions = useMemo(
    () => leafPaths(profile?.sections || {}).slice(0, 200),
    [profile],
  )

  const flagged = new Set((flags.data || []).filter((f) => f.do_not_disclose).map((f) => f.field_path))

  async function add(fieldPath, why) {
    const target = (fieldPath || '').trim()
    if (!target) return
    setFailure(null)
    setBusy(true)
    try {
      await api.put('/profile/disclosure-flags', {
        field_path: target,
        do_not_disclose: true,
        reason: why?.trim() || null,
      })
      setPath('')
      setReason('')
      flags.reload()
      onFlagsChanged?.()
    } catch (err) {
      setFailure(err)
    } finally {
      setBusy(false)
    }
  }

  async function remove(flag) {
    setRemoving(null)
    setFailure(null)
    try {
      await api.del(
        `/profile/disclosure-flags?field_path=${encodeURIComponent(flag.field_path)}`,
      )
      flags.reload()
      onFlagsChanged?.()
    } catch (err) {
      setFailure(err)
    }
  }

  return (
    <div className="col" style={{ gap: 14 }}>
      {failure && <ErrorBox error={failure} onRetry={() => setFailure(null)} />}

      {/* FR-106 + CR-410: the guarantee, stated where the decision is made. */}
      <Caution title="What a do-not-disclose flag actually does">
        A flagged field is removed from every generated CV, briefing, motivation document and
        email, and it is stripped out of the request before anything reaches the AI provider —
        so the provider never receives it either.
        <HelpTip term="do_not_disclose" /> It stays in your profile here, where only you can see
        it, and it is still exported when you export your own data.
      </Caution>

      <div className="card">
        <div className="card-header">
          <h3>Commonly withheld</h3>
        </div>
        <p className="small muted" style={{ marginTop: 0 }}>
          A photo, a date of birth, a home address and a current salary are the four that most
          often cost people something and gain them nothing. One click flags them.
        </p>
        <div className="chips">
          {COMMON.map((c) => (
            <span
              key={c.path}
              className={`chip clickable${flagged.has(c.path) ? ' on' : ''}`}
              onClick={() => !flagged.has(c.path) && add(c.path, 'Flagged from the common list')}
              title={c.path}
            >
              {c.label}
              {flagged.has(c.path) ? ' ✓' : ''}
            </span>
          ))}
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Flag another field</h3>
          <HelpTip
            title="Field paths"
            align="right"
          >
            A path points into your profile the way the sections are stored:
            <span className="mono"> experience.0.description</span> is the description of your
            most recent role. The suggestions come from your own profile.
          </HelpTip>
        </div>
        <div className="entry-grid">
          <Field label="Field path" hint="Start typing — paths from your profile are suggested.">
            <input
              type="text"
              list="dnd-paths"
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder="contact.phone"
            />
            <datalist id="dnd-paths">
              {suggestions.map((p) => (
                <option key={p} value={p} />
              ))}
            </datalist>
          </Field>
          <Field label="Why" hint="For your own memory. It is never shown to anyone else.">
            <input
              type="text"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Not relevant, and not their business"
            />
          </Field>
        </div>
        <button
          className="btn btn-primary btn-sm"
          disabled={!path.trim() || busy}
          onClick={() => add(path, reason)}
        >
          {busy ? 'Saving…' : 'Never disclose this'}
        </button>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Withheld fields</h3>
          <span className="badge">{(flags.data || []).length}</span>
        </div>

        {flags.loading && <Loading rows={3} />}
        {flags.error && <ErrorBox error={flags.error} onRetry={flags.reload} />}

        {!flags.loading && !flags.error && (flags.data || []).length === 0 && (
          <Empty title="Nothing is withheld">
            Everything in your profile may be used in a generated document. If that is what you
            want, there is nothing to do here.
          </Empty>
        )}

        {(flags.data || []).length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Field</th>
                  <th>Reason</th>
                  <th>State</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {flags.data.map((f) => (
                  <tr key={f.field_path}>
                    <td>
                      <span className="field-path">{f.field_path}</span>
                    </td>
                    <td>{f.reason || <span className="muted tiny">no reason given</span>}</td>
                    <td>
                      {f.do_not_disclose ? (
                        <Badge tone="danger">never disclosed</Badge>
                      ) : (
                        <Badge>allowed</Badge>
                      )}
                    </td>
                    <td className="nowrap">
                      <button className="btn btn-sm btn-danger" onClick={() => setRemoving(f)}>
                        Allow again
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* House style: an outward-facing change confirms first — this one widens
          what may leave the system. */}
      {removing && (
        <Modal
          title="Allow this field to be used again?"
          onClose={() => setRemoving(null)}
          actions={
            <>
              <button className="btn" onClick={() => setRemoving(null)}>
                Keep it withheld
              </button>
              <button className="btn btn-danger" onClick={() => remove(removing)}>
                Allow it
              </button>
            </>
          }
        >
          <p>
            <span className="field-path">{removing.field_path}</span> becomes available to every
            document generated from now on, and to the AI provider. Documents already generated
            do not change.
          </p>
        </Modal>
      )}
    </div>
  )
}
