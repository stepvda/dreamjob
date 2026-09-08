/**
 * Confirmations and the version history for saved directive sets (FR-148).
 *
 * Duplicating and deleting are outward-facing enough to confirm first, and the
 * version list is where "why does this campaign rank things that way?" gets
 * answered — so both live in a modal rather than inline in the editor.
 */

import Icon from '../../components/Icon'
import { ErrorBox, Loading, Modal, formatDate } from '../../components/ui'

export function DuplicateDialog({ dialog, onChange, onConfirm, onClose }) {
  return (
    <Modal
      title="Duplicate directive set"
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" onClick={onConfirm} disabled={!dialog.name.trim()}>
            <Icon name="copy" /> Duplicate
          </button>
        </>
      }
    >
      <p className="small muted">
        A duplicate starts its own version line at version 1, so the original keeps its history and
        the campaigns that used it stay explainable.
      </p>
      <div className="field">
        <label>New name</label>
        <input
          type="text"
          value={dialog.name}
          maxLength={120}
          autoFocus
          onChange={(e) => onChange({ ...dialog, name: e.target.value })}
        />
      </div>
    </Modal>
  )
}

export function DeleteDialog({ dialog, onConfirm, onClose }) {
  return (
    <Modal
      title={`Delete “${dialog.set.name}”?`}
      onClose={onClose}
      actions={
        <>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-danger" onClick={onConfirm}>
            <Icon name="trash" /> Delete
          </button>
        </>
      }
    >
      <p>
        This removes version {dialog.set.version} of this set. A set a campaign has already run
        under cannot be deleted — the server refuses it, and tells you why.
      </p>
    </Modal>
  )
}

export function VersionsDialog({ versions, onLoad, onClose }) {
  return (
    <Modal title={`Versions of “${versions.name}”`} onClose={onClose}>
      {versions.rows === null && <Loading rows={3} />}
      {versions.error && <ErrorBox error={versions.error} />}
      {versions.rows?.length === 0 && !versions.error && (
        <p className="small muted">No other version of this set exists yet.</p>
      )}
      {versions.rows?.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Version</th>
                <th className="nowrap">
                  <Icon name="calendar" /> Created
                </th>
                <th>Titles</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {versions.rows.map((v) => (
                <tr key={v.id}>
                  <td>v{v.version}</td>
                  <td className="nowrap">{formatDate(v.created_at)}</td>
                  <td>{(v.job_content?.target_titles || []).join(', ') || '–'}</td>
                  <td>
                    <button className="btn btn-sm" onClick={() => onLoad(v)}>
                      <Icon name="download" /> Load
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  )
}
