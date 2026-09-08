/**
 * The two dispatch backends (FR-325, NFR-204).
 *
 * They are not two ways of doing the same thing, and the screen says so
 * rather than presenting a pair of equivalent switches:
 *
 * - Gmail sends as the job seeker and, because the answers arrive in the job
 *   seeker's own inbox, it is the only backend that can detect replies and
 *   bounces at all (FR-326, section 2.4).
 * - Resend is a one-way relay from stepvda.com. It has no mailbox, so delivery
 *   news arrives as a webhook and replies are never seen by Dream Job.
 *
 * NFR-204 also asks that the granted access be visible and revocable from the
 * user interface. That is what the scope list, the expiry and the two
 * confirmed controls at the bottom of the Gmail card are.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge, ErrorBox, Modal, SectionCard } from '../../components/ui'
import ResendCard from './ResendCard'
import { formatWhen, isPast, scopeLabel, scopeName } from './vocabulary'

export default function Backends({
  status,
  onConnect,
  connecting,
  consentOpened,
  consentUrl,
  onReload,
  isAdmin,
}) {
  const backends = status?.backends || []
  const gmail = backends.find((b) => b.backend === 'gmail_oauth')
  const resend = backends.find((b) => b.backend === 'resend')
  const accounts = status?.accounts || []

  return (
    <div className="col" style={{ gap: 14 }}>
      <GmailCard
        gmail={gmail}
        accounts={accounts.filter((a) => a.backend === 'gmail_oauth')}
        onConnect={onConnect}
        connecting={connecting}
        consentOpened={consentOpened}
        consentUrl={consentUrl}
        onReload={onReload}
      />
      <ResendCard resend={resend} isAdmin={isAdmin} onReload={onReload} />
    </div>
  )
}

/* --- Gmail ---------------------------------------------------------------- */

function GmailCard({ gmail, accounts, onConnect, connecting, consentOpened, consentUrl, onReload }) {
  const [confirm, setConfirm] = useState(null) // { account, action: 'revoke' | 'forget' }
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [outcome, setOutcome] = useState(null)

  async function run() {
    if (!confirm) return
    setBusy(true)
    setError(null)
    try {
      if (confirm.action === 'revoke') {
        // NFR-204: revoke at Google as well as here. The backend wipes the
        // stored token either way and reports whether Google agreed, because a
        // half-revocation the user is not told about is the worst outcome.
        const res = await api.post(`/mail/accounts/${confirm.account.id}/revoke`)
        setOutcome(
          res?.revoked_at_google
            ? `The token for ${confirm.account.address} was revoked at Google and erased here.`
            : `The token for ${confirm.account.address} was erased here, but Google did not confirm the revocation${res?.error ? ` (${res.error})` : ''}. Finish it at myaccount.google.com → Security → Third-party access.`,
        )
      } else {
        await api.del(`/mail/accounts/${confirm.account.id}`)
        setOutcome(`${confirm.account.address} was disconnected and forgotten.`)
      }
      setConfirm(null)
      onReload()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  const connected = Boolean(gmail?.configured)
  const canAuthorize = Boolean(gmail?.authorize_path)

  return (
    <SectionCard
      icon="mailsetup"
      title="Gmail — your own mailbox"
      phase="phase-0"
      actions={
        <Badge tone={connected ? 'ok' : 'warn'}>{connected ? 'Connected' : 'Not connected'}</Badge>
      }
    >
      <p className="small muted" style={{ marginTop: 0 }}>
        Applications leave from your own address, land in your own Sent folder, and — because
        the answers come back to your inbox — this is the one backend that can detect replies
        and bounces for you (FR-326). Everything else has to be recorded by hand.
      </p>

      {outcome && <div className="alert alert-ok"><div>{outcome}</div></div>}
      <ErrorBox error={error} />

      {!connected && (
        <div className={canAuthorize ? 'alert alert-info' : 'alert alert-warn'}>
          <div>
            <strong>{canAuthorize ? 'No mailbox connected yet.' : 'Gmail is not set up on this installation.'}</strong>
            <div style={{ marginTop: 4 }}>{gmail?.reason}</div>
            {canAuthorize && (
              <div className="row" style={{ marginTop: 10 }}>
                <button className="btn btn-primary btn-sm" onClick={onConnect} disabled={connecting}>
                  {connecting ? <span className="spinner" /> : <Icon name="external" />} Connect Gmail
                </button>
                {consentOpened && (
                  <>
                    <button className="btn btn-sm" onClick={onReload}>
                      <Icon name="refresh" /> I have approved it
                    </button>
                    {consentUrl && (
                      <a
                        className="btn btn-sm btn-ghost"
                        href={consentUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        <Icon name="link" /> Open the consent screen
                      </a>
                    )}
                  </>
                )}
              </div>
            )}
            {consentOpened && (
              <div className="small muted" style={{ marginTop: 8 }}>
                A Google consent screen opened in a new tab. Approve it there, close the tab,
                then choose “I have approved it”.
              </div>
            )}
          </div>
        </div>
      )}

      {connected && (
        <dl className="mail-kv">
          <dt>Connected address</dt>
          <dd className="mono">{gmail.address}</dd>

          <dt>
            Granted access
            <HelpTip term="oauth_scope" />
          </dt>
          <dd>
            <div className="mail-scope">
              {(gmail.scopes || []).map((s) => (
                <span key={s} className="row" style={{ gap: 6, alignItems: 'baseline' }}>
                  <Badge tone={s.includes('mail.google.com') ? 'warn' : 'info'}>{scopeName(s)}</Badge>
                  <span className="small muted">{scopeLabel(s)}</span>
                </span>
              ))}
            </div>
          </dd>

          <dt>
            Token expires
            <HelpTip term="token_expiry" />
          </dt>
          <dd>
            {formatWhen(gmail.token_expires_at)}{' '}
            {isPast(gmail.token_expires_at) && (
              <Badge tone="warn">expired — renewed on the next send</Badge>
            )}
          </dd>

          <dt>
            Reply and bounce detection
            <HelpTip term="detected_reply" />
          </dt>
          <dd>
            <Badge tone="ok">Replies land in your inbox</Badge>{' '}
            <span className="small muted">
              read back by {gmail.imap_available ? 'IMAP' : 'the Gmail API'}, on demand from
              the send log.
            </span>
          </dd>
        </dl>
      )}

      {accounts.length > 0 && (
        <div className="table-wrap" style={{ marginTop: 14 }}>
          <table>
            <thead>
              <tr>
                <th>Mailbox</th>
                <th>Connected</th>
                <th>Token</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {accounts.map((a) => (
                <tr key={a.id}>
                  <td>
                    <div className="mono">{a.address}</div>
                    <div className="tiny muted">
                      {a.display_name || 'no display name'} ·{' '}
                      {a.is_active ? 'in use' : 'not in use'}
                    </div>
                  </td>
                  <td className="nowrap">{formatWhen(a.connected_at)}</td>
                  <td className="nowrap">
                    {a.has_credentials ? (
                      <Badge tone={isPast(a.token_expires_at) ? 'warn' : 'ok'}>
                        {isPast(a.token_expires_at) ? 'expired' : 'held, encrypted'}
                      </Badge>
                    ) : (
                      <Badge>revoked</Badge>
                    )}
                  </td>
                  <td className="nowrap">
                    <div className="row" style={{ gap: 6 }}>
                      {a.has_credentials && (
                        <button
                          className="btn btn-sm btn-danger"
                          onClick={() => setConfirm({ account: a, action: 'revoke' })}
                        >
                          <Icon name="lock" /> Revoke
                        </button>
                      )}
                      <button
                        className="btn btn-sm btn-ghost"
                        onClick={() => setConfirm({ account: a, action: 'forget' })}
                      >
                        <Icon name="trash" /> Forget
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {confirm && (
        <Modal
          title={confirm.action === 'revoke' ? 'Revoke this token?' : 'Forget this mailbox?'}
          onClose={() => setConfirm(null)}
          actions={
            <>
              <button className="btn btn-sm" onClick={() => setConfirm(null)}>
                Cancel
              </button>
              <button className="btn btn-sm btn-danger" onClick={run} disabled={busy}>
                {busy ? <span className="spinner" /> : confirm.action === 'revoke' ? 'Revoke' : 'Forget it'}
              </button>
            </>
          }
        >
          <p>
            {confirm.action === 'revoke' ? (
              <>
                The refresh token for <strong>{confirm.account.address}</strong> is sent to
                Google’s revocation endpoint and erased from this installation. Nothing further
                can be sent from that mailbox, and no reply or bounce will be detected, until
                you authorise it again.
              </>
            ) : (
              <>
                <strong>{confirm.account.address}</strong> is revoked first and then deleted, so
                no token is left orphaned. The send log for messages already sent from it is
                kept.
              </>
            )}
          </p>
        </Modal>
      )}
    </SectionCard>
  )
}
