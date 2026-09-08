/**
 * Mail setup (FR-325, FR-326, FR-327, NFR-204).
 *
 * Everything the product sends leaves from here, so this screen answers four
 * questions and keeps them apart:
 *
 *   Which mailbox?   Two backends, and they are genuinely different. Gmail
 *                    sends as the job seeker and can read the answers back;
 *                    Resend is a one-way relay whose key does not exist yet.
 *   Under what rules? The FR-325 guard rails, reported rather than offered as
 *                    sliders, with the RK-05 warning about raising them.
 *   What went out?   The FR-326 send log, down to the message id.
 *   What is owed?    The FR-327 follow-ups.
 *
 * The four are tabs rather than one long page because they are consulted at
 * different moments: setup once, rules rarely, the log constantly.
 */

import { useState } from 'react'

import { api } from '../api/client'
import { FirstRun, ScreenIntro } from '../components/Help'
import Icon from '../components/Icon'
import WorkflowMap from '../components/WorkflowMap'
import { ErrorBox, Loading, Tabs, useFetch } from '../components/ui'
import Backends from './mail/Backends'
import DispatchLog from './mail/DispatchLog'
import FollowUps from './mail/FollowUps'
import SendingRules from './mail/SendingRules'

export default function MailSettingsPage() {
  const status = useFetch(() => api.get('/mail/status'))
  const settings = useFetch(() => api.get('/mail/settings'))
  const dispatches = useFetch(() => api.get('/mail/dispatches?limit=200'))
  const replies = useFetch(() => api.get('/mail/replies?limit=100'))
  const followUps = useFetch(() => api.get('/mail/follow-ups'))
  // Neither of these is load-bearing: the strip and the administrator-only
  // control degrade quietly rather than failing the screen.
  const journey = useFetch(() => api.get('/overview/journey').catch(() => null))
  const me = useFetch(() => api.get('/auth/me').catch(() => null))

  const [tab, setTab] = useState('mailboxes')
  const [connecting, setConnecting] = useState(false)
  const [consentOpened, setConsentOpened] = useState(false)
  const [consentUrl, setConsentUrl] = useState(null)
  const [connectError, setConnectError] = useState(null)

  const accounts = status.data?.accounts || []
  const counts = status.data?.dispatch_counts || {}
  const sentTotal = Object.values(counts).reduce((a, b) => a + b, 0)
  const queued = counts.queued || 0
  const hasGmail = accounts.some((a) => a.backend === 'gmail_oauth' && a.has_credentials)

  /**
   * NFR-204: the consent screen is Google's own, opened in a new tab, and the
   * loopback callback finishes the handshake server-side. Nothing here ever
   * sees the code or the token.
   */
  async function connectGmail() {
    setConnecting(true)
    setConnectError(null)
    try {
      const res = await api.get('/mail/gmail/authorize')
      // The URL is kept as well as opened: a popup blocker can swallow a
      // window.open that happens after an await, and a dead "Connect" button
      // is indistinguishable from a broken installation.
      window.open(res.authorization_url, '_blank', 'noopener,noreferrer')
      setConsentUrl(res.authorization_url)
      setConsentOpened(true)
    } catch (err) {
      setConnectError(err)
    } finally {
      setConnecting(false)
    }
  }

  function reloadAll() {
    setConsentOpened(false)
    setConsentUrl(null)
    status.reload()
    dispatches.reload()
    replies.reload()
    followUps.reload()
  }

  // Three distinct states for the screen's own load. The tabs each hold their
  // own three for the lists inside them.
  if (status.loading || settings.loading) {
    return (
      <div className="content-wide">
        <ScreenIntro pathname="/mail" />
        <Loading rows={5} />
      </div>
    )
  }

  if (status.error || settings.error) {
    return (
      <div className="content-wide">
        <ScreenIntro pathname="/mail" />
        <ErrorBox
          error={status.error || settings.error}
          onRetry={() => {
            status.reload()
            settings.reload()
          }}
        />
      </div>
    )
  }

  const firstRun = accounts.length === 0 && sentTotal === 0

  const tabs = [
    { key: 'mailboxes', label: 'Mailboxes', count: accounts.length || undefined },
    { key: 'rules', label: 'Sending rules', count: queued || undefined },
    { key: 'log', label: 'Dispatch log', count: sentTotal || undefined },
    { key: 'followups', label: 'Follow-ups', count: followUps.data?.length || undefined },
  ]

  return (
    <div className="content-wide">
      <WorkflowMap journey={journey.data?.journey || {}} compact current="dispatch" />
      <ScreenIntro pathname="/mail" />

      <ErrorBox error={connectError} />

      {firstRun && (
        <FirstRun
          pathname="/mail"
          title="No mailbox is connected yet"
          action={
            <button className="btn btn-primary" onClick={connectGmail} disabled={connecting}>
              {connecting ? <span className="spinner" /> : <Icon name="external" />} Connect Gmail
            </button>
          }
        >
          Nothing can be sent until one of the two backends is usable. Gmail is the one to
          choose if you can: it sends from your own address and, because the answers come back
          to your own inbox, it is the only backend that detects replies and bounces for you.
          Resend is ready to use but its API key has not been issued yet.
        </FirstRun>
      )}

      <Tabs tabs={tabs} active={tab} onChange={setTab} />

      {tab === 'mailboxes' && (
        <Backends
          status={status.data}
          onConnect={connectGmail}
          connecting={connecting}
          consentOpened={consentOpened}
          consentUrl={consentUrl}
          onReload={reloadAll}
          isAdmin={Boolean(me.data?.is_admin)}
        />
      )}

      {tab === 'rules' && (
        <SendingRules
          settings={settings.data}
          queued={queued}
          onProcessed={() => {
            status.reload()
            dispatches.reload()
          }}
        />
      )}

      {tab === 'log' && (
        <DispatchLog
          dispatches={dispatches}
          replies={replies}
          counts={counts}
          canPoll={hasGmail}
        />
      )}

      {tab === 'followups' && (
        <FollowUps
          followUps={followUps}
          settings={settings.data}
          onSettingsSaved={settings.reload}
        />
      )}
    </div>
  )
}
