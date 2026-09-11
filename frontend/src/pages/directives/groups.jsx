/**
 * The five directive groups (FR-142..146).
 *
 * Each card edits one Pydantic group from `dreamjob.pipeline.directives` and
 * uses exactly the field names that model persists, because the whole set is
 * posted back as a `DirectiveSetPayload`. Every option list comes from
 * GET /api/directives/vocabulary so the labels follow the interface language
 * (NFR-501) and no drop-down can offer a value the model would reject.
 */

import { useState } from 'react'

import { api } from '../../api/client'
import { Caution, HelpTip } from '../../components/Help'
import Icon from '../../components/Icon'
import { ChipSelect, Field, formatDuration, formatMoney } from '../../components/ui'
import { Advanced, CompanyList, Group, NumberBox, PlaceAutocomplete, Select, Slider, TitleAutocomplete } from './controls'

/** Vocabulary group -> ChipSelect options. */
export function opts(vocab, group) {
  return (vocab?.groups?.[group] || []).map((o) => ({ value: o.key, label: o.label }))
}

function labelOf(vocab, group, key) {
  return (vocab?.groups?.[group] || []).find((o) => o.key === key)?.label || key
}

const COMMON_COUNTRIES = ['BE', 'NL', 'FR', 'DE', 'LU', 'GB', 'IE', 'ES', 'IT', 'PT', 'DK', 'SE', 'CH', 'AT', 'PL']
const COMMON_BENEFITS = [
  'company_car', 'meal_vouchers', 'hospitalisation_insurance', 'group_pension',
  'net_allowance', 'home_office_allowance', 'training_budget', 'extra_leave', 'bicycle_lease',
]
const CURRENCIES = ['EUR', 'USD', 'GBP', 'CHF', 'SEK', 'DKK', 'PLN']

/* --- FR-142: job content --------------------------------------------------- */

export function JobContentCard({ value, onChange, vocab, locale }) {
  const v = value
  const set = (patch) => onChange(patch)
  const synonymPool = Array.from(new Set([...(v.title_synonyms || [])]))

  return (
    <Group
      icon="vacancy"
      title="Job content"
      tip={<HelpTip term="directive" />}
      defaultOpen
      summary={
        v.target_titles.length
          ? `${v.target_titles.length} titles · ${v.function_families.length} families`
          : 'No titles yet'
      }
    >
      <Field
        label={
          <>
            Target job titles
            <HelpTip title="Title auto-complete">
              Titles come from a bundled catalogue of roles and their synonyms, so a source
              adapter can widen a query without inventing a title that nobody advertises. A
              title the catalogue does not know is still accepted — press Enter to keep it.
            </HelpTip>
          </>
        }
        hint="The single most decisive directive: every source is searched for these."
      >
        <TitleAutocomplete
          locale={locale}
          family={v.function_families?.[0]}
          disabledTitles={v.target_titles}
          onPick={(t) => {
            const canonical = t.canonical
            if (v.target_titles.includes(canonical)) return
            // FR-142: accepted synonyms are kept apart from the titles the job
            // seeker actually chose, so widening a query never invents a role.
            const synonyms = Array.from(
              new Set([...(v.title_synonyms || []), ...(t.synonyms || [])]),
            )
            const families = t.family && !v.function_families.includes(t.family)
              ? [...v.function_families, t.family]
              : v.function_families
            set({
              target_titles: [...v.target_titles, canonical],
              title_synonyms: synonyms,
              function_families: families,
            })
          }}
        />
        {v.target_titles.length > 0 && (
          <div className="chips" style={{ marginTop: 8 }}>
            {v.target_titles.map((t) => (
              <span className="chip on" key={t}>
                {t}
                <button
                  type="button"
                  onClick={() => set({ target_titles: v.target_titles.filter((x) => x !== t) })}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        )}
      </Field>

      <div className="grid grid-2">
        <Select
          label="Seniority from"
          tip={
            <HelpTip title="Seniority range">
              A range, not a single level. The individual-contributor and management tracks
              share ranks where they are genuinely comparable, so “senior to director” means
              what it says rather than excluding principals and leads.
            </HelpTip>
          }
          value={v.seniority_min}
          onChange={(x) => set({ seniority_min: x })}
          options={vocab?.groups?.seniority}
          placeholder="No lower bound"
        />
        <Select
          label="Seniority to"
          value={v.seniority_max}
          onChange={(x) => set({ seniority_max: x })}
          options={vocab?.groups?.seniority}
          placeholder="No upper bound"
        />
      </div>

      <Field label="Must-have skills" hint="Type and press Enter. These filter; the nice-to-haves only score.">
        <ChipSelect
          options={(v.must_have_skills || []).map((s) => ({ value: s, label: s }))}
          value={v.must_have_skills || []}
          onChange={(x) => set({ must_have_skills: x })}
          allowCustom
        />
      </Field>

      <Advanced>
        <Field
          label={
            <>
              Accepted synonyms
              <HelpTip term="title_synonyms" />
            </>
          }
          hint="Picked up from the catalogue when you choose a title. Remove any that would pull in the wrong work."
        >
          <ChipSelect
            options={synonymPool.map((s) => ({ value: s, label: s }))}
            value={v.title_synonyms || []}
            onChange={(x) => set({ title_synonyms: x })}
            allowCustom
          />
        </Field>

        <Field label="Function families">
          <ChipSelect
            options={opts(vocab, 'function_family')}
            value={v.function_families || []}
            onChange={(x) => set({ function_families: x })}
          />
        </Field>

        <div className="grid grid-2">
          <Select
            label="Management scope"
            value={v.management_scope}
            onChange={(x) => set({ management_scope: x })}
            options={vocab?.groups?.management_scope}
          />
          <NumberBox
            label="Minimum direct reports"
            value={v.min_direct_reports}
            onChange={(x) => set({ min_direct_reports: x })}
            range={vocab?.ranges?.min_direct_reports}
            unit="people"
            hint="Leave blank unless team size is a real requirement."
          />
        </div>

        <Field label="Nice-to-have skills">
          <ChipSelect
            options={(v.nice_to_have_skills || []).map((s) => ({ value: s, label: s }))}
            value={v.nice_to_have_skills || []}
            onChange={(x) => set({ nice_to_have_skills: x })}
            allowCustom
          />
        </Field>

        <div className="grid grid-2">
          <Field label="Industries to include">
            <ChipSelect
              options={(v.industries_include || []).map((s) => ({ value: s, label: s }))}
              value={v.industries_include || []}
              onChange={(x) => set({ industries_include: x })}
              allowCustom
            />
          </Field>
          <Field label="Industries to exclude">
            <ChipSelect
              options={(v.industries_exclude || []).map((s) => ({ value: s, label: s }))}
              value={v.industries_exclude || []}
              onChange={(x) => set({ industries_exclude: x })}
              allowCustom
            />
          </Field>
        </div>

        <Field
          label="Keywords to avoid"
          hint="A vacancy containing one of these is dropped before it is ever scored."
        >
          <ChipSelect
            options={(v.keywords_to_avoid || []).map((s) => ({ value: s, label: s }))}
            value={v.keywords_to_avoid || []}
            onChange={(x) => set({ keywords_to_avoid: x })}
            allowCustom
          />
        </Field>
      </Advanced>
    </Group>
  )
}

/* --- FR-143: company type -------------------------------------------------- */

export function CompanyTypeCard({ value, onChange, vocab }) {
  const v = value
  const set = (patch) => onChange(patch)
  const fte = vocab?.size_band_fte || {}

  return (
    <Group
      icon="companies"
      title="Company type"
      summary={
        v.size_bands.length || v.stages.length
          ? `${v.size_bands.length} size bands · ${v.stages.length} stages`
          : 'Any employer'
      }
    >
      <Field
        label={
          <>
            Headcount bands
            <HelpTip term="size_band" />
          </>
        }
        hint={
          v.size_bands.length
            ? v.size_bands
                .map((b) => {
                  const r = fte[b]
                  return `${labelOf(vocab, 'size_band', b)}: ${r ? `${r.min}–${r.max ?? '∞'} FTE` : '?'}`
                })
                .join(' · ')
            : 'Nothing selected means every size is acceptable.'
        }
      >
        <ChipSelect
          options={opts(vocab, 'size_band')}
          value={v.size_bands || []}
          onChange={(x) => set({ size_bands: x })}
        />
      </Field>

      <Field label="Company stage">
        <ChipSelect options={opts(vocab, 'stage')} value={v.stages || []} onChange={(x) => set({ stages: x })} />
      </Field>

      <Advanced>
        <Field
          label={
            <>
              Trajectory
              <HelpTip title="Trajectory">
                Read from five years of filed accounts and recent news, not from what the company
                says about itself. It feeds the ability-to-pay and investment-capacity scores that
                decide whether a speculative opening is plausible.
              </HelpTip>
            </>
          }
        >
          <ChipSelect
            options={opts(vocab, 'trajectory')}
            value={v.trajectories || []}
            onChange={(x) => set({ trajectories: x })}
          />
        </Field>

        <Field label="Ownership">
          <ChipSelect
            options={opts(vocab, 'ownership')}
            value={v.ownerships || []}
            onChange={(x) => set({ ownerships: x })}
          />
        </Field>

        <div className="grid grid-2">
          <Field label="Always include these companies" hint="Searched even if they fail the filters above.">
            <CompanyList value={v.include_companies || []} onChange={(x) => set({ include_companies: x })} />
          </Field>
          <Field
            label="Never search these companies"
            hint="A preference, not a secrecy measure — for that, use discretion mode below."
          >
            <CompanyList value={v.exclude_companies || []} onChange={(x) => set({ exclude_companies: x })} />
          </Field>
        </div>
      </Advanced>
    </Group>
  )
}

/* --- FR-144: location ------------------------------------------------------ */

export function LocationCard({ value, onChange, vocab, locale }) {
  const v = value
  const set = (patch) => onChange(patch)
  const radiusRange = vocab?.ranges?.radius_km

  return (
    <Group
      icon="browser"
      title="Location"
      summary={
        v.areas.length
          ? `${v.areas.length} area${v.areas.length === 1 ? '' : 's'}${v.willing_to_relocate ? ' · will relocate' : ''}`
          : 'Anywhere'
      }
    >
      <Field
        label={
          <>
            Search areas
            <HelpTip title="Areas and radius">
              Each area is a point with a radius. The point comes from a geocoder; the radius is
              what sources with a distance filter are actually given. An area that could not be
              geocoded still works as a text query — it simply has no radius filter.
            </HelpTip>
          </>
        }
      >
        <PlaceAutocomplete
          locale={locale}
          countries={v.countries}
          onPick={(area) =>
            set({
              areas: [
                ...v.areas,
                { radius_km: radiusRange?.default ?? 25, ...area },
              ],
            })
          }
        />
      </Field>

      {v.areas.map((a, i) => (
        <div className="dir-area" key={`${a.label}-${i}`}>
          <div className="row">
            <strong className="small">{a.label}</strong>
            {a.latitude != null ? (
              <span className="tiny muted">
                {a.latitude.toFixed(3)}, {a.longitude.toFixed(3)}
                {a.country_code ? ` · ${a.country_code.toUpperCase()}` : ''}
              </span>
            ) : (
              <span className="badge badge-warn">
                <Icon name="warning" /> not geocoded
              </span>
            )}
            <div className="spacer" />
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              onClick={() => set({ areas: v.areas.filter((_, j) => j !== i) })}
              aria-label={`Remove ${a.label}`}
            >
              <Icon name="x" />
            </button>
          </div>
          <div className="dir-slider-row" style={{ marginTop: 6 }}>
            <input
              type="range"
              min={radiusRange?.min ?? 0}
              max={radiusRange?.max ?? 2000}
              step={radiusRange?.step ?? 5}
              value={a.radius_km ?? 25}
              onChange={(e) =>
                set({
                  areas: v.areas.map((x, j) => (j === i ? { ...x, radius_km: Number(e.target.value) } : x)),
                })
              }
            />
            <span className="dir-slider-val">{Math.round(a.radius_km ?? 25)} km</span>
          </div>
        </div>
      ))}

      <Field label="Countries" hint="ISO two-letter codes. Also narrows the place lookup above.">
        <ChipSelect
          options={COMMON_COUNTRIES.map((c) => ({ value: c, label: c }))}
          value={v.countries || []}
          onChange={(x) => set({ countries: x.map((c) => c.toUpperCase()) })}
          allowCustom
        />
      </Field>

      <Advanced>
        <Field label="Regions" hint="Free labels a source may understand, such as “Flanders”.">
          <ChipSelect
            options={(v.regions || []).map((s) => ({ value: s, label: s }))}
            value={v.regions || []}
            onChange={(x) => set({ regions: x })}
            allowCustom
          />
        </Field>

        <Field label="Relocation">
          <label className="checkline">
            <input
              type="checkbox"
              checked={Boolean(v.willing_to_relocate)}
              onChange={(e) => set({ willing_to_relocate: e.target.checked })}
            />
            I am willing to relocate
          </label>
          {v.willing_to_relocate && (
            <div style={{ marginTop: 8 }}>
              <ChipSelect
                options={COMMON_COUNTRIES.map((c) => ({ value: c, label: c }))}
                value={v.relocation_countries || []}
                onChange={(x) => set({ relocation_countries: x.map((c) => c.toUpperCase()) })}
                allowCustom
              />
            </div>
          )}
        </Field>

        <Field
          label={
            <>
              Home location
              <HelpTip title="Why the system needs this">
                Only to work out commute time. It is never written into a CV or an e-mail, and a
                commute tolerance is treated as a tighter statement than a plain radius — set one
                and the areas above are narrowed to match.
              </HelpTip>
            </>
          }
          hint={v.home_location?.label ? `Currently: ${v.home_location.label}` : 'Not set'}
        >
          <PlaceAutocomplete
            locale={locale}
            countries={v.countries}
            placeholder="Where you commute from…"
            onPick={(area) => set({ home_location: { radius_km: 0, ...area } })}
          />
          {v.home_location && (
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              style={{ alignSelf: 'flex-start', marginTop: 6 }}
              onClick={() => set({ home_location: null })}
            >
              <Icon name="x" /> Clear home location
            </button>
          )}
        </Field>

        <div className="grid grid-2">
          <Slider
            label="Maximum commute"
            tip={
              <HelpTip title="Commute tolerance">
                Converted into a search radius using a documented speed model per mode — not a
                routing engine. It is a filter bound, not a promise about any particular journey.
              </HelpTip>
            }
            value={v.max_commute_minutes}
            onChange={(x) => set({ max_commute_minutes: x })}
            range={vocab?.ranges?.max_commute_minutes}
            format={(x) => `${x} min`}
            nullable
          />
          <Select
            label="Commute mode"
            value={v.commute_mode || 'car'}
            onChange={(x) => set({ commute_mode: x || 'car' })}
            options={vocab?.groups?.commute_mode}
            placeholder="Car"
          />
        </div>

        <CommuteEstimator home={v.home_location} areas={v.areas} vocab={vocab} />
      </Advanced>
    </Group>
  )
}

/** FR-144: door-to-door time from home to an area, per mode. */
function CommuteEstimator({ home, areas, vocab }) {
  const [rows, setRows] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [target, setTarget] = useState(0)

  const geocodedAreas = (areas || []).filter((a) => a.latitude != null)
  const ready = home?.latitude != null && geocodedAreas.length > 0
  const modes = vocab?.commute_modes || []

  async function run() {
    setBusy(true)
    setError(null)
    try {
      const dest = geocodedAreas[Math.min(target, geocodedAreas.length - 1)]
      const results = await Promise.all(
        modes.map((mode) =>
          api.post('/directives/locations/commute', {
            origin: { latitude: home.latitude, longitude: home.longitude },
            destination: { latitude: dest.latitude, longitude: dest.longitude },
            mode,
          }),
        ),
      )
      setRows(results)
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }

  if (!ready) {
    return (
      <p className="small muted" style={{ marginTop: 4 }}>
        Set a geocoded home location and at least one geocoded area to see commute times by mode.
      </p>
    )
  }

  return (
    <div className="dir-area" style={{ marginTop: 4 }}>
      <div className="row row-wrap">
        <span className="small row" style={{ gap: 6 }}>
          <Icon name="clock" /> Commute time by mode
        </span>
        {geocodedAreas.length > 1 && (
          <select
            className="dir-inline-select"
            value={target}
            onChange={(e) => setTarget(Number(e.target.value))}
          >
            {geocodedAreas.map((a, i) => (
              <option key={`${a.label}-${i}`} value={i}>
                {a.label}
              </option>
            ))}
          </select>
        )}
        <div className="spacer" />
        <button type="button" className="btn btn-sm" onClick={run} disabled={busy}>
          {busy ? (
            <span className="spinner" />
          ) : (
            <>
              <Icon name={rows ? 'refresh' : 'clock'} /> {rows ? 'Recalculate' : 'Estimate'}
            </>
          )}
        </button>
      </div>
      {error && <p className="small" style={{ color: 'var(--danger)' }}>{error.message}</p>}
      {rows && (
        <div className="table-wrap" style={{ marginTop: 8 }}>
          <table>
            <thead>
              <tr>
                <th>Mode</th>
                <th>Distance</th>
                <th className="nowrap">
                  <Icon name="clock" /> Door to door
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.mode}>
                  <td>{labelOf(vocab, 'commute_mode', r.mode)}</td>
                  <td className="nowrap">{r.distance_km} km</td>
                  <td className="nowrap">{formatDuration(r.minutes * 60)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {rows?.[0]?.model && <p className="tiny muted" style={{ marginTop: 6 }}>Model: {rows[0].model}.</p>}
    </div>
  )
}

/* --- FR-145: work arrangement ---------------------------------------------- */

export function WorkArrangementCard({ value, onChange, vocab }) {
  const v = value
  const set = (patch) => onChange(patch)
  const ceiling =
    v.max_travel_percent != null
      ? v.max_travel_percent
      : { none: 0, occasional: 10, regular: 25, frequent: 50, extensive: 100 }[v.travel_tolerance] ?? 10

  return (
    <Group
      icon="calendar"
      title="Work arrangement"
      summary={v.arrangements.length ? v.arrangements.map((a) => labelOf(vocab, 'work_arrangement', a)).join(', ') : 'Any'}
    >
      <Field label="On-site, hybrid or remote">
        <ChipSelect
          options={opts(vocab, 'work_arrangement')}
          value={v.arrangements || []}
          onChange={(x) => set({ arrangements: x })}
        />
      </Field>

      <Slider
        label="Minimum remote days per week"
        value={v.min_remote_days}
        onChange={(x) => set({ min_remote_days: x })}
        range={vocab?.ranges?.min_remote_days}
        format={(x) => `${x} day${x === 1 ? '' : 's'}`}
        nullable
      />

      <div className="grid grid-2">
        <Field label="Employment type">
          <ChipSelect
            options={opts(vocab, 'employment_type')}
            value={v.employment_types || []}
            onChange={(x) => set({ employment_types: x })}
          />
        </Field>
        <Field label="Contract type">
          <ChipSelect
            options={opts(vocab, 'contract_type')}
            value={v.contract_types || []}
            onChange={(x) => set({ contract_types: x })}
          />
        </Field>
      </div>

      <Advanced>
        <div className="grid grid-2">
          <Slider
            label="Minimum FTE"
            value={v.fte_percentage_min}
            onChange={(x) => set({ fte_percentage_min: x })}
            range={vocab?.ranges?.fte_percentage}
            format={(x) => `${x}%`}
            nullable
          />
          <Slider
            label="Maximum FTE"
            value={v.fte_percentage_max}
            onChange={(x) => set({ fte_percentage_max: x })}
            range={vocab?.ranges?.fte_percentage}
            format={(x) => `${x}%`}
            nullable
          />
        </div>

        <Select
          label="Travel tolerance"
          tip={
            <HelpTip title="Travel tolerance">
              A band rather than a number, because that is how vacancies describe it. Each band
              implies a ceiling — occasional means up to 10% of your time. Override it below when
              a vacancy-stated percentage matters more than the band.
            </HelpTip>
          }
          value={v.travel_tolerance || 'occasional'}
          onChange={(x) => set({ travel_tolerance: x || 'occasional' })}
          options={vocab?.groups?.travel_tolerance}
          placeholder="Occasional"
          hint={`Current ceiling: ${ceiling}% of your time.`}
        />

        <Slider
          label="Override the travel ceiling"
          value={v.max_travel_percent}
          onChange={(x) => set({ max_travel_percent: x })}
          range={vocab?.ranges?.max_travel_percent}
          format={(x) => `${x}%`}
          nullable
        />
      </Advanced>
    </Group>
  )
}

/* --- FR-146: compensation --------------------------------------------------- */

export function CompensationCard({ value, onChange, vocab }) {
  const v = value
  const set = (patch) => onChange(patch)
  const [disclosureAck, setDisclosureAck] = useState(false)

  return (
    <Group
      icon="money"
      title="Compensation"
      tip={<HelpTip term="compensation_disclosure" />}
      badge={
        v.disclose_in_content ? (
          <span className="badge badge-warn">
            <Icon name="eye" /> disclosed in content
          </span>
        ) : (
          <span className="badge badge-ok">
            <Icon name="lock" /> not disclosed
          </span>
        )
      }
      summary={
        v.minimum_package
          ? `${formatMoney(v.minimum_package, v.currency)} ${v.period}`
          : 'No minimum set'
      }
    >
      <div className="grid grid-3">
        <Field label="Minimum package">
          <input
            type="number"
            min={0}
            step={500}
            value={v.minimum_package ?? ''}
            placeholder="No minimum"
            onChange={(e) => set({ minimum_package: e.target.value === '' ? null : Number(e.target.value) })}
          />
        </Field>
        <Field label="Currency">
          <select value={v.currency || 'EUR'} onChange={(e) => set({ currency: e.target.value })}>
            {CURRENCIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </Field>
        <Select
          label="Period"
          value={v.period || 'annual'}
          onChange={(x) => set({ period: x || 'annual' })}
          options={vocab?.groups?.compensation_period}
          placeholder="Annual"
        />
      </div>

      {/* FR-146: compensation is for filtering and scoring only. The opt-in is
          explicit, defaults to off, and every generator checks it before a
          figure can appear in a CV, a letter or an e-mail. */}
      <Caution
        title="Salary expectations stay out of what you send — unless you say otherwise"
        acknowledge={v.disclose_in_content ? undefined : 'I understand what disclosing means'}
        acknowledged={disclosureAck || v.disclose_in_content}
        onAcknowledge={() => setDisclosureAck(true)}
      >
        The figures above filter and score opportunities. They are never written into a
        generated CV, motivation document or introduction e-mail while this is off. Turning it
        on lets the AI state your expectation in outgoing content — which narrows the
        negotiation before it has begun, and is why it is off by default.
      </Caution>

      <div className="checkline" style={{ marginTop: 12 }}>
        <input
          id="dir-disclose-compensation"
          type="checkbox"
          checked={Boolean(v.disclose_in_content)}
          disabled={!disclosureAck && !v.disclose_in_content}
          onChange={(e) => {
            // Turning it back off must not lock the control: the acknowledgement
            // gates the first opt-in, not every later change of mind.
            if (!e.target.checked) setDisclosureAck(true)
            set({ disclose_in_content: e.target.checked })
          }}
        />
        <label htmlFor="dir-disclose-compensation">
          Disclose my compensation expectation in generated content
        </label>
        <HelpTip term="do_not_disclose" align="right" />
      </div>

      <Advanced>
        <div className="grid grid-2">
          <Select
            label="How equity counts"
            tip={
              <HelpTip title="Counting equity and variable pay">
                “Ignore” leaves it out of the package total, “partial” counts the share you set
                below, “full” counts all of it, and “required” drops any role that offers none.
              </HelpTip>
            }
            value={v.equity_treatment || 'ignore'}
            onChange={(x) => set({ equity_treatment: x || 'ignore' })}
            options={vocab?.groups?.pay_component_treatment}
            placeholder="Ignore"
          />
          <Select
            label="How variable pay counts"
            value={v.variable_treatment || 'partial'}
            onChange={(x) => set({ variable_treatment: x || 'partial' })}
            options={vocab?.groups?.pay_component_treatment}
            placeholder="Partial"
          />
        </div>

        <Slider
          label="Share of variable pay counted"
          value={v.variable_share_counted ?? 0.5}
          onChange={(x) => set({ variable_share_counted: x })}
          range={vocab?.ranges?.variable_share_counted}
          format={(x) => `${Math.round(x * 100)}%`}
          hint="Applied when variable pay is counted partially."
        />

        <Field label="Benefits you will not go without">
          <ChipSelect
            options={COMMON_BENEFITS.map((b) => ({ value: b, label: b.replace(/_/g, ' ') }))}
            value={v.benefits_must_have || []}
            onChange={(x) => set({ benefits_must_have: x })}
            allowCustom
          />
        </Field>

      </Advanced>
    </Group>
  )
}
