/**
 * The Resend relay (FR-325, FR-326).
 *
 * A one-way transactional relay from stepvda.com, and the screen is explicit
 * about what that costs: no mailbox means no reply detection, and delivery
 * news arrives as a signed webhook rather than in an inbox.
 *
 * **The API key does not exist yet.** That is a known state of this
 * installation, not a fault, so it is stated as one - with the exact steps to
 * take when the key is issued - rather than rendered as a broken backend.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, ErrorBox, SectionCard } from '../../components/ui'
import { copyText, domainOf, webhookUrl } from './vocabulary'

export default function ResendCard({ resend, isAdmin, onReload }) {
  const [secret, setSecret] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [saved, setSaved] = useState(false)
  const [copied, setCopied] = useState(false)

  const url = webhookUrl(resend?.webhook_path)
  const domain = domainOf(resend?.from_address)

  async function saveSecret(e) {
    e.preventDefault()
    setSaving(true)
    setError(null)
    try {
      await api.put('/mail/resend/webhook-secret', { secret })
      setSecret('')
      setSaved(true)
      onReload()
    } catch (err) {
      setError(err)
    } finally {
      setSaving(false)
    }
  }

  return (
    <SectionCard
      icon="send"
      title="Resend — relay from stepvda.com"
      phase="phase-0"
      actions={
        <Badge tone={resend?.configured ? 'ok' : 'warn'}>
          {resend?.configured ? 'API key present' : 'API key not available yet'}
        </Badge>
      }
    >
      {/* The product owner does not have the key yet. That is a known, expected
          state, so it is stated as one rather than rendered as a failure. */}
      {!resend?.configured && (
        <div className="alert alert-info">
          <div>
            <strong>Nothing is broken — the key simply does not exist yet.</strong>
            <div style={{ marginTop: 4 }}>{resend?.reason}</div>
            <div style={{ marginTop: 10 }}>
              <strong className="small">When you have it:</strong>
              <ol className="help-steps" style={{ marginTop: 4 }}>
                <li>Create an API key at resend.com/api-keys.</li>
                <li>
                  Verify <span className="mono">{domain || 'stepvda.com'}</span> as a sending
                  domain, so {resend?.from_address} is allowed to send.
                </li>
                <li>
                  Put <span className="mono">RESEND_API_KEY=re_…</span> in{' '}
                  <span className="mono">.env</span> and restart the API.
                </li>
                <li>Point a Resend webhook at the address below and set its signing secret here.</li>
              </ol>
              Until then, connect Gmail above. It is also the only backend that puts replies in
              your own inbox.
            </div>
          </div>
        </div>
      )}

      <dl className="mail-kv">
        <dt>From address</dt>
        <dd className="mono">{resend?.from_address || '–'}</dd>

        <dt>
          Domain verification
          <HelpTip term="domain_verification" />
        </dt>
        <dd>
          {resend?.configured ? (
            <>
              <Badge tone="info">Check at resend.com/domains</Badge>{' '}
              <span className="small muted">
                Dream Job does not read the domain’s state; Resend refuses the send if{' '}
                {domain} is not verified.
              </span>
            </>
          ) : (
            <>
              <Badge>Cannot be checked yet</Badge>{' '}
              <span className="small muted">
                Verification is confirmed at Resend, and there is no key here to ask with.
              </span>
            </>
          )}
        </dd>

        <dt>
          Webhook address
          <HelpTip
            title="Why a webhook"
            align="right"
          >
            Resend has no mailbox to poll, so delivery, bounce and complaint events are pushed
            here instead. This is the only way this backend learns that a message did not
            arrive.
          </HelpTip>
        </dt>
        <dd>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            <span className="mono">{url}</span>
            <button
              className="btn btn-sm btn-ghost"
              onClick={async () => setCopied(await copyText(url))}
            >
              <Icon name="copy" /> {copied ? 'Copied' : 'Copy'}
            </button>
          </div>
        </dd>

        <dt>
          Signing secret
          <HelpTip term="webhook_signing_secret" />
        </dt>
        <dd>
          <Badge tone={resend?.webhook_secret_set ? 'ok' : 'warn'}>
            {resend?.webhook_secret_set ? 'set' : 'not set'}
          </Badge>{' '}
          <span className="small muted">
            {resend?.webhook_secret_set
              ? 'Stored write-only; it is never read back to this screen.'
              : 'Without it every incoming webhook is rejected, so no bounce is ever recorded.'}
          </span>
        </dd>

        <dt>Replies</dt>
        <dd>
          <Badge tone="warn">Not detected</Badge>{' '}
          <span className="small muted">
            Reply-To points at you, so answers reach you directly — but they never pass through
            Dream Job. Record them on the <Link to="/responses">Responses</Link> screen.
          </span>
        </dd>
      </dl>

      {saved && <div className="alert alert-ok"><div>Signing secret stored.</div></div>}
      <ErrorBox error={error} />

      {isAdmin ? (
        <form onSubmit={saveSecret} style={{ marginTop: 14, maxWidth: 420 }}>
          <div className="field">
            <label>Set the Resend signing secret</label>
            <input
              type="password"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              placeholder="whsec_…"
              minLength={8}
              autoComplete="off"
            />
            <span className="hint">
              Copy it from the webhook you created at Resend. At least 8 characters.
            </span>
          </div>
          <button className="btn btn-sm" disabled={saving || secret.trim().length < 8}>
            {saving ? <span className="spinner" /> : 'Store the secret'}
          </button>
        </form>
      ) : (
        <p className="small muted" style={{ marginBottom: 0 }}>
          The signing secret is set by an administrator.
        </p>
      )}
    </SectionCard>
  )
}
