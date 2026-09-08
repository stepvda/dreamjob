/**
 * FR-463: what a finished package actually contains.
 *
 * The manifest is the honest half of an export: it names the counts, the files
 * and - the part that matters when the package is going to somebody else - the
 * fields that were withheld because they are marked do-not-disclose, and the
 * isolation check that proves no other job seeker is in it.
 */

import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { Badge } from '../../components/ui'

export function ExportResult({ result, onDownload }) {
  const manifest = result.manifest || {}
  const counts = manifest.counts || {}
  const privacy = manifest.privacy || {}
  const files = manifest.files || {}

  return (
    <div className="card phase-edge phase-4">
      <div className="card-header">
        <Icon name="success" />
        <h3>Package ready</h3>
        <div className="spacer" />
        <button className="btn btn-primary btn-sm" onClick={onDownload}>
          <Icon name="download" /> Download {formatBytes(result.byte_size)}
        </button>
      </div>

      <div className="grid grid-4" style={{ marginBottom: 12 }}>
        <Count label="Opportunities" value={counts.opportunities} />
        <Count label="Companies" value={counts.companies} />
        <Count label="Vacancies" value={counts.vacancies} />
        <Count label="Contacts" value={counts.contacts} />
        <Count label="Applications" value={counts.application_packages} />
        <Count label="Documents" value={counts.documents} />
      </div>

      <div className="row row-wrap small" style={{ marginBottom: 10 }}>
        <span className="muted">Inside:</span>
        {files.json && <Badge tone="info">{files.json}</Badge>}
        {files.pdf && <Badge tone="info">{files.pdf}</Badge>}
        <Badge>MANIFEST.json</Badge>
        {files.documents?.length > 0 && (
          <Badge>
            {files.documents.length} document{files.documents.length === 1 ? '' : 's'}
          </Badge>
        )}
      </div>

      <div className="entry-card">
        <div className="small" style={{ display: 'flex', alignItems: 'center', marginBottom: 6 }}>
          <strong>What was withheld</strong>
          <HelpTip term="do_not_disclose" />
        </div>
        <div className="small muted" style={{ lineHeight: 1.6 }}>
          {privacy.do_not_disclose_applied ? (
            <>
              {privacy.do_not_disclose_paths?.length || 0} field
              {privacy.do_not_disclose_paths?.length === 1 ? '' : 's'} you marked
              do-not-disclose {privacy.do_not_disclose_paths?.length === 1 ? 'was' : 'were'}{' '}
              removed:{' '}
              <span className="mono">{(privacy.do_not_disclose_paths || []).join(', ')}</span>
            </>
          ) : (
            'You have not marked any field do-not-disclose, so the profile is complete in this package.'
          )}
          {privacy.discretion_mode && (
            <div style={{ marginTop: 6 }}>
              Discretion mode was on: {privacy.companies_excluded_by_discretion ?? 0} compan
              {privacy.companies_excluded_by_discretion === 1 ? 'y was' : 'ies were'} excluded.
            </div>
          )}
          {privacy.isolation && <div style={{ marginTop: 6 }}>{privacy.isolation}</div>}
          {manifest.isolation_check?.result && (
            <div style={{ marginTop: 6 }}>
              <Badge tone="ok">Isolation check passed</Badge>{' '}
              <span className="tiny">{manifest.isolation_check.result}</span>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function Count({ label, value }) {
  return (
    <div className="entry-card">
      <div className="stat-value" style={{ fontSize: 20 }}>
        {value ?? 0}
      </div>
      <div className="stat-label">{label}</div>
    </div>
  )
}

/* --- Helpers -------------------------------------------------------------- */

export function summarise(manifest) {
  const counts = manifest?.counts
  if (!counts) return '–'
  return [
    `${counts.opportunities ?? 0} opportunities`,
    `${counts.companies ?? 0} companies`,
    `${counts.contacts ?? 0} contacts`,
    `${counts.documents ?? 0} documents`,
  ].join(' · ')
}

export function formatBytes(bytes) {
  if (bytes == null) return '–'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} kB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
