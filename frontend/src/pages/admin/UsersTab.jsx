/**
 * User management (FR-362, FR-101, NFR-202).
 *
 * The administration's view of the people on this installation, and the small
 * set of actions an operator needs when an account has to be handed on,
 * recovered, suspended or closed:
 *
 *   create · grant/revoke administrator · reset password · sign out
 *   suspend/restore · delete
 *
 * Two safety rules are enforced by the API, and the interface shows why a
 * control is unavailable rather than hiding it: an administrator cannot act on
 * their own account in a way that would end the session they are working in,
 * and the last remaining administrator cannot be demoted or suspended by
 * anyone. Everything else is one click with a confirmation.
 *
 * Nothing here reveals a password or a TOTP secret. A password that the
 * server generates is shown once, in the response, and never stored in clear.
 */

import { useEffect, useState } from 'react'

import { api } from '../../api/client'
import { Caution, HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import {
  Badge,
  Empty,
  ErrorBox,
  Field,
  Loading,
  Modal,
  SectionCard,
  useFetch,
} from '../../components/ui'
import { stamp } from './format'

export default function UsersTab() {
  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')
  const [creating, setCreating] = useState(false)
  const [cleanup, setCleanup] = useState(false)
  const [notice, setNotice] = useState(null)

  // Debounce so typing does not fire a request per keystroke.
  useEffect(() => {
    const t = setTimeout(() => setDebounced(query.trim()), 250)
    return () => clearTimeout(t)
  }, [query])

  const state = useFetch(
    () => api.get(`/admin/users${debounced ? `?q=${encodeURIComponent(debounced)}` : ''}`),
    [debounced],
  )

  if (state.loading) return <Loading rows={5} />
  if (state.error) return <ErrorBox error={state.error} onRetry={state.reload} />

  const { users = [], total = 0, administrators = 0, me } = state.data || {}

  return (
    <div className="stack">
      {notice && <SecretPanel notice={notice} onDismiss={() => setNotice(null)} />}

      <SectionCard
        icon="admin"
        title={
          <>
            Users
            <HelpTip title="Accounts on this installation">
              Each account has its own private space — profile, directives, applications and
              documents. The shared company knowledge base is common to all of them. An
              administrator can suspend, recover or remove an account, but never read its
              private data from here.
            </HelpTip>
          </>
        }
        phase="phase-0"
        actions={
          <>
            <span className="badge">
              {total} account{total === 1 ? '' : 's'}
            </span>
            <span className="badge badge-accent">
              {administrators} administrator{administrators === 1 ? '' : 's'}
            </span>
            <button className="btn btn-sm btn-primary" onClick={() => setCreating(true)}>
              <Icon name="plus" />
              New user
            </button>
            <button className="btn btn-sm" onClick={() => setCleanup(true)}>
              <Icon name="trash" />
              Clean up
            </button>
          </>
        }
      >
        <div className="row" style={{ marginBottom: 12 }}>
          <span className="icon-chip phase-chip">
            <Icon name="search" />
          </span>
          <input
            type="search"
            placeholder="Search by e-mail or name…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            style={{ flex: 1 }}
          />
        </div>

        {users.length === 0 ? (
          <Empty title={debounced ? 'No matching account' : 'No accounts'}>
            {debounced
              ? `Nothing matches “${debounced}”.`
              : 'Create the first account with “New user”.'}
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>User</th>
                  <th>Role</th>
                  <th>Status</th>
                  <th>
                    Sessions
                    <HelpTip title="Open sessions">
                      Signing out ends every session the account has open, on every browser,
                      immediately. Resetting a password and suspending an account both do the
                      same, because a revoked password should not leave a working tab behind.
                    </HelpTip>
                  </th>
                  <th>Last sign-in</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <UserRow
                    key={u.id}
                    user={u}
                    isMe={u.id === me}
                    isLastAdmin={u.is_admin && administrators <= 1}
                    onChanged={state.reload}
                    onSecret={setNotice}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </SectionCard>

      {creating && (
        <CreateUserModal
          onClose={() => setCreating(false)}
          onCreated={(result) => {
            setCreating(false)
            setNotice(result)
            state.reload()
          }}
        />
      )}

      {cleanup && (
        <CleanupModal
          onClose={() => setCleanup(false)}
          onDone={(result) => {
            setCleanup(false)
            setNotice({
              kind: 'purged',
              title: `Deleted ${result.deleted} account${result.deleted === 1 ? '' : 's'}`,
              detail:
                result.failed > 0
                  ? `${result.failed} could not be deleted and were left in place — see the server log.`
                  : 'Their private data is gone; the shared company knowledge base was kept.',
            })
            state.reload()
          }}
        />
      )}
    </div>
  )
}

/* --- One account ----------------------------------------------------------- */

function UserRow({ user, isMe, isLastAdmin, onChanged, onSecret }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [modal, setModal] = useState(null) // 'reset' | 'delete'

  async function run(fn) {
    setBusy(true)
    setError(null)
    try {
      await fn()
      onChanged()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  const lockedRole = isMe || isLastAdmin
  const roleTitle = isMe
    ? 'You cannot change your own administrator role here'
    : isLastAdmin
      ? 'This is the only administrator; the role cannot be removed'
      : undefined

  return (
    <tr className={user.disabled ? 'row-disabled' : undefined}>
      <td>
        <div className="row" style={{ gap: 8 }}>
          <span className="icon-chip" style={{ width: 26, height: 26, fontSize: 13 }}>
            <Icon name="profile" />
          </span>
          <span>
            <strong>{user.display_name || '—'}</strong>
            {isMe && (
              <span className="badge" style={{ marginLeft: 6 }}>
                you
              </span>
            )}
            <div className="tiny muted mono">{user.email}</div>
          </span>
        </div>
      </td>

      <td>
        {user.is_admin ? (
          <Badge tone="accent">
            <Icon name="lock" size={11} /> administrator
          </Badge>
        ) : (
          <Badge>user</Badge>
        )}
      </td>

      <td>
        <div className="row" style={{ gap: 5, flexWrap: 'wrap' }}>
          {user.disabled ? (
            <Badge tone="danger">
              <Icon name="error" size={11} /> suspended
            </Badge>
          ) : (
            <Badge tone="ok">
              <Icon name="check" size={11} /> active
            </Badge>
          )}
          {user.mfa_enrolled && <Badge tone="info">MFA</Badge>}
          {user.passwordless && <Badge tone="warn">no password</Badge>}
        </div>
      </td>

      <td className="num">{user.active_sessions || '–'}</td>
      <td className="small muted">{user.last_login_at ? stamp(user.last_login_at) : 'never'}</td>

      <td>
        <div className="row" style={{ gap: 6, justifyContent: 'flex-end', flexWrap: 'wrap' }}>
          <button
            className="btn btn-sm"
            disabled={busy || lockedRole}
            title={roleTitle}
            onClick={() => run(() => api.patch(`/admin/users/${user.id}`, { is_admin: !user.is_admin }))}
          >
            {user.is_admin ? 'Revoke admin' : 'Make admin'}
          </button>

          <button className="btn btn-sm" disabled={busy} onClick={() => setModal('reset')}>
            <Icon name="lock" />
            Reset password
          </button>

          <button
            className="btn btn-sm"
            disabled={busy || !user.active_sessions}
            title={user.active_sessions ? 'Sign out everywhere' : 'No open sessions'}
            onClick={() => run(() => api.post(`/admin/users/${user.id}/logout`, {}))}
          >
            <Icon name="x" />
            Sign out
          </button>

          <button
            className="btn btn-sm"
            disabled={busy || (isMe && !user.disabled)}
            title={isMe && !user.disabled ? 'You cannot suspend your own account' : undefined}
            onClick={() =>
              run(() => api.patch(`/admin/users/${user.id}`, { disabled: !user.disabled }))
            }
          >
            {user.disabled ? (
              <>
                <Icon name="play" />
                Restore
              </>
            ) : (
              <>
                <Icon name="pause" />
                Suspend
              </>
            )}
          </button>

          <button
            className="btn btn-sm btn-danger"
            disabled={busy || isMe || isLastAdmin}
            title={
              isMe
                ? 'You cannot delete your own account'
                : isLastAdmin
                  ? 'The only administrator cannot be deleted'
                  : `Delete ${user.email}`
            }
            onClick={() => setModal('delete')}
          >
            <Icon name="trash" />
            Delete
          </button>
        </div>

        {error && (
          <div className="small" style={{ color: 'var(--danger)', marginTop: 6 }}>
            {error.message}
          </div>
        )}
      </td>

      {modal === 'reset' && (
        <ResetPasswordModal
          user={user}
          onClose={() => setModal(null)}
          onDone={(result) => {
            setModal(null)
            onSecret(result)
            onChanged()
          }}
        />
      )}

      {modal === 'delete' && (
        <DeleteUserModal
          user={user}
          onClose={() => setModal(null)}
          onDone={() => {
            setModal(null)
            onChanged()
          }}
        />
      )}
    </tr>
  )
}

/* --- Bulk clean-up --------------------------------------------------------- */

/**
 * Deletes every account with no open session — the throwaway accounts an
 * end-to-end run leaves behind. Always previews first: the modal loads the
 * dry-run count and a sample, and deletion only happens after an explicit
 * second click.
 */
function CleanupModal({ onClose, onDone }) {
  const [excludeAdmins, setExcludeAdmins] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [preview, setPreview] = useState(null)

  async function loadPreview() {
    setBusy(true)
    setError(null)
    try {
      setPreview(
        await api.post('/admin/users/purge-without-sessions', {
          exclude_admins: excludeAdmins,
          dry_run: true,
        }),
      )
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  // Load the preview once the modal opens.
  useEffect(() => {
    loadPreview()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function run() {
    setBusy(true)
    setError(null)
    try {
      const result = await api.post('/admin/users/purge-without-sessions', {
        exclude_admins: excludeAdmins,
        dry_run: false,
      })
      onDone(result)
    } catch (err) {
      setError(err)
      setBusy(false)
    }
  }

  const count = preview?.candidates ?? 0

  return (
    <Modal
      title="Clean up unused accounts"
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-danger" disabled={busy || count === 0} onClick={run}>
            {busy ? <span className="spinner" /> : `Delete ${count} account${count === 1 ? '' : 's'}`}
          </button>
        </>
      }
    >
      {error && <ErrorBox error={error} />}

      <p className="small muted" style={{ marginTop: 0 }}>
        Accounts with no open session are removed, along with everything private in them.
        The shared company knowledge base is kept, because it belongs to no one.
      </p>

      <label className="checkline" style={{ marginBottom: 12 }}>
        <input
          type="checkbox"
          checked={excludeAdmins}
          onChange={(e) => {
            setExcludeAdmins(e.target.checked)
            setTimeout(loadPreview, 0)
          }}
        />
        Keep administrators
        <HelpTip title="Keep administrators">
          A dormant administrator is legitimate to keep, and removing the last one would
          lock the installation out of its own administration. Your own account is never a
          candidate either.
        </HelpTip>
      </label>

      {busy && !preview ? (
        <Loading rows={2} />
      ) : (
        <>
          <div className={`alert ${count ? 'alert-warn' : 'alert-ok'}`}>
            <Icon name={count ? 'warning' : 'success'} />
            <div>
              <strong>
                {count
                  ? `${count} account${count === 1 ? '' : 's'} will be deleted`
                  : 'Nothing to clean up'}
              </strong>
              {count > 0 && (
                <div className="small" style={{ marginTop: 4 }}>
                  They have never signed in, or their session has expired.
                </div>
              )}
            </div>
          </div>

          {preview?.sample?.length > 0 && (
            <div>
              <div className="tiny muted" style={{ marginBottom: 4 }}>
                First {preview.sample.length} of {count}:
              </div>
              <div
                className="mono tiny"
                style={{
                  maxHeight: 160,
                  overflowY: 'auto',
                  background: 'var(--surface-sunk)',
                  borderRadius: 'var(--radius-sm)',
                  padding: '8px 10px',
                  lineHeight: 1.7,
                }}
              >
                {preview.sample.map((email) => (
                  <div key={email}>{email}</div>
                ))}
              </div>
            </div>
          )}

          {count > 0 && (
            <Caution title="This cannot be undone">
              Deleting these accounts erases their profiles, campaigns, applications and
              generated documents. There is no copy and no undo.
            </Caution>
          )}
        </>
      )}
    </Modal>
  )
}

/* --- Create ---------------------------------------------------------------- */

function CreateUserModal({ onClose, onCreated }) {
  const [form, setForm] = useState({
    email: '',
    display_name: '',
    password: '',
    is_admin: false,
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  function update(key, value) {
    setForm((f) => ({ ...f, [key]: value }))
  }

  async function submit() {
    setBusy(true)
    setError(null)
    try {
      const res = await api.post('/admin/users', {
        email: form.email.trim(),
        display_name: form.display_name.trim(),
        password: form.password.trim() || null,
        is_admin: form.is_admin,
      })
      onCreated({
        kind: 'created',
        title: `${res.user.display_name} was created`,
        email: res.user.email,
        password: res.password,
        generated: res.password_generated,
      })
    } catch (err) {
      setError(err)
      setBusy(false)
    }
  }

  const valid = form.email.includes('@') && form.display_name.trim().length > 0

  return (
    <Modal
      title="New user"
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={busy || !valid} onClick={submit}>
            {busy ? <span className="spinner" /> : 'Create user'}
          </button>
        </>
      }
    >
      {error && <ErrorBox error={error} />}
      <p className="small muted" style={{ marginTop: 0 }}>
        The account starts empty: its own private space, with no profile or campaign yet.
      </p>

      <Field label="E-mail">
        <input
          type="email"
          value={form.email}
          onChange={(e) => update('email', e.target.value)}
          placeholder="name@example.com"
          autoFocus
        />
      </Field>

      <Field label="Name">
        <input
          type="text"
          value={form.display_name}
          onChange={(e) => update('display_name', e.target.value)}
          placeholder="Full name"
        />
      </Field>

      <Field
        label="Password"
        hint="Leave empty to have a strong one generated and shown to you once."
      >
        <input
          type="text"
          value={form.password}
          onChange={(e) => update('password', e.target.value)}
          placeholder="Optional"
          autoComplete="new-password"
        />
      </Field>

      <label className="checkline">
        <input
          type="checkbox"
          checked={form.is_admin}
          onChange={(e) => update('is_admin', e.target.checked)}
        />
        Grant administrator access
        <HelpTip title="Administrator">
          Administrators can configure models and sources, read the audit trail and the AI
          call log, and manage other accounts. The AI call log can contain another job
          seeker&rsquo;s profile text, so this role is not for ordinary users.
        </HelpTip>
      </label>
    </Modal>
  )
}

/* --- Reset ----------------------------------------------------------------- */

function ResetPasswordModal({ user, onClose, onDone }) {
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function submit() {
    setBusy(true)
    setError(null)
    try {
      const res = await api.post(`/admin/users/${user.id}/reset-password`, {
        new_password: password.trim() || null,
      })
      onDone({
        kind: 'reset',
        title: `Password reset for ${user.display_name || user.email}`,
        email: user.email,
        password: res.password,
        generated: res.password_generated,
      })
    } catch (err) {
      setError(err)
      setBusy(false)
    }
  }

  return (
    <Modal
      title={`Reset password — ${user.display_name || user.email}`}
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={busy} onClick={submit}>
            {busy ? <span className="spinner" /> : 'Reset password'}
          </button>
        </>
      }
    >
      {error && <ErrorBox error={error} />}
      <Caution title="Every session for this account will be signed out">
        A password reset ends all of {user.display_name || user.email}&rsquo;s open sessions
        immediately, so a browser tab they already had open stops working.
      </Caution>

      <Field
        label="New password"
        hint="Leave empty to generate a strong one and show it to you once."
      >
        <input
          type="text"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Optional"
          autoComplete="new-password"
          autoFocus
        />
      </Field>
    </Modal>
  )
}

/* --- Delete ---------------------------------------------------------------- */

function DeleteUserModal({ user, onClose, onDone }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function submit() {
    setBusy(true)
    setError(null)
    try {
      await api.del(`/admin/users/${user.id}`)
      onDone()
    } catch (err) {
      setError(err)
      setBusy(false)
    }
  }

  return (
    <Modal
      title={`Delete ${user.display_name || user.email}`}
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-danger" disabled={busy} onClick={submit}>
            {busy ? <span className="spinner" /> : 'Delete permanently'}
          </button>
        </>
      }
    >
      {error && <ErrorBox error={error} />}
      <Caution title="This cannot be undone">
        Deleting {user.email} erases the account and everything private in it — profile,
        directives, applications, generated documents and the AI call log entries it owns.
        The shared company knowledge base is kept, because it carries no link back to any
        person. There is no copy and no undo.
      </Caution>
    </Modal>
  )
}

/* --- One-time password display --------------------------------------------- */

/**
 * A generated password is shown once and never stored in clear. This panel is
 * the only place it appears, so it says so plainly and offers a copy button.
 * It doubles as the "N accounts deleted" confirmation, which carries no
 * secret and simply reports what happened.
 */
function SecretPanel({ notice, onDismiss }) {
  async function copy() {
    try {
      await navigator.clipboard?.writeText(notice.password)
    } catch {
      /* clipboard is best-effort */
    }
  }

  const hasSecret = Boolean(notice.password)

  return (
    <div className={`alert ${hasSecret ? 'alert-warn' : 'alert-ok'}`}>
      <Icon name={hasSecret ? 'warning' : 'success'} />
      <div style={{ flex: 1 }}>
        <strong>{notice.title}</strong>
        {hasSecret ? (
          <>
            <div className="small" style={{ marginTop: 4 }}>
              {notice.generated ? 'A password was generated. ' : ''}
              Give this to <span className="mono">{notice.email}</span> now — it is shown
              once and is not stored anywhere in readable form.
            </div>
            <div className="row" style={{ gap: 8, marginTop: 8, alignItems: 'center' }}>
              <code
                className="mono"
                style={{
                  background: 'var(--surface)',
                  border: '1px solid var(--line-strong)',
                  borderRadius: 'var(--radius-sm)',
                  padding: '4px 10px',
                  fontSize: 13,
                }}
              >
                {notice.password}
              </code>
              <button className="btn btn-sm" onClick={copy}>
                <Icon name="copy" />
                Copy
              </button>
            </div>
          </>
        ) : (
          notice.detail && (
            <div className="small" style={{ marginTop: 4 }}>
              {notice.detail}
            </div>
          )
        )}
        <div className="row" style={{ marginTop: 8 }}>
          <button className="btn btn-sm btn-ghost" onClick={onDismiss}>
            Dismiss
          </button>
        </div>
      </div>
    </div>
  )
}
