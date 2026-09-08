/**
 * FR-463: the campaign package.
 *
 * One zip per campaign, holding a readable PDF bundle, the complete JSON
 * package, the documents already generated, and a manifest naming exactly what
 * is inside. It is built for somebody outside the system - a career coach,
 * usually - which is why the two guarantees are stated on the screen and
 * repeated in the manifest rather than left in the documentation: the fields
 * marked do-not-disclose are stripped, and the package contains no other job
 * seeker's data.
 *
 * The build is a single synchronous call on the API, so progress is honest
 * about what it can show: elapsed time and the stages being assembled, with
 * no cancel control, because there is no job to cancel.
 */

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../../api/client'
import { Caution, HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  Modal,
  formatDate,
  formatDuration,
  useFetch,
} from '../../components/ui'
import { ExportResult, formatBytes, summarise } from './ExportManifest'

const LANGUAGES = [
  { value: 'en', label: 'English' },
  { value: 'nl', label: 'Nederlands' },
  { value: 'fr', label: 'Français' },
]

const STAGES = [
  'Reading the campaign, its directives and the profile version it used',
  'Applying your do-not-disclose flags to every profile block (FR-106)',
  'Collecting the ranked list, company profiles, financials and contacts',
  'Rendering the PDF bundle and writing the JSON package',
  'Checking no other job seeker appears anywhere in the package',
]

export default function CampaignExport({ campaigns, campaignId, onPickCampaign }) {
  // Exports need a named campaign — the API has no "most recent" default here.
  const chosen = campaignId || campaigns[0]?.id || ''
  const campaign = campaigns.find((c) => c.id === chosen)

  const [language, setLanguage] = useState('en')
  const [includePdf, setIncludePdf] = useState(true)
  const [includeDocuments, setIncludeDocuments] = useState(true)
  const [includeContacts, setIncludeContacts] = useState(true)
  const [building, setBuilding] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [result, setResult] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [confirming, setConfirming] = useState(false)

  const packages = useFetch(
    () => (chosen ? api.get(`/networking/exports?campaign_id=${encodeURIComponent(chosen)}`) : Promise.resolve([])),
    [chosen],
  )

  // NFR-502 asks for progress on anything slow. The build is one blocking
  // request, so the only truthful progress is how long it has been going.
  useEffect(() => {
    if (!building) return undefined
    setElapsed(0)
    const started = Date.now()
    const timer = setInterval(() => setElapsed((Date.now() - started) / 1000), 500)
    return () => clearInterval(timer)
  }, [building])

  async function build() {
    setConfirming(false)
    setActionError(null)
    setResult(null)
    setBuilding(true)
    try {
      const res = await api.post('/networking/exports', {
        campaign_id: chosen,
        language,
        include_pdf: includePdf,
        include_documents: includeDocuments,
        include_contacts: includeContacts,
      })
      setResult(res)
      packages.reload()
    } catch (err) {
      setActionError(err)
    } finally {
      setBuilding(false)
    }
  }

  async function download(exportId, name) {
    setActionError(null)
    try {
      await api.download(`/networking/exports/${exportId}/download`, name)
    } catch (err) {
      setActionError(err)
    }
  }

  const rows = packages.data || []

  if (!chosen) {
    return (
      <Empty title="No campaign to export">
        An export is built from one campaign. <Link to="/campaigns">Create a campaign</Link>{' '}
        first, and it becomes exportable as soon as it has collected anything.
      </Empty>
    )
  }

  return (
    <>
      {/* FR-463 states both guarantees on the screen, not only in the manifest. */}
      <Caution title="What leaves this machine, and what does not.">
        The package respects your do-not-disclose flags: every field you marked is stripped
        from the profile blocks before the PDF or the JSON is written, and the manifest lists
        which paths were applied so the reader knows the document is deliberately incomplete.
        It contains the data of one job seeker — you — and nothing else; the package is checked
        for any other job seeker's identifier before it is sealed, and the export is refused if
        one is found. Making an export is recorded in your audit trail (NFR-702).
      </Caution>

      <div className="card">
        <div className="card-header">
          <Icon name="download" />
          <h3>Build a package</h3>
          <div className="spacer" />
          <HelpTip term="campaign_package" align="right" />
        </div>

        <div className="grid grid-2">
          <div className="field">
            <label>Campaign</label>
            <select value={chosen} onChange={(e) => onPickCampaign(e.target.value)}>
              {campaigns.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name} · {c.status}
                </option>
              ))}
            </select>
            <span className="hint">
              Everything in the package comes from this one campaign: its directives, the
              profile version it ran on, and the opportunities it found.
            </span>
          </div>

          <div className="field">
            <label>Language</label>
            <select value={language} onChange={(e) => setLanguage(e.target.value)}>
              {LANGUAGES.map((l) => (
                <option key={l.value} value={l.value}>
                  {l.label}
                </option>
              ))}
            </select>
            <span className="hint">The language the PDF bundle is written in.</span>
          </div>
        </div>

        <div className="col" style={{ gap: 6, marginBottom: 12 }}>
          <label className="checkline">
            <input
              type="checkbox"
              checked={includePdf}
              onChange={(e) => setIncludePdf(e.target.checked)}
            />
            <span style={{ display: 'flex', alignItems: 'center' }}>
              PDF bundle — the readable version
              <HelpTip title="PDF bundle and JSON package">
                Two views of the same campaign in one download. The PDF is written to be read
                by a person: the ranked list, the company profiles, the applications and the
                outcomes. The JSON is the complete structured record, for anything that needs
                to process it. The JSON is always included; the PDF is what this toggle
                controls.
              </HelpTip>
            </span>
          </label>
          <label className="checkline">
            <input
              type="checkbox"
              checked={includeDocuments}
              onChange={(e) => setIncludeDocuments(e.target.checked)}
            />
            <span>Generated documents — the tailored CVs and briefings already produced</span>
          </label>
          <label className="checkline">
            <input
              type="checkbox"
              checked={includeContacts}
              onChange={(e) => setIncludeContacts(e.target.checked)}
            />
            <span style={{ display: 'flex', alignItems: 'center' }}>
              Hiring contacts
              <HelpTip title="Third-party contact data">
                Names and business addresses of people at target companies. They are
                professional contact data on a legitimate-interest basis, and anyone who
                objected is excluded. Leave this off if the package is going to somebody who
                has no reason to see them.
              </HelpTip>
            </span>
          </label>
        </div>

        <div className="row">
          <button
            className="btn btn-primary"
            disabled={building}
            onClick={() => setConfirming(true)}
          >
            {building ? <span className="spinner" /> : <Icon name="download" />} Build the
            package
          </button>
          <span className="small muted">
            The JSON package is always written; the PDF is optional.
          </span>
        </div>

        {building && (
          <div style={{ marginTop: 14 }}>
            <div className="progress-track">
              <div className="progress-fill indeterminate" />
            </div>
            <div className="row small muted" style={{ marginTop: 6 }}>
              <span>Assembling · {formatDuration(elapsed)} elapsed</span>
              <div className="spacer" />
              <span>Runs to completion on the server; there is nothing to cancel.</span>
            </div>
            <ul className="help-tips" style={{ marginTop: 8 }}>
              {STAGES.map((s) => (
                <li key={s}>{s}</li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {actionError && <ErrorBox error={actionError} />}

      {result && (
        <ExportResult
          result={result}
          onDownload={() => download(result.id, `campaign-${chosen}.zip`)}
        />
      )}

      <div className="card">
        <div className="card-header">
          <Icon name="document" />
          <h3>Packages built for {campaign?.name || 'this campaign'}</h3>
        </div>

        {packages.loading && <Loading rows={2} />}
        {packages.error && <ErrorBox error={packages.error} onRetry={packages.reload} />}

        {!packages.loading && !packages.error && rows.length === 0 && (
          <Empty title="No package yet">
            Nothing has been exported for this campaign. Build one above — it can be rebuilt
            at any time, and each build is kept.
          </Empty>
        )}

        {rows.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Built</th>
                  <th>Language</th>
                  <th>Contents</th>
                  <th className="num">Size</th>
                  <th>Status</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id}>
                    <td>{formatDate(row.created_at)}</td>
                    <td>{row.language}</td>
                    <td className="small muted">{summarise(row.manifest)}</td>
                    <td className="num">{formatBytes(row.byte_size)}</td>
                    <td>
                      <Badge tone={row.status === 'ready' ? 'ok' : 'danger'}>
                        {row.status}
                      </Badge>
                      {row.last_error && (
                        <div className="tiny muted">{row.last_error}</div>
                      )}
                    </td>
                    <td>
                      <button
                        className="btn btn-sm"
                        disabled={row.status !== 'ready'}
                        onClick={() => download(row.id, `campaign-${row.campaign_id}.zip`)}
                      >
                        <Icon name="download" /> Download
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {confirming && (
        <Modal
          title="Build and download this campaign as a package?"
          onClose={() => setConfirming(false)}
          actions={
            <>
              <button className="btn" onClick={() => setConfirming(false)}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={build}>
                Build it
              </button>
            </>
          }
        >
          <p className="small" style={{ lineHeight: 1.6 }}>
            The package is a file you can send to anyone, so it is worth being deliberate about
            it. It will contain the ranked list, the company profiles and financials,{' '}
            {includeContacts ? 'the hiring contacts, ' : 'no hiring contacts, '}
            {includeDocuments ? 'the documents already generated, ' : 'no generated documents, '}
            and your profile with every do-not-disclose field removed.
          </p>
          <p className="small muted" style={{ lineHeight: 1.6 }}>
            Nothing is sent anywhere. The file is written on this machine and stays there until
            you download it. The fact that you made it is recorded in your audit trail.
          </p>
        </Modal>
      )}
    </>
  )
}
