/**
 * Documents tab — the retained originals and the version history
 * (FR-102, FR-103, FR-105, DR-102).
 *
 * The two uploads are separate targets on purpose: the LinkedIn export defines
 * the shape of a profile (FR-102) and the CV is merged against it (FR-103), so
 * dropping a CV where the export belongs would silently produce a worse
 * profile. The originals are kept so extraction can be re-run without asking
 * for the files again (DR-102) — that is what "Re-read the originals" does.
 */

import { useRef, useState } from 'react'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import {
  Badge,
  Empty,
  ErrorBox,
  Loading,
  Modal,
  formatDate,
  useFetch,
} from '../../components/ui'

const SOURCE_LABEL = { linkedin_pdf: 'LinkedIn export', cv: 'CV' }

export default function DocumentsTab({ profile, onNewVersion }) {
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [warnings, setWarnings] = useState([])
  const [photoBroken, setPhotoBroken] = useState(false)
  const [viewing, setViewing] = useState(null)
  const [restoring, setRestoring] = useState(null)

  const versions = useFetch(() => api.get('/profile/versions'), [profile?.id])

  async function run(kind, work) {
    setError(null)
    setBusy(kind)
    try {
      const res = await work()
      setWarnings(res.warnings || [])
      setPhotoBroken(false)
      onNewVersion(res.profile)
      versions.reload()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(null)
    }
  }

  function upload(kind, file) {
    if (!file) return
    const body = new FormData()
    body.append('file', file)
    const path = kind === 'linkedin_pdf' ? '/profile/uploads/linkedin' : '/profile/uploads/cv'
    run(kind, () => api.upload(path, body))
  }

  async function restore(version) {
    setError(null)
    try {
      const next = await api.post(`/profile/versions/${version.id}/restore`)
      setRestoring(null)
      onNewVersion(next)
      versions.reload()
    } catch (err) {
      setRestoring(null)
      setError(err)
    }
  }

  const sources = profile?.sources || []
  const sourceOf = (kind) => sources.find((s) => s.kind === kind)

  return (
    <div className="col" style={{ gap: 14 }}>
      {error && <ErrorBox error={error} onRetry={() => setError(null)} />}

      {warnings.length > 0 && (
        <div className="alert alert-warn">
          <Icon name="warning" />
          <div>
            <strong>The document was read, with reservations.</strong>
            <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
              {warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          </div>
        </div>
      )}

      <div className="grid grid-2">
        <DropZone
          title="LinkedIn export (PDF)"
          hint='In LinkedIn: your profile → "Resources" → "Save to PDF". The export defines the shape of your profile (FR-102).'
          accept=".pdf"
          busy={busy === 'linkedin_pdf'}
          current={sourceOf('linkedin_pdf')}
          onFile={(f) => upload('linkedin_pdf', f)}
        />
        <DropZone
          title="CV (PDF or DOCX)"
          hint="Merged against the export. Where the two disagree you are asked which is right — nothing is picked for you (FR-103)."
          accept=".pdf,.docx"
          busy={busy === 'cv'}
          current={sourceOf('cv')}
          onFile={(f) => upload('cv', f)}
        />
      </div>

      <div className="card phase-edge phase-1">
        <div className="card-header">
          <Icon name="document" />
          <h3>Retained originals</h3>
          <HelpTip term="provenance" />
          <div className="spacer" />
          <button
            className="btn btn-sm"
            disabled={!sources.length || busy === 'rebuild'}
            onClick={() => run('rebuild', () => api.post('/profile/rebuild'))}
          >
            <Icon name="refresh" />
            {busy === 'rebuild' ? 'Re-reading…' : 'Re-read the originals'}
          </button>
        </div>

        <p className="small muted" style={{ marginTop: 0 }}>
          {/* DR-102: originals are kept so extraction can be improved and re-run. */}
          Your files are kept so the profile can be re-extracted later without asking you for
          them again. Re-reading creates a new version and never overwrites your edits.
        </p>

        <div className="row row-wrap" style={{ alignItems: 'flex-start', gap: 16 }}>
          <div style={{ flex: 1, minWidth: 260 }}>
            {sources.length === 0 ? (
              <p className="small muted">No document has been kept yet.</p>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Document</th>
                      <th>File</th>
                      <th className="num">Size</th>
                      <th>
                        <Icon name="clock" /> Kept
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {sources.map((s) => (
                      <tr key={s.kind}>
                        <td>
                          <Badge tone="info">{SOURCE_LABEL[s.kind] || s.kind}</Badge>
                        </td>
                        <td>{s.filename}</td>
                        <td className="num">{Math.round((s.byte_size || 0) / 1024)} kB</td>
                        <td className="nowrap">{formatDate(s.retained_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="col" style={{ gap: 6, alignItems: 'center' }}>
            <span className="small muted">
              Photo
              <HelpTip term="do_not_disclose" align="right" />
            </span>
            {profile?.photo_path && !photoBroken ? (
              <img
                className="profile-photo"
                src={`/api/profile/photo?v=${profile.version}`}
                alt="Photo extracted from your documents"
                onError={() => setPhotoBroken(true)}
              />
            ) : (
              <div
                className="profile-photo col"
                style={{ justifyContent: 'center', alignItems: 'center', textAlign: 'center', gap: 4 }}
              >
                <Icon name="profile" size={20} className="muted" />
                <span className="tiny muted">none extracted</span>
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="card phase-edge phase-1">
        <div className="card-header">
          <Icon name="clock" />
          <h3>Version history</h3>
          <HelpTip term="profile_version" />
        </div>

        {versions.loading && <Loading rows={3} />}
        {versions.error && <ErrorBox error={versions.error} onRetry={versions.reload} />}
        {!versions.loading && !versions.error && (versions.data || []).length === 0 && (
          <Empty
            title={
              <>
                <span className="icon-chip icon-chip-lg phase-chip" style={{ display: 'flex' }}>
                  <Icon name="clock" />
                </span>
                No versions yet
              </>
            }
          >
            The first upload or manual save creates version 1.
          </Empty>
        )}
        {!versions.loading && !versions.error && (versions.data || []).length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th className="num">Version</th>
                  <th>Saved from</th>
                  <th>
                    <Icon name="calendar" /> Created
                  </th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {versions.data.map((v) => (
                  <tr key={v.id}>
                    <td className="num">
                      {v.version}
                      {profile?.id === v.id && (
                        <Badge tone="accent">current</Badge>
                      )}
                    </td>
                    <td>{v.source_note || 'manual'}</td>
                    <td className="nowrap">{formatDate(v.created_at)}</td>
                    <td className="nowrap">
                      <button className="btn btn-sm" onClick={() => setViewing(v)}>
                        <Icon name="eye" />
                        View
                      </button>{' '}
                      {profile?.id !== v.id && (
                        <button className="btn btn-sm" onClick={() => setRestoring(v)}>
                          <Icon name="refresh" />
                          Restore
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {viewing && <VersionModal version={viewing} onClose={() => setViewing(null)} />}

      {/* House style: anything that changes what the rest of the system reads
          confirms first. A restore appends a new version, it does not delete. */}
      {restoring && (
        <Modal
          title={`Restore version ${restoring.version}?`}
          onClose={() => setRestoring(null)}
          actions={
            <>
              <button className="btn" onClick={() => setRestoring(null)}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={() => restore(restoring)}>
                <Icon name="refresh" />
                Restore as a new version
              </button>
            </>
          }
        >
          <p>
            Version {restoring.version} is brought back as the newest version. Nothing is
            deleted — the version you are on now stays in the history, and your unresolved
            conflicts move across with it.
          </p>
        </Modal>
      )}
    </div>
  )
}

/* --- Pieces --------------------------------------------------------------- */

function DropZone({ title, hint, accept, busy, current, onFile }) {
  const [over, setOver] = useState(false)
  const input = useRef(null)

  return (
    <div
      className={`dropzone${over ? ' over' : ''}${busy ? ' busy' : ''}`}
      role="button"
      tabIndex={0}
      onClick={() => !busy && input.current?.click()}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          if (!busy) input.current?.click()
        }
      }}
      onDragOver={(e) => {
        e.preventDefault()
        setOver(true)
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault()
        setOver(false)
        if (!busy) onFile(e.dataTransfer?.files?.[0])
      }}
    >
      <input
        ref={input}
        type="file"
        accept={accept}
        hidden
        onChange={(e) => {
          onFile(e.target.files?.[0])
          e.target.value = ''
        }}
      />
      <span className="icon-chip icon-chip-lg phase-chip" style={{ display: 'flex', margin: '0 auto 10px' }}>
        <Icon name={busy ? 'refresh' : 'upload'} />
      </span>
      <div className="dropzone-title">{title}</div>
      <div className="dropzone-hint">{hint}</div>
      <div className="dropzone-file">
        {busy ? (
          <span className="spinner" />
        ) : current ? (
          <>
            <Badge tone="ok">{current.filename}</Badge>{' '}
            <span className="muted tiny">drop a new file to replace it</span>
          </>
        ) : (
          <span className="muted">Drop a file here, or click to choose one</span>
        )}
      </div>
    </div>
  )
}

/** Read-only look at an older version, so restoring is an informed choice. */
function VersionModal({ version, onClose }) {
  const detail = useFetch(() => api.get(`/profile/versions/${version.id}`), [version.id])
  const sections = detail.data?.sections || {}
  const listed = Object.entries(sections).filter(([k]) => k !== '_meta')

  return (
    <Modal title={`Version ${version.version}`} onClose={onClose} wide>
      {detail.loading && <Loading rows={5} />}
      {detail.error && <ErrorBox error={detail.error} onRetry={detail.reload} />}
      {detail.data && (
        <div className="col" style={{ gap: 12 }}>
          <div className="row row-wrap small muted">
            <span>Saved from {detail.data.source_note || 'manual entry'}</span>
            <span>· {formatDate(detail.data.created_at)}</span>
            {detail.data.unresolved_conflicts > 0 && (
              <Badge tone="danger">{detail.data.unresolved_conflicts} unresolved</Badge>
            )}
          </div>

          <div className="chips">
            {listed.map(([key, value]) => (
              <span className="chip" key={key}>
                {key.replace(/_/g, ' ')}
                {Array.isArray(value) ? ` · ${value.length}` : ''}
              </span>
            ))}
          </div>

          {sections.summary && <div className="doc-preview">{sections.summary}</div>}

          <div className="doc-preview mono" style={{ maxHeight: 320 }}>
            {JSON.stringify(Object.fromEntries(listed), null, 2)}
          </div>
        </div>
      )}
    </Modal>
  )
}
