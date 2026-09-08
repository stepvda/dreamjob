/**
 * The draft shape behind the directive editor.
 *
 * It mirrors `DirectiveSetPayload` in `dreamjob.pipeline.directives` field for
 * field, including its defaults, so an unsaved draft and a set loaded from the
 * API are the same object to every control on the screen and a save is a plain
 * POST of what the user sees.
 */

/** A new, empty set with exactly the backend's defaults (FR-141). */
export function blankDraft(name = 'My directives') {
  return {
    name,
    persona_id: null,
    job_content: {
      target_titles: [], title_synonyms: [], function_families: [],
      seniority_min: null, seniority_max: null,
      must_have_skills: [], nice_to_have_skills: [],
      industries_include: [], industries_exclude: [],
      management_scope: null, min_direct_reports: null, keywords_to_avoid: [],
    },
    company_type: {
      size_bands: [], stages: [], trajectories: [], ownerships: [],
      include_companies: [], exclude_companies: [],
    },
    location: {
      areas: [], countries: [], regions: [], willing_to_relocate: false,
      relocation_countries: [], home_location: null,
      max_commute_minutes: null, commute_mode: 'car',
    },
    work_arrangement: {
      arrangements: [], min_remote_days: null, employment_types: [],
      fte_percentage_min: null, fte_percentage_max: null, contract_types: [],
      travel_tolerance: 'occasional', max_travel_percent: null,
    },
    compensation: {
      minimum_package: null, currency: 'EUR', period: 'annual',
      equity_treatment: 'ignore', variable_treatment: 'partial',
      variable_share_counted: 0.5, benefits_must_have: [], disclose_in_content: false,
    },
    notes_to_ai: '',
    spontaneous_only: false,
    discretion_mode: false,
    discretion_excluded_companies: [],
    discretion_excluded_contacts: [],
  }
}

/** A stored set (or a /propose result) as an editable draft. */
export function fromServer(set) {
  const b = blankDraft(set.name)
  return {
    ...b,
    ...set,
    job_content: { ...b.job_content, ...(set.job_content || {}) },
    company_type: { ...b.company_type, ...(set.company_type || {}) },
    location: { ...b.location, ...(set.location || {}) },
    work_arrangement: { ...b.work_arrangement, ...(set.work_arrangement || {}) },
    compensation: { ...b.compensation, ...(set.compensation || {}) },
    notes_to_ai: set.notes_to_ai || '',
  }
}

/**
 * Draft -> request body. `job_content.titles` is a computed field the API
 * serialises for source adapters (FR-162); it is read-only, so it is dropped
 * rather than echoed back, and the identity columns are never written.
 */
export function toPayload(d) {
  const { titles, ...job_content } = d.job_content || {}
  return {
    name: d.name,
    persona_id: d.persona_id ?? null,
    job_content,
    company_type: d.company_type,
    location: d.location,
    work_arrangement: d.work_arrangement,
    compensation: d.compensation,
    notes_to_ai: d.notes_to_ai?.trim() ? d.notes_to_ai.trim() : null,
    spontaneous_only: Boolean(d.spontaneous_only),
    discretion_mode: Boolean(d.discretion_mode),
    discretion_excluded_companies: d.discretion_excluded_companies || [],
    discretion_excluded_contacts: d.discretion_excluded_contacts || [],
  }
}
