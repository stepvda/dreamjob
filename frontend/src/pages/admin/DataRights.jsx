/**
 * Accounts, export and erasure (FR-108, NFR-301).
 *
 * Export and erasure act on the signed-in account, because that is where the
 * API puts them: GET /api/auth/me/export and DELETE /api/auth/me. There is
 * deliberately no administrator route that erases another person — a
 * data-subject right is exercised by the data subject — and the note under the
 * table says so rather than leaving an operator hunting for a missing button.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { Caution, HelpTip } from '../../components/Help'
import {
  Badge,
  ErrorBox,
  Field,
  Loading,
  Modal,
  SectionCard,
  useFetch,
} from '../../components/ui'
import { useSession } from '../../session'
import { stamp } from './format'

export default function DataRights() {
  const { session, setSession } = useSession()
  const seekers = useFetch(() => api.get('/admin/seekers'), [])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [erasing, setErasing] = useState(false)
  const [typed, setTyped] = useState('')

  async function exportAll() {
    setBusy(true)
    setError(null)
    try {
      // NFR-301: every private row held for this job seeker, as JSON.
      await api.download('/auth/me/export', `dreamjob-export-${session.email}.json`)
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  async function erase() {
    setBusy(true)
    setError(null)
    try {
      await api.del('/auth/me')
      // The session cookie is gone; drop the client-side session so the shell
      // falls back to sign-in rather than 401-ing on the next request.
      setSession(null)
    } catch (err) {
      setError(err)
      setBusy(false)
    }
  }

  async function setAdmin(row, value) {
    setBusy(true)
    setError(null)
    try {
      await api.post(`/admin/seekers/${row.id}/admin`, { is_admin: value })
      seekers.reload()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <SectionCard
      icon="lock"
      title={
        <>
          Accounts and data-subject rights
          <HelpTip term="right_to_erasure" />
        </>
      }
      phase="phase-0"
    >
      {error && <ErrorBox error={error} />}

      <div className="row row-wrap" style={{ marginBottom: 14 }}>
        <button className="btn" disabled={busy} onClick={exportAll}>
          Export everything held about me
        </button>
        <button className="btn btn-danger" disabled={busy} onClick={() => setErasing(true)}>
          Erase my account
        </button>
        <span className="small muted">
          Both act on <strong>{session.email}</strong> — the account you are signed in as.
        </span>
      </div>

      {seekers.loading ? (
        <Loading rows={3} />
      ) : seekers.error ? (
        <ErrorBox error={seekers.error} onRetry={seekers.reload} />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Account</th>
                <th>Registered</th>
                <th>MFA</th>
                <th>Role</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(seekers.data || []).map((s) => (
                <tr key={s.id}>
                  <td>
                    <div style={{ fontWeight: 600 }}>
                      {s.display_name}
                      {s.id === session.id && (
                        <span style={{ marginLeft: 6 }}>
                          <Badge tone="accent">you</Badge>
                        </span>
                      )}
                    </div>
                    <div className="small muted">{s.email}</div>
                  </td>
                  <td className="small nowrap">{stamp(s.created_at)}</td>
                  <td>{s.mfa_enrolled ? <Badge tone="ok">enrolled</Badge> : <Badge>none</Badge>}</td>
                  <td>{s.is_admin ? <Badge tone="accent">administrator</Badge> : <Badge>job seeker</Badge>}</td>
                  <td style={{ textAlign: 'right' }}>
                    <button
                      className="btn btn-sm"
                      disabled={busy || (s.id === session.id && s.is_admin)}
                      onClick={() => setAdmin(s, !s.is_admin)}
                      title={
                        s.id === session.id && s.is_admin
                          ? 'The API refuses to remove your own administrator role.'
                          : undefined
                      }
                    >
                      {s.is_admin ? 'Remove administrator' : 'Make administrator'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="small muted" style={{ marginBottom: 0 }}>
        Erasure is a right the account holder exercises, so there is no route here that
        deletes somebody else — each person signs in and erases their own account. What an
        administrator can do is change roles, above.
      </p>

      {erasing && (
        <Modal
          title="Erase this account"
          onClose={() => setErasing(false)}
          actions={
            <>
              <button className="btn" onClick={() => setErasing(false)}>
                Cancel
              </button>
              <button
                className="btn btn-danger"
                disabled={busy || typed.trim() !== session.email}
                onClick={erase}
              >
                {busy ? <span className="spinner" /> : 'Erase permanently'}
              </button>
            </>
          }
        >
          {/* FR-108, NFR-301: irreversible, and it ends the session it runs in. */}
          <Caution title="This cannot be undone">
            Your account, profile, campaigns, opportunities, contacts, generated documents
            and uploaded files are deleted, and you are signed out immediately. The shared
            company knowledge base is kept — it describes companies rather than people —
            and an anonymous marker is left behind so the erasure itself stays provable.
          </Caution>
          <p className="small">
            Export your data first if you want a copy: the export is the only thing that
            survives this.
          </p>
          <Field
            label={`Type ${session.email} to confirm`}
            hint="Typed out in full, deliberately."
          >
            <input value={typed} onChange={(e) => setTyped(e.target.value)} />
          </Field>
        </Modal>
      )}
    </SectionCard>
  )
}
