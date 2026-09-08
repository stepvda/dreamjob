/**
 * The source catalogue (FR-363, IR-101, NFR-403).
 *
 * IR-101 is the reason this tab is not a list of switches. A source whose
 * terms prohibit automated access is disabled and stays disabled until an
 * administrator says, by name and in writing, that they take responsibility;
 * the backend refuses `enabled: true` with a 409 until then. Enabling it is a
 * second, separate decision afterwards — the acknowledgement records a
 * judgement, it does not act on one. That two-step shape is deliberate and is
 * reproduced here rather than collapsed into one click.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { Caution, HelpTip } from '../../components/Help'
import {
  Badge,
  ErrorBox,
  Loading,
  formatDate,
  formatPercent,
  useFetch,
} from '../../components/ui'
import { parseJson } from './format'
import { AcknowledgeModal, SourceModal, TOS_TONE } from './SourceModals'

export default function SourcesTab() {
  const sources = useFetch(() => api.get('/admin/sources'), [])
  const [open, setOpen] = useState(null) // adapter_key of the settings modal
  const [ack, setAck] = useState(null) // adapter_key of the acknowledgement modal
  const [busyKey, setBusyKey] = useState(null)
  const [error, setError] = useState(null)

  if (sources.loading) return <Loading rows={6} />
  if (sources.error) return <ErrorBox error={sources.error} onRetry={sources.reload} />

  const rows = sources.data || []
  const blocked = rows.filter((r) => r.blocked_pending_acknowledgement)
  const live = rows.filter((r) => r.effective_enabled)

  async function toggle(row) {
    setError(null)
    setBusyKey(row.adapter_key)
    try {
      await api.patch(`/admin/sources/${row.adapter_key}`, { enabled: !row.enabled })
      sources.reload()
    } catch (err) {
      setError(err)
    } finally {
      setBusyKey(null)
    }
  }

  const current = rows.find((r) => r.adapter_key === open)
  const acking = rows.find((r) => r.adapter_key === ack)

  return (
    <div className="stack">
      {error && <ErrorBox error={error} />}

      <div className="row row-wrap">
        <Badge tone="ok">{live.length} in use</Badge>
        <Badge>{rows.length} in the catalogue</Badge>
        {blocked.length > 0 && (
          <Badge tone="danger">{blocked.length} blocked pending acknowledgement</Badge>
        )}
      </div>

      {/* IR-101: the notice is on the screen, not in the help drawer. */}
      {blocked.length > 0 && (
        <Caution title="Some sources prohibit automated access in their own terms">
          {blocked.map((r) => r.display_name).join(', ')}{' '}
          {blocked.length === 1 ? 'is' : 'are'} disabled and cannot be switched on. Reading
          them automatically would breach the terms you agreed to when using the site, and
          the account used could be restricted. An administrator may accept that risk on
          behalf of this installation — open the source and read what its terms say first.
          The acknowledgement is recorded in the audit trail under your name.
        </Caution>
      )}

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Source</th>
              <th>Type</th>
              <th className="nowrap">
                Access
                <HelpTip term="access_method" />
              </th>
              <th>Coverage</th>
              <th className="nowrap">
                Rate limit
                <HelpTip
                  title="Rate limit"
                  align="right"
                >
                  Requests per second the adapter is allowed to make. It paces collection
                  rather than capping it; the ceiling on a single run is set per source
                  under “Configure”.
                </HelpTip>
              </th>
              <th className="nowrap">
                Terms
                <HelpTip term="terms_status" align="right" />
              </th>
              <th className="nowrap">
                Extraction
                <HelpTip term="extraction_rate" align="right" />
              </th>
              <th style={{ textAlign: 'right' }}>State</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const countries = parseJson(row.coverage_countries, []) || []
              return (
                <tr key={row.adapter_key}>
                  <td>
                    <div style={{ fontWeight: 600 }}>{row.display_name}</div>
                    <div className="mono tiny muted">{row.adapter_key}</div>
                  </td>
                  <td className="small">{row.source_type}</td>
                  <td className="small">{row.access_method}</td>
                  <td className="small muted">
                    {countries.length ? countries.join(', ') : 'Not country-specific'}
                  </td>
                  <td className="small nowrap">
                    {row.rate_limit_rps == null ? '–' : `${row.rate_limit_rps}/s`}
                  </td>
                  <td>
                    <Badge tone={TOS_TONE[row.tos_status]}>{row.tos_status}</Badge>
                    {row.acknowledged_at && (
                      <div className="tiny muted" style={{ marginTop: 3 }}>
                        acknowledged {formatDate(row.acknowledged_at)}
                      </div>
                    )}
                  </td>
                  <td className="small nowrap">
                    {row.extraction_success_rate == null
                      ? '–'
                      : formatPercent(row.extraction_success_rate, 0)}
                  </td>
                  <td style={{ textAlign: 'right' }}>
                    <div className="row" style={{ justifyContent: 'flex-end' }}>
                      {row.blocked_pending_acknowledgement ? (
                        <button
                          className="btn btn-sm btn-danger"
                          onClick={() => setAck(row.adapter_key)}
                        >
                          Read the terms
                        </button>
                      ) : (
                        <button
                          className="btn btn-sm"
                          disabled={busyKey === row.adapter_key}
                          onClick={() => toggle(row)}
                        >
                          {row.enabled ? 'Disable' : 'Enable'}
                        </button>
                      )}
                      <button className="btn btn-sm btn-ghost" onClick={() => setOpen(row.adapter_key)}>
                        Configure
                      </button>
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {acking && (
        <AcknowledgeModal
          row={acking}
          onClose={() => setAck(null)}
          onDone={() => {
            setAck(null)
            sources.reload()
          }}
        />
      )}

      {current && (
        <SourceModal
          row={current}
          onClose={() => setOpen(null)}
          onSaved={() => {
            setOpen(null)
            sources.reload()
          }}
        />
      )}
    </div>
  )
}
