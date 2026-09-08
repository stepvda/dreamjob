/**
 * Sign in / register (NFR-202).
 *
 * Passwords plus TOTP. The server decides whether MFA is required and answers
 * a first-factor success with `mfa_required`, so this screen never has to know
 * in advance which accounts have MFA enrolled.
 */

import { useState } from 'react'

import { api } from '../api/client'

export default function SignIn({ onSignedIn }) {
  const [mode, setMode] = useState('signin')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [totp, setTotp] = useState('')
  const [mfaRequired, setMfaRequired] = useState(false)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      if (mode === 'register') {
        await api.post('/auth/register', {
          email,
          password,
          display_name: displayName || email.split('@')[0],
        })
      }
      const res = await api.post('/auth/login', {
        email,
        password,
        totp_code: totp || undefined,
      })
      if (res?.mfa_required) {
        setMfaRequired(true)
        setBusy(false)
        return
      }
      onSignedIn(await api.get('/auth/me'))
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  return (
    <div
      style={{
        minHeight: '100%',
        display: 'grid',
        placeItems: 'center',
        padding: 24,
        background: 'var(--bg)',
      }}
    >
      <div className="card" style={{ width: 380 }}>
        <div style={{ marginBottom: 20 }}>
          <h1 style={{ margin: 0, fontSize: 20, letterSpacing: '-0.02em' }}>Dream Job</h1>
          <p className="muted small" style={{ margin: '4px 0 0' }}>
            AI-assisted job discovery and application platform
          </p>
        </div>

        {error && <div className="alert alert-danger">{error}</div>}

        <form onSubmit={submit}>
          {mode === 'register' && (
            <div className="field">
              <label>Name</label>
              <input
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="Stephane van der Aa"
                autoComplete="name"
              />
            </div>
          )}

          <div className="field">
            <label>Email</label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoComplete="username"
              autoFocus
            />
          </div>

          <div className="field">
            <label>Password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoComplete={mode === 'register' ? 'new-password' : 'current-password'}
            />
            {mode === 'register' && (
              <span className="hint">
                At least 9 characters combining three of: lower case, upper case, digits,
                symbols. Your data is encrypted at rest with a key derived for your account
                alone.
              </span>
            )}
          </div>

          {mfaRequired && (
            <div className="field">
              <label>Authentication code</label>
              <input
                type="text"
                value={totp}
                onChange={(e) => setTotp(e.target.value)}
                inputMode="numeric"
                maxLength={6}
                placeholder="000000"
                autoFocus
              />
              <span className="hint">Six-digit code from your authenticator app.</span>
            </div>
          )}

          <button className="btn btn-primary btn-lg" style={{ width: '100%' }} disabled={busy}>
            {busy ? <span className="spinner" /> : mode === 'register' ? 'Create account' : 'Sign in'}
          </button>
        </form>

        <div style={{ marginTop: 14, textAlign: 'center' }}>
          <button
            className="btn btn-sm btn-ghost"
            onClick={() => {
              setMode(mode === 'signin' ? 'register' : 'signin')
              setError(null)
              setMfaRequired(false)
            }}
          >
            {mode === 'signin' ? 'Create an account' : 'I already have an account'}
          </button>
        </div>
      </div>
    </div>
  )
}
