/**
 * The standardised profile (FR-222) in the fixed order every company uses:
 * identity, what it does, how big it is, where it is, how it is organised
 * (FR-223), who runs it, who buys from it, what it builds with, and what it
 * has been in the news for.
 *
 * The layout is deliberately identical for every company, including the empty
 * sections: a missing departmental map is information, and hiding it would
 * make a thin profile look like a full one.
 */

import { HelpTip } from '../../components/Help'
import { Badge, Provenance, formatDate } from '../../components/ui'

/** NFR-402: a field carries where it came from and how sure the reader may be. */
function FieldMark({ profile, path }) {
  const source = profile.field_sources?.[path]
  const confidence = profile.confidence?.[path]
  if (!source && confidence == null) return null
  return <Provenance source={source || path} confidence={confidence} />
}

function Section({ title, tip, profile, path, children, count }) {
  const low = path && (profile.low_confidence_fields || []).includes(path)
  return (
    <div className="card">
      <div className="card-header">
        <h3>
          {title}
          {tip}
        </h3>
        {count != null && <span className="badge">{count}</span>}
        <div className="spacer" />
        {path && <FieldMark profile={profile} path={path} />}
      </div>
      <div className={low ? 'confidence-low' : undefined}>{children}</div>
    </div>
  )
}

function Nothing({ children }) {
  return (
    <p className="small muted" style={{ margin: 0 }}>
      {children}
    </p>
  )
}

function label(item, ...keys) {
  for (const key of keys) {
    if (item?.[key]) return item[key]
  }
  return null
}

export default function CompanyProfileTab({ profile }) {
  const identity = profile.identity || {}
  const size = profile.size || {}
  const structure = profile.structure || {}
  const units = structure.business_units || []
  const functions = structure.functions || []
  const people = profile.key_people || []
  const references = profile.references || []
  const stack = profile.tech_stack || []
  const news = profile.news || []
  const sources = profile.sources || []
  const provenanceRows = Object.entries(profile.confidence || {}).sort(([a], [b]) =>
    a.localeCompare(b),
  )

  return (
    <div className="stack">
      <Section title="Identity" profile={profile} path="legal_id">
        <div className="entry-grid">
          <div>
            <div className="tiny muted">Registered name</div>
            <div>{identity.name || profile.name}</div>
          </div>
          <div>
            <div className="tiny muted">
              Legal identifier
              <HelpTip title="Legal identifier">
                The registry number the company is filed under — a KBO/BCE number in Belgium, a
                Companies House number in the UK, an LEI for a listed group. It is the key the
                knowledge base de-duplicates on, so two spellings of one name still resolve to one
                company (DR-101).
              </HelpTip>
            </div>
            <div className="mono">
              {identity.legal_id || '–'}
              {identity.legal_id_type ? ` (${identity.legal_id_type})` : ''}
            </div>
          </div>
          <div>
            <div className="tiny muted">VAT number</div>
            <div className="mono">{identity.vat_number || '–'}</div>
          </div>
          <div>
            <div className="tiny muted">Jurisdiction</div>
            <div>{identity.jurisdiction || identity.country || '–'}</div>
          </div>
          <div>
            <div className="tiny muted">Website</div>
            <div>
              {identity.domain ? (
                <a href={`https://${identity.domain}`} target="_blank" rel="noreferrer">
                  {identity.domain}
                </a>
              ) : (
                '–'
              )}
            </div>
          </div>
          <div>
            <div className="tiny muted">Careers page</div>
            <div>
              {identity.careers_url ? (
                <a href={identity.careers_url} target="_blank" rel="noreferrer">
                  open
                </a>
              ) : (
                '–'
              )}
              {identity.ats_vendor ? (
                <span className="small muted"> · {identity.ats_vendor}</span>
              ) : null}
            </div>
          </div>
        </div>
      </Section>

      <Section title="What it does" profile={profile} path="business_summary">
        {profile.business_summary ? (
          <p style={{ margin: 0, lineHeight: 1.6 }}>{profile.business_summary}</p>
        ) : (
          <Nothing>No summary was extracted from this company's own pages.</Nothing>
        )}

        {(profile.products_services || []).length > 0 && (
          <>
            <div className="tiny muted" style={{ margin: '14px 0 6px' }}>
              Products and services
              <FieldMark profile={profile} path="products_services" />
            </div>
            <div className="chips">
              {profile.products_services.map((p, i) => (
                <span key={i} className="chip" title={label(p, 'description') || undefined}>
                  {label(p, 'name', 'text', 'value')}
                </span>
              ))}
            </div>
          </>
        )}

        {(profile.markets || []).length > 0 && (
          <>
            <div className="tiny muted" style={{ margin: '14px 0 6px' }}>
              Markets
              <FieldMark profile={profile} path="markets" />
            </div>
            <div className="chips">
              {profile.markets.map((m, i) => (
                <span key={i} className="chip">
                  {label(m, 'name', 'text', 'value')}
                  {m.kind ? <span className="muted"> · {m.kind}</span> : null}
                </span>
              ))}
            </div>
          </>
        )}

        {(profile.sector_codes || []).length > 0 && (
          <>
            <div className="tiny muted" style={{ margin: '14px 0 6px' }}>
              Sector codes
              <FieldMark profile={profile} path="sector_codes" />
            </div>
            <div className="chips">
              {profile.sector_codes.map((s, i) => (
                <span key={i} className="chip">
                  <span className="mono">{s.code || '—'}</span>
                  {s.label ? ` ${s.label}` : ''}
                  {s.system ? <span className="muted"> · {s.system}</span> : null}
                </span>
              ))}
            </div>
          </>
        )}
      </Section>

      <Section title="Size and shape" profile={profile} path="size_band">
        <div className="entry-grid">
          <div>
            <div className="tiny muted">Headcount</div>
            <div>{size.fte ? `${size.fte} FTE` : '–'}</div>
          </div>
          <div>
            <div className="tiny muted">
              Size band
              <HelpTip term="size_band" />
            </div>
            <div>{size.band || '–'}</div>
          </div>
          <div>
            <div className="tiny muted">
              Stage
              <HelpTip term="company_stage" />
            </div>
            <div>{size.stage || '–'}</div>
          </div>
          <div>
            <div className="tiny muted">Ownership</div>
            <div>{size.ownership || '–'}</div>
          </div>
          <div>
            <div className="tiny muted">
              Trajectory
              <HelpTip term="trajectory" />
            </div>
            <div>{size.trajectory || '–'}</div>
          </div>
        </div>

        <div className="tiny muted" style={{ margin: '14px 0 6px' }}>
          Locations
          <FieldMark profile={profile} path="locations" />
        </div>
        {(profile.locations || []).length ? (
          <div className="chips">
            {profile.locations.map((l, i) => (
              <span key={i} className="chip">
                {label(l, 'label', 'city', 'name') || '–'}
                {l.country ? <span className="muted"> · {l.country}</span> : null}
                {l.kind ? <span className="muted"> · {l.kind}</span> : null}
              </span>
            ))}
          </div>
        ) : (
          <Nothing>No sites were listed on the pages that were read.</Nothing>
        )}
      </Section>

      {/* FR-223: the departmental map, with the head of each unit where the
          site names one. */}
      <Section
        title="Departments and units"
        tip={<HelpTip term="departmental_map" />}
        profile={profile}
        path="structure"
        count={units.length + functions.length || undefined}
      >
        {units.length === 0 && functions.length === 0 ? (
          <Nothing>
            {structure.expected_for_size
              ? 'No departmental map could be built, although a company of this size normally has one described somewhere. Refreshing the profile with a deeper crawl sometimes finds an "about" or "team" page that carries it.'
              : 'This company does not describe an internal organisation on its site, which is usual below about fifty staff.'}
          </Nothing>
        ) : (
          <>
            {units.map((u, i) => (
              <div key={i} className="entry-card">
                <div className="entry-head">
                  <strong>{label(u, 'name', 'text') || 'Unnamed unit'}</strong>
                  {u.head?.name && (
                    <Badge tone="info">
                      {u.head.name}
                      {u.head.role ? ` · ${u.head.role}` : ''}
                    </Badge>
                  )}
                  <div className="spacer" />
                  <Provenance source={u.source} confidence={u.confidence} />
                </div>
                {u.description && <div className="small">{u.description}</div>}
                {(u.functions || []).length > 0 && (
                  <div className="chips" style={{ marginTop: 8 }}>
                    {u.functions.map((f, j) => (
                      <span key={j} className="chip">
                        {typeof f === 'string' ? f : label(f, 'name', 'text')}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}

            {functions.length > 0 && (
              <>
                <div className="tiny muted" style={{ margin: '10px 0 6px' }}>
                  Functions that span the company
                </div>
                <div className="chips">
                  {functions.map((f, i) => (
                    <span key={i} className="chip" title={label(f, 'description') || undefined}>
                      {label(f, 'name', 'text')}
                    </span>
                  ))}
                </div>
              </>
            )}

            {structure.notes && (
              <p className="small muted" style={{ marginTop: 10 }}>
                {structure.notes}
              </p>
            )}
            <div className="tiny muted" style={{ marginTop: 8 }}>
              {structure.heads_known || 0} of {units.length} units name their head.
            </div>
          </>
        )}
      </Section>

      <Section
        title="Key people"
        profile={profile}
        path="key_people"
        count={people.length || undefined}
      >
        {people.length === 0 ? (
          <Nothing>No named people were found on the pages that were read.</Nothing>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Role</th>
                  <th>Unit</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {people.map((p, i) => (
                  <tr key={i}>
                    <td>
                      {p.linkedin_url ? (
                        <a href={p.linkedin_url} target="_blank" rel="noreferrer">
                          {label(p, 'name', 'text')}
                        </a>
                      ) : (
                        label(p, 'name', 'text')
                      )}
                    </td>
                    <td className="small">{p.role || '–'}</td>
                    <td className="small muted">{p.unit || '–'}</td>
                    <td>
                      <Provenance source={p.source} confidence={p.confidence} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <div className="grid grid-2">
        <Section
          title="Reference customers"
          profile={profile}
          path="reference_customers"
          count={references.length || undefined}
        >
          {references.length === 0 ? (
            <Nothing>No customers, case studies or partners were named.</Nothing>
          ) : (
            <div className="chips">
              {references.map((r, i) => (
                <span key={i} className="chip" title={label(r, 'detail') || undefined}>
                  {label(r, 'name', 'text')}
                  {r.kind ? <span className="muted"> · {r.kind}</span> : null}
                </span>
              ))}
            </div>
          )}
        </Section>

        <Section
          title="Technology"
          profile={profile}
          path="tech_stack"
          count={stack.length || undefined}
        >
          {stack.length === 0 ? (
            <Nothing>No technologies were named on the site or in its job adverts.</Nothing>
          ) : (
            <div className="chips">
              {stack.map((t, i) => (
                <span key={i} className="chip">
                  {label(t, 'name', 'text')}
                  {t.category ? <span className="muted"> · {t.category}</span> : null}
                </span>
              ))}
            </div>
          )}
        </Section>
      </div>

      <Section title="In the news" profile={profile} path="news" count={news.length || undefined}>
        {news.length === 0 ? (
          <Nothing>Nothing was read from a newsroom or press page.</Nothing>
        ) : (
          <div className="col" style={{ gap: 8 }}>
            {news.map((n, i) => (
              <div key={i} className="row">
                <span className="small">
                  {n.url ? (
                    <a href={n.url} target="_blank" rel="noreferrer">
                      {n.title}
                    </a>
                  ) : (
                    n.title
                  )}
                </span>
                {n.signal_type && <Badge tone="info">{n.signal_type}</Badge>}
                <div className="spacer" />
                {n.occurred_at && <span className="tiny muted">{formatDate(n.occurred_at)}</span>}
              </div>
            ))}
          </div>
        )}
      </Section>

      {/* NFR-402: the whole provenance table, once, for a reader who wants to
          audit rather than skim. */}
      <Section
        title="Sources and confidence"
        tip={<HelpTip term="provenance" />}
        profile={profile}
        count={sources.length || undefined}
      >
        {provenanceRows.length > 0 && (
          <div className="table-wrap" style={{ marginBottom: 12 }}>
            <table>
              <thead>
                <tr>
                  <th>Field</th>
                  <th>Confidence</th>
                  <th>Read from</th>
                </tr>
              </thead>
              <tbody>
                {provenanceRows.map(([path, conf]) => (
                  <tr key={path}>
                    <td className="field-path">{path}</td>
                    <td className="small">
                      {Math.round(conf * 100)}%
                      {(profile.low_confidence_fields || []).includes(path) && (
                        <span style={{ marginLeft: 6 }}>
                          <Badge tone="warn">low</Badge>
                        </span>
                      )}
                    </td>
                    <td>
                      <Provenance source={profile.field_sources?.[path]} confidence={conf} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {sources.length === 0 ? (
          <Nothing>No page inventory is on record for this profile.</Nothing>
        ) : (
          <div className="col" style={{ gap: 4 }}>
            {sources.slice(0, 25).map((s, i) => (
              <div key={i} className="row small">
                <a href={s.url} target="_blank" rel="noreferrer" className="nowrap">
                  {s.kind || 'page'}
                </a>
                <span className="muted" style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {s.url}
                </span>
                <div className="spacer" />
                <span className="tiny muted nowrap">{formatDate(s.fetched_at)}</span>
              </div>
            ))}
            {sources.length > 25 && (
              <span className="tiny muted">and {sources.length - 25} more pages</span>
            )}
          </div>
        )}
      </Section>
    </div>
  )
}
