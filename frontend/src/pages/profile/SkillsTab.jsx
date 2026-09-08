/**
 * Skills tab (FR-107).
 *
 * A skill is stored twice: the raw label as it appeared in your documents, and
 * a normalised label from the ESCO taxonomy. Matching runs on the normalised
 * one, which is why it is editable here — a mis-normalised skill is invisible
 * to every later scoring step rather than merely untidy.
 */

import { useEffect, useState } from 'react'

import { api } from '../../api/client'
import { HelpTip } from '../../components/Help'
import { Badge, Empty, ErrorBox, Loading, Modal, useFetch } from '../../components/ui'

const PROFICIENCY = {
  1: '1 · aware',
  2: '2 · working',
  3: '3 · practised',
  4: '4 · strong',
  5: '5 · expert',
}

export default function SkillsTab({ profile }) {
  const { data, loading, error, reload } = useFetch(() => api.get('/profile/skills'), [profile?.id])
  const [rows, setRows] = useState([])
  const [busy, setBusy] = useState(null)
  const [failure, setFailure] = useState(null)
  const [deleting, setDeleting] = useState(null)

  useEffect(() => {
    setRows(data || [])
  }, [data])

  function edit(id, patch) {
    setRows((current) => current.map((s) => (s.id === id ? { ...s, ...patch, _dirty: true } : s)))
  }

  async function persist(skill) {
    if (!skill._dirty) return
    setFailure(null)
    setBusy(skill.id)
    try {
      const next = await api.put(`/profile/skills/${skill.id}`, {
        normalised_label: skill.normalised_label || undefined,
        proficiency: skill.proficiency ?? undefined,
        years_experience: skill.years_experience ?? undefined,
        last_used_year: skill.last_used_year ?? undefined,
      })
      setRows((current) => current.map((s) => (s.id === next.id ? next : s)))
    } catch (err) {
      setFailure(err)
    } finally {
      setBusy(null)
    }
  }

  /** FR-107: normalisation is a server-side taxonomy lookup, not a guess here. */
  async function normalise(skill) {
    setFailure(null)
    setBusy(skill.id)
    try {
      const match = await api.get(
        `/profile/skills/normalise?label=${encodeURIComponent(skill.raw_label)}`,
      )
      if (match.normalised_label) {
        const next = await api.put(`/profile/skills/${skill.id}`, {
          normalised_label: match.normalised_label,
        })
        setRows((current) => current.map((s) => (s.id === next.id ? next : s)))
      } else {
        setFailure(new Error(`“${skill.raw_label}” has no match in the taxonomy — edit it by hand.`))
      }
    } catch (err) {
      setFailure(err)
    } finally {
      setBusy(null)
    }
  }

  async function refresh() {
    setFailure(null)
    setBusy('refresh')
    try {
      setRows(await api.post('/profile/skills/refresh'))
    } catch (err) {
      setFailure(err)
    } finally {
      setBusy(null)
    }
  }

  async function remove(skill) {
    setDeleting(null)
    try {
      await api.del(`/profile/skills/${skill.id}`)
      setRows((current) => current.filter((s) => s.id !== skill.id))
    } catch (err) {
      setFailure(err)
    }
  }

  if (loading) return <Loading rows={5} />
  if (error) return <ErrorBox error={error} onRetry={reload} />

  return (
    <div className="col" style={{ gap: 14 }}>
      {failure && <ErrorBox error={failure} onRetry={() => setFailure(null)} />}

      <div className="row">
        <span className="small muted">
          {rows.length} skill{rows.length === 1 ? '' : 's'} derived from version {profile?.version}
        </span>
        <div className="spacer" />
        <button className="btn btn-sm" disabled={!profile || busy === 'refresh'} onClick={refresh}>
          {busy === 'refresh' ? 'Re-deriving…' : 'Re-derive from the profile'}
        </button>
      </div>

      {rows.length === 0 ? (
        <Empty title="No skills yet">
          Skills are read out of your experience and top-skills sections. Import a document or
          save the Sections tab, then re-derive.
        </Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>As written</th>
                <th>
                  Normalised
                  <HelpTip term="normalised_skill" />
                </th>
                <th>
                  Proficiency
                  <HelpTip
                    title="Proficiency"
                  >
                    Your own 1–5 rating. It is used to decide what a tailored CV leads with, and
                    it is never presented to an employer as a number.
                  </HelpTip>
                </th>
                <th className="num">Years</th>
                <th className="num">
                  Last used
                  <HelpTip term="staleness" align="right" />
                </th>
                <th>Evidence</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((s) => (
                <tr key={s.id}>
                  <td>
                    <div>{s.raw_label}</div>
                    {s.taxonomy_code && <span className="tiny muted mono">{s.taxonomy_code}</span>}
                  </td>
                  <td style={{ minWidth: 200 }}>
                    <input
                      type="text"
                      value={s.normalised_label ?? ''}
                      onChange={(e) => edit(s.id, { normalised_label: e.target.value })}
                      onBlur={() => persist(s)}
                    />
                    <button
                      className="btn btn-sm btn-ghost"
                      style={{ marginTop: 4 }}
                      disabled={busy === s.id}
                      onClick={() => normalise(s)}
                    >
                      Look up in the taxonomy
                    </button>
                  </td>
                  <td>
                    <select
                      value={s.proficiency ?? ''}
                      onChange={(e) => {
                        const v = e.target.value === '' ? null : Number(e.target.value)
                        edit(s.id, { proficiency: v })
                      }}
                      onBlur={() => persist(s)}
                    >
                      <option value="">–</option>
                      {[1, 2, 3, 4, 5].map((p) => (
                        <option key={p} value={p}>
                          {PROFICIENCY[p]}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="num" style={{ minWidth: 80 }}>
                    <input
                      type="number"
                      min="0"
                      step="0.5"
                      value={s.years_experience ?? ''}
                      onChange={(e) =>
                        edit(s.id, {
                          years_experience: e.target.value === '' ? null : Number(e.target.value),
                        })
                      }
                      onBlur={() => persist(s)}
                    />
                  </td>
                  <td className="num" style={{ minWidth: 90 }}>
                    <input
                      type="number"
                      min="1900"
                      max="2100"
                      value={s.last_used_year ?? ''}
                      onChange={(e) =>
                        edit(s.id, {
                          last_used_year: e.target.value === '' ? null : Number(e.target.value),
                        })
                      }
                      onBlur={() => persist(s)}
                    />
                  </td>
                  <td>
                    {(s.evidence_refs || []).length > 0 ? (
                      <Badge tone="ok">{s.evidence_refs.length} linked</Badge>
                    ) : (
                      <span className="tiny muted">none</span>
                    )}
                  </td>
                  <td className="nowrap">
                    {busy === s.id && <span className="spinner" />}
                    <button className="btn btn-sm btn-danger" onClick={() => setDeleting(s)}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {deleting && (
        <Modal
          title={`Remove “${deleting.normalised_label || deleting.raw_label}”?`}
          onClose={() => setDeleting(null)}
          actions={
            <>
              <button className="btn" onClick={() => setDeleting(null)}>
                Cancel
              </button>
              <button className="btn btn-danger" onClick={() => remove(deleting)}>
                Remove
              </button>
            </>
          }
        >
          <p>
            Matching will stop counting this skill. Re-deriving from the profile brings it back if
            it is still written somewhere in your experience.
          </p>
        </Modal>
      )}
    </div>
  )
}
