-- ===========================================================================
-- Dream Job - initial schema (CR-408: SQLite, versioned migrations)
--
-- Two data domains live in one file-level database but are kept strictly
-- apart by convention and by foreign keys (FR-101, FR-341, FR-344):
--
--   PRIVATE  tables carry a job_seeker_id column and are never readable
--            across job seekers.  Every private query MUST filter on it.
--   SHARED   tables form the knowledge base.  They carry NO link back to
--            the job seeker whose campaign produced them (FR-344).
--
-- Freshness metadata (collected_at / source / access_method) is present on
-- every shared record so the planner can decide reuse vs. re-collection
-- (FR-342, FR-343).
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- PRIVATE: job seeker, authentication, consent
-- ---------------------------------------------------------------------------
CREATE TABLE job_seeker (
    id                  TEXT PRIMARY KEY,
    email               TEXT NOT NULL UNIQUE,
    display_name        TEXT NOT NULL,
    password_hash       TEXT,                    -- argon2id (NFR-202)
    totp_secret_enc     BLOB,                    -- encrypted MFA secret
    webauthn_credentials TEXT,                   -- JSON array of passkeys (NFR-202)
    locale              TEXT NOT NULL DEFAULT 'en',
    is_admin            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE session (
    id              TEXT PRIMARY KEY,
    job_seeker_id   TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    token_hash      TEXT NOT NULL UNIQUE,
    client_binding  TEXT,                        -- UA+IP fingerprint (NFR-202)
    expires_at      TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_session_seeker ON session(job_seeker_id);

-- CR-410: consent for transferring profile data to a non-EU LLM provider.
CREATE TABLE consent_record (
    id              TEXT PRIMARY KEY,
    job_seeker_id   TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,               -- llm_transfer | linkedin_automation | enrichment
    granted         INTEGER NOT NULL,
    detail          TEXT,                        -- what exactly was acknowledged
    granted_at      TEXT NOT NULL
);
CREATE INDEX idx_consent_seeker ON consent_record(job_seeker_id, kind);

-- ---------------------------------------------------------------------------
-- PRIVATE: persona (FR-442)
-- ---------------------------------------------------------------------------
CREATE TABLE persona (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    emphasis            TEXT,                    -- how the profile is slanted
    dream_job_statement TEXT,
    directive_defaults  TEXT,                    -- JSON
    is_default          INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_persona_seeker ON persona(job_seeker_id);

-- ---------------------------------------------------------------------------
-- PRIVATE: profile and versions (FR-102..109)
-- ---------------------------------------------------------------------------
CREATE TABLE profile_version (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    persona_id          TEXT REFERENCES persona(id) ON DELETE SET NULL,
    version             INTEGER NOT NULL,
    -- LinkedIn export section structure defines the base schema (FR-102)
    sections            TEXT NOT NULL,           -- JSON: summary/experience/education/...
    dream_job_statement TEXT,                    -- FR-109, no length limit
    photo_path          TEXT,                    -- extracted from CV/LinkedIn
    source_note         TEXT,                    -- linkedin_pdf | cv | manual | merged
    created_at          TEXT NOT NULL,
    UNIQUE (job_seeker_id, version)
);
CREATE INDEX idx_profile_seeker ON profile_version(job_seeker_id);

-- FR-106: fields the job seeker never wants disclosed in generated content.
CREATE TABLE disclosure_flag (
    id              TEXT PRIMARY KEY,
    job_seeker_id   TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    field_path      TEXT NOT NULL,               -- JSON pointer into profile sections
    do_not_disclose INTEGER NOT NULL DEFAULT 1,
    reason          TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (job_seeker_id, field_path)
);

-- FR-103: conflicts between LinkedIn export and CV, for the user to resolve.
CREATE TABLE profile_conflict (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    profile_version_id  TEXT REFERENCES profile_version(id) ON DELETE CASCADE,
    field_path          TEXT NOT NULL,
    value_linkedin      TEXT,
    value_cv            TEXT,
    resolution          TEXT,                    -- linkedin | cv | manual | unresolved
    resolved_value      TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_conflict_seeker ON profile_conflict(job_seeker_id, resolution);

-- FR-107: normalised skill taxonomy with proficiency and recency.
CREATE TABLE profile_skill (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    profile_version_id  TEXT NOT NULL REFERENCES profile_version(id) ON DELETE CASCADE,
    raw_label           TEXT NOT NULL,
    normalised_label    TEXT NOT NULL,
    taxonomy            TEXT NOT NULL DEFAULT 'esco',
    taxonomy_code       TEXT,
    proficiency         INTEGER,                 -- 1..5
    years_experience    REAL,
    last_used_year      INTEGER,
    evidence_refs       TEXT                     -- JSON array of evidence_item ids
);
CREATE INDEX idx_skill_seeker ON profile_skill(job_seeker_id, normalised_label);

-- FR-441: evidence attached to skills and achievements.
CREATE TABLE evidence_item (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL,           -- repository|publication|talk|case_study|article|reference|certificate
    title               TEXT NOT NULL,
    url                 TEXT,
    file_path           TEXT,
    description         TEXT,
    linked_skills       TEXT,                    -- JSON array of normalised skill labels
    linked_achievements TEXT,                    -- JSON array of free-text achievement keys
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_evidence_seeker ON evidence_item(job_seeker_id);

-- ---------------------------------------------------------------------------
-- PRIVATE: enrichment, composite profile, dream job model (FR-121..128)
-- ---------------------------------------------------------------------------
CREATE TABLE enrichment_finding (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    url                 TEXT NOT NULL,
    title               TEXT,
    extracted_facts     TEXT,                    -- JSON
    identity_signals    TEXT,                    -- JSON: which signals corroborated (FR-123)
    identity_score      REAL NOT NULL DEFAULT 0,
    classification      TEXT NOT NULL,           -- confirmed | probable | doubtful
    status              TEXT NOT NULL DEFAULT 'pending', -- pending|accepted|rejected
    rejected_permanently INTEGER NOT NULL DEFAULT 0,     -- FR-124: never re-propose
    created_at          TEXT NOT NULL,
    UNIQUE (job_seeker_id, url)
);
CREATE INDEX idx_enrich_seeker ON enrichment_finding(job_seeker_id, status);

CREATE TABLE composite_profile (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    persona_id          TEXT REFERENCES persona(id) ON DELETE SET NULL,
    profile_version_id  TEXT NOT NULL REFERENCES profile_version(id) ON DELETE CASCADE,
    version             INTEGER NOT NULL,
    narrative           TEXT,
    -- JSON blocks, each statement carrying a provenance ref (FR-125)
    career_trajectory   TEXT,
    core_competencies   TEXT,
    adjacent_competencies TEXT,
    seniority           TEXT,
    domains             TEXT,
    achievements        TEXT,
    public_footprint    TEXT,
    inferred_preferences TEXT,
    constraints         TEXT,
    evidence_refs       TEXT,                    -- JSON: statement -> source
    edited_by_user      INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_composite_seeker ON composite_profile(job_seeker_id, version);

CREATE TABLE dream_job_model (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    persona_id          TEXT REFERENCES persona(id) ON DELETE SET NULL,
    version             INTEGER NOT NULL,
    statement           TEXT NOT NULL,           -- the raw FR-109 text
    target_roles        TEXT,                    -- JSON
    role_families       TEXT,
    responsibilities    TEXT,
    company_characteristics TEXT,
    culture_values      TEXT,
    deal_breakers       TEXT,
    implicit_preferences TEXT,
    confirmed_by_user   INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_dream_seeker ON dream_job_model(job_seeker_id, version);

-- ---------------------------------------------------------------------------
-- PRIVATE: directives and campaigns (FR-141..166)
-- ---------------------------------------------------------------------------
CREATE TABLE directive_set (
    id              TEXT PRIMARY KEY,
    job_seeker_id   TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    persona_id      TEXT REFERENCES persona(id) ON DELETE SET NULL,
    name            TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    job_content     TEXT,                        -- JSON (FR-142)
    company_type    TEXT,                        -- JSON (FR-143)
    location        TEXT,                        -- JSON (FR-144)
    work_arrangement TEXT,                       -- JSON (FR-145)
    compensation    TEXT,                        -- JSON (FR-146)
    notes_to_ai     TEXT,                        -- the only free-text field (FR-141)
    spontaneous_only INTEGER NOT NULL DEFAULT 0, -- FR-149
    -- FR-385 discretion mode
    discretion_mode          INTEGER NOT NULL DEFAULT 0,
    discretion_excluded_companies TEXT,          -- JSON array
    discretion_excluded_contacts  TEXT,          -- JSON array
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_directive_seeker ON directive_set(job_seeker_id);

CREATE TABLE campaign (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    persona_id          TEXT REFERENCES persona(id) ON DELETE SET NULL,
    directive_set_id    TEXT NOT NULL REFERENCES directive_set(id),
    profile_version_id  TEXT NOT NULL REFERENCES profile_version(id),   -- FR-105
    composite_profile_id TEXT REFERENCES composite_profile(id),
    dream_job_model_id  TEXT REFERENCES dream_job_model(id),
    name                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'draft',  -- draft|planned|running|paused|completed|cancelled|failed
    stage               TEXT,                    -- current pipeline stage name
    token_budget        INTEGER NOT NULL DEFAULT 2000000,
    tokens_used         INTEGER NOT NULL DEFAULT 0,
    cost_eur            REAL NOT NULL DEFAULT 0,
    caps                TEXT,                    -- JSON: max pages/companies/people/duration (FR-186)
    reuse_report        TEXT,                    -- JSON: what the KB saved (FR-342)
    started_at          TEXT,
    finished_at         TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_campaign_seeker ON campaign(job_seeker_id, status);

CREATE TABLE source_plan_item (
    id              TEXT PRIMARY KEY,
    campaign_id     TEXT NOT NULL REFERENCES campaign(id) ON DELETE CASCADE,
    adapter_key     TEXT NOT NULL,
    native_query    TEXT,                        -- JSON, source's own query form (FR-162)
    rationale       TEXT,
    caps            TEXT,                        -- JSON
    estimated_pages INTEGER,
    estimated_seconds INTEGER,
    estimated_cost_eur REAL,
    excluded_by_user INTEGER NOT NULL DEFAULT 0, -- FR-163
    status          TEXT NOT NULL DEFAULT 'planned', -- planned|running|done|failed|skipped
    records_collected INTEGER NOT NULL DEFAULT 0,
    error_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_plan_campaign ON source_plan_item(campaign_id, status);

-- ---------------------------------------------------------------------------
-- SHARED: knowledge base - companies (FR-221..226, DR-101)
-- ---------------------------------------------------------------------------
CREATE TABLE company (
    id                  TEXT PRIMARY KEY,
    -- DR-101 keys, in priority order
    legal_id            TEXT,                    -- KBO/BCE, CH number, LEI
    legal_id_type       TEXT,
    vat_number          TEXT,
    domain              TEXT,
    normalised_name     TEXT NOT NULL,
    name                TEXT NOT NULL,
    country             TEXT,
    jurisdiction        TEXT,                    -- BE|NL|UK|US|FR|DE...
    business_summary    TEXT,
    products_services   TEXT,                    -- JSON
    markets             TEXT,                    -- JSON
    sector_codes        TEXT,                    -- JSON: NACE/SIC
    size_fte            INTEGER,
    size_band           TEXT,
    stage               TEXT,                    -- startup|scaleup|established|listed|public|nonprofit
    ownership           TEXT,                    -- founder_led|pe_backed|subsidiary|cooperative
    trajectory          TEXT,                    -- growing|stable|declining|restructuring|volatile
    locations           TEXT,                    -- JSON
    structure           TEXT,                    -- JSON departmental map (FR-223)
    key_people          TEXT,                    -- JSON
    reference_customers TEXT,                    -- JSON
    tech_stack          TEXT,                    -- JSON
    values_culture      TEXT,                    -- JSON (FR-384)
    careers_url         TEXT,
    ats_vendor          TEXT,                    -- greenhouse|lever|workday|...
    ats_slug            TEXT,
    news                TEXT,                    -- JSON
    -- freshness (FR-343)
    collected_at        TEXT NOT NULL,
    refreshed_at        TEXT,
    source              TEXT,
    access_method       TEXT NOT NULL DEFAULT 'http',  -- http|api|browser|manual (FR-207)
    confidence          REAL NOT NULL DEFAULT 0.5,
    UNIQUE (legal_id, legal_id_type)
);
CREATE INDEX idx_company_domain ON company(domain);
CREATE INDEX idx_company_nname ON company(normalised_name);
CREATE INDEX idx_company_country ON company(country, size_band);

CREATE TABLE competitor_link (
    id              TEXT PRIMARY KEY,
    company_id      TEXT NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    peer_company_id TEXT REFERENCES company(id) ON DELETE CASCADE,
    peer_name       TEXT,                        -- when the peer is not yet profiled
    basis           TEXT NOT NULL,               -- sector|customers|press|product|directory|linkedin
    strength        REAL NOT NULL DEFAULT 0.5,
    collected_at    TEXT NOT NULL,
    UNIQUE (company_id, peer_company_id, basis)
);
CREATE INDEX idx_competitor_company ON competitor_link(company_id);

-- FR-225 / FR-402: hiring and timing signals.
CREATE TABLE hiring_signal (
    id              TEXT PRIMARY KEY,
    company_id      TEXT NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    signal_type     TEXT NOT NULL,               -- headcount_growth|new_office|funding|product_launch|postings|reorg|leadership_change|fiscal_year_start|competitor_layoff
    description     TEXT,
    occurred_at     TEXT,
    source_url      TEXT,
    strength        REAL NOT NULL DEFAULT 0.5,
    collected_at    TEXT NOT NULL
);
CREATE INDEX idx_signal_company ON hiring_signal(company_id, occurred_at);

-- ---------------------------------------------------------------------------
-- SHARED: financial analysis (FR-241..246, DR-103)
-- ---------------------------------------------------------------------------
CREATE TABLE financial_year (
    id                  TEXT PRIMARY KEY,
    company_id          TEXT NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    fiscal_year         INTEGER NOT NULL,
    period_end          TEXT,
    currency            TEXT NOT NULL DEFAULT 'EUR',
    reporting_standard  TEXT,                    -- BE-GAAP|IFRS|UK-GAAP|US-GAAP
    fx_rate_to_eur      REAL,
    fx_date             TEXT,
    revenue             REAL,
    gross_margin        REAL,
    ebit                REAL,
    ebitda              REAL,
    net_result          REAL,
    equity              REAL,
    cash                REAL,
    total_debt          REAL,
    headcount_fte       REAL,
    personnel_costs     REAL,
    capex               REAL,
    is_estimated        INTEGER NOT NULL DEFAULT 0,   -- FR-245
    reconciliation_flags TEXT,                   -- JSON (NFR-404)
    filing_document_id  TEXT,                    -- -> raw_document.id
    source              TEXT,
    collected_at        TEXT NOT NULL,
    UNIQUE (company_id, fiscal_year)
);
CREATE INDEX idx_finyear_company ON financial_year(company_id, fiscal_year);

CREATE TABLE financial_analysis (
    id                      TEXT PRIMARY KEY,
    company_id              TEXT NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    years_covered           TEXT,                -- JSON array
    revenue_cagr            REAL,
    headcount_cagr          REAL,
    margin_trend            TEXT,
    personnel_cost_per_fte  REAL,
    current_ratio           REAL,
    solvency_ratio          REAL,
    trajectory              TEXT,                -- growing|stable|declining|volatile
    trajectory_confidence   REAL,
    ability_to_pay          INTEGER,             -- 0..100 (FR-244)
    ability_to_pay_rationale TEXT,
    investment_capacity     INTEGER,             -- 0..100
    investment_capacity_rationale TEXT,
    is_estimated            INTEGER NOT NULL DEFAULT 0,
    computed_at             TEXT NOT NULL,
    UNIQUE (company_id)
);

-- ---------------------------------------------------------------------------
-- SHARED: vacancies and raw documents (FR-261, DR-102)
-- ---------------------------------------------------------------------------
CREATE TABLE vacancy (
    id                  TEXT PRIMARY KEY,
    company_id          TEXT REFERENCES company(id) ON DELETE SET NULL,
    company_name_raw    TEXT,
    title               TEXT NOT NULL,
    function_family     TEXT,
    seniority           TEXT,
    description         TEXT,
    required_skills     TEXT,                    -- JSON
    desirable_skills    TEXT,                    -- JSON
    location            TEXT,
    country             TEXT,
    latitude            REAL,
    longitude           REAL,
    work_arrangement    TEXT,                    -- onsite|hybrid|remote
    remote_days         INTEGER,
    contract_type       TEXT,                    -- permanent|fixed_term|freelance|interim
    fte_percentage      INTEGER,
    salary_min          REAL,
    salary_max          REAL,
    salary_currency     TEXT,
    posted_at           TEXT,
    application_channel TEXT,                    -- email|ats_form|url
    application_target  TEXT,
    source_url          TEXT,
    source_adapter      TEXT,
    raw_document_id     TEXT,
    dedup_key           TEXT,                    -- FR-184 fuzzy key
    language            TEXT,
    collected_at        TEXT NOT NULL,
    access_method       TEXT NOT NULL DEFAULT 'http',
    confidence          REAL NOT NULL DEFAULT 0.7
);
CREATE INDEX idx_vacancy_company ON vacancy(company_id);
CREATE INDEX idx_vacancy_dedup ON vacancy(dedup_key);
CREATE INDEX idx_vacancy_collected ON vacancy(collected_at);

CREATE TABLE raw_document (
    id              TEXT PRIMARY KEY,
    url             TEXT,
    content_type    TEXT,
    content_hash    TEXT NOT NULL,
    storage_path    TEXT NOT NULL,               -- on disk, referenced from DB
    byte_size       INTEGER,
    http_status     INTEGER,
    access_method   TEXT NOT NULL DEFAULT 'http',
    fetched_at      TEXT NOT NULL,
    UNIQUE (content_hash)
);
CREATE INDEX idx_rawdoc_url ON raw_document(url, fetched_at);

-- Provenance: which plan item produced which record (FR-166).
CREATE TABLE provenance (
    id                  TEXT PRIMARY KEY,
    entity_type         TEXT NOT NULL,           -- company|vacancy|contact|financial_year|...
    entity_id           TEXT NOT NULL,
    source_plan_item_id TEXT,
    raw_document_id     TEXT REFERENCES raw_document(id) ON DELETE SET NULL,
    adapter_key         TEXT,
    field_path          TEXT,                    -- NFR-402: per-field provenance
    confidence          REAL,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_prov_entity ON provenance(entity_type, entity_id);

-- FR-182: HTTP cache so repeat campaigns do not re-fetch unchanged pages.
CREATE TABLE http_cache (
    url_hash        TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    status_code     INTEGER,
    headers         TEXT,
    body_path       TEXT,
    etag            TEXT,
    last_modified   TEXT,
    fetched_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);

CREATE TABLE robots_cache (
    domain      TEXT PRIMARY KEY,
    body        TEXT,
    fetched_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- SHARED (restricted): contacts (FR-301..306, NFR-302, NFR-303)
-- ---------------------------------------------------------------------------
CREATE TABLE contact (
    id                  TEXT PRIMARY KEY,
    company_id          TEXT REFERENCES company(id) ON DELETE CASCADE,
    full_name           TEXT,
    role_title          TEXT,
    department          TEXT,
    email               TEXT,
    email_source_method TEXT,                    -- website|press|pattern_inference|lookup_service (FR-303)
    email_validation    TEXT,                    -- valid|risky|invalid|unknown (FR-304)
    email_validation_detail TEXT,                -- JSON: syntax/mx/disposable/role/smtp/catch_all
    email_validated_at  TEXT,
    is_generic_mailbox  INTEGER NOT NULL DEFAULT 0,
    linkedin_url        TEXT,
    source              TEXT,
    access_method       TEXT NOT NULL DEFAULT 'http',
    -- NFR-303: browser-collected contacts are campaign-scoped, not shared.
    shareable           INTEGER NOT NULL DEFAULT 1,
    owning_campaign_id  TEXT,
    retention_until     TEXT,
    objected            INTEGER NOT NULL DEFAULT 0,   -- NFR-302: permanent block
    objected_at         TEXT,
    collected_at        TEXT NOT NULL,
    confidence          REAL NOT NULL DEFAULT 0.5
);
CREATE INDEX idx_contact_company ON contact(company_id);
CREATE INDEX idx_contact_email ON contact(email);

-- FR-305: validation cache, keyed on address, to avoid repeated SMTP probes.
CREATE TABLE email_validation_cache (
    email           TEXT PRIMARY KEY,
    result          TEXT NOT NULL,
    detail          TEXT,
    checked_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- PRIVATE: opportunities, scoring (FR-261..285, FR-383)
-- ---------------------------------------------------------------------------
CREATE TABLE opportunity (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    campaign_id         TEXT NOT NULL REFERENCES campaign(id) ON DELETE CASCADE,
    company_id          TEXT REFERENCES company(id) ON DELETE SET NULL,
    vacancy_id          TEXT REFERENCES vacancy(id) ON DELETE SET NULL,
    kind                TEXT NOT NULL,           -- vacancy | speculative (FR-263)
    title               TEXT NOT NULL,
    function_family     TEXT,
    seniority           TEXT,
    description         TEXT,
    speculative_rationale TEXT,                  -- FR-262
    plausibility        REAL,                    -- FR-262
    -- FR-264 compensation estimate
    comp_min            REAL,
    comp_max            REAL,
    comp_currency       TEXT,
    comp_confidence     REAL,
    comp_sources        TEXT,                    -- JSON
    employer_rating     REAL,                    -- FR-265
    employer_review_themes TEXT,                 -- JSON
    -- FR-281 sub-scores
    score               REAL,
    score_profile_fit   REAL,
    score_dream_fit     REAL,
    score_directive_fit REAL,
    score_company       REAL,
    score_compensation  REAL,
    score_plausibility  REAL,
    score_reachability  REAL,
    rationale           TEXT,                    -- FR-282
    dream_fit_detail    TEXT,                    -- JSON: met/partial/violated (FR-383)
    -- user control (FR-284)
    manual_rank         INTEGER,
    pinned              INTEGER NOT NULL DEFAULT 0,
    selected            INTEGER NOT NULL DEFAULT 0,
    user_status         TEXT NOT NULL DEFAULT 'new',  -- new|interested|not_interested|applied
    not_interested_reason TEXT,
    tags                TEXT,                    -- JSON: destination|stepping_stone|... (FR-382)
    timing_flag         TEXT,                    -- favourable window (FR-402)
    language            TEXT,                    -- language for generated content
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX idx_opp_seeker ON opportunity(job_seeker_id, campaign_id);
CREATE INDEX idx_opp_score ON opportunity(campaign_id, score DESC);

-- FR-285: feedback used to re-tune weights.
CREATE TABLE scoring_weights (
    id              TEXT PRIMARY KEY,
    job_seeker_id   TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    weights         TEXT NOT NULL,               -- JSON
    learned_from    TEXT,                        -- JSON explanation (FR-425)
    updated_at      TEXT NOT NULL,
    UNIQUE (job_seeker_id)
);

-- ---------------------------------------------------------------------------
-- PRIVATE: introduction paths (FR-302, FR-461)
-- ---------------------------------------------------------------------------
CREATE TABLE introduction_path (
    id              TEXT PRIMARY KEY,
    job_seeker_id   TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    opportunity_id  TEXT REFERENCES opportunity(id) ON DELETE CASCADE,
    company_id      TEXT REFERENCES company(id) ON DELETE SET NULL,
    target_contact_id TEXT REFERENCES contact(id) ON DELETE SET NULL,
    intermediary_name TEXT,
    intermediary_role TEXT,
    intermediary_linkedin TEXT,
    relationship    TEXT,                        -- first_degree|alumni|former_colleague|community
    degree          INTEGER,
    strength        REAL NOT NULL DEFAULT 0.5,
    message_draft   TEXT,                        -- FR-461
    status          TEXT NOT NULL DEFAULT 'proposed',
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_intro_seeker ON introduction_path(job_seeker_id, opportunity_id);

-- ---------------------------------------------------------------------------
-- PRIVATE: application packages and dispatch (FR-321..331, NFR-702)
-- ---------------------------------------------------------------------------
CREATE TABLE application_package (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    opportunity_id      TEXT NOT NULL REFERENCES opportunity(id) ON DELETE CASCADE,
    contact_id          TEXT REFERENCES contact(id) ON DELETE SET NULL,
    language            TEXT NOT NULL DEFAULT 'en',
    cv_template         TEXT,
    cv_docx_path        TEXT,
    cv_pdf_path         TEXT,
    briefing_pdf_path   TEXT,                    -- FR-329
    motivation_pdf_path TEXT,                    -- FR-330
    email_subject       TEXT,
    email_body          TEXT,
    -- FR-322 factual-consistency check
    consistency_status  TEXT,                    -- pass|fail|not_run
    consistency_report  TEXT,                    -- JSON
    leak_scan_status    TEXT,                    -- NFR-206
    status              TEXT NOT NULL DEFAULT 'draft', -- draft|approved|discarded|sent
    approved_at         TEXT,
    approved_by         TEXT,                    -- NFR-702 audit
    profile_version_id  TEXT,                    -- FR-331 versions used
    company_snapshot_at TEXT,
    generation_notes    TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX idx_pkg_seeker ON application_package(job_seeker_id, status);

CREATE TABLE dispatch (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    application_package_id TEXT NOT NULL REFERENCES application_package(id) ON DELETE CASCADE,
    backend             TEXT NOT NULL,           -- gmail_oauth | resend
    recipient_email     TEXT NOT NULL,
    recipient_name      TEXT,
    subject             TEXT,
    message_id          TEXT,
    thread_id           TEXT,
    attachments         TEXT,                    -- JSON list of paths
    sent_at             TEXT,
    delivery_status     TEXT NOT NULL DEFAULT 'queued', -- queued|sent|delivered|bounced|failed
    delivery_detail     TEXT,
    bounce_detected_at  TEXT,
    reply_detected_at   TEXT,
    follow_up_due_at    TEXT,                    -- FR-327
    follow_up_sent_at   TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_dispatch_seeker ON dispatch(job_seeker_id, delivery_status);

CREATE TABLE mail_account (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    backend             TEXT NOT NULL,           -- gmail_oauth | resend
    address             TEXT NOT NULL,
    display_name        TEXT,
    -- NFR-204: encrypted, scoped, revocable
    credentials_enc     BLOB,
    scopes              TEXT,
    token_expires_at    TEXT,
    is_active           INTEGER NOT NULL DEFAULT 1,
    connected_at        TEXT NOT NULL,
    UNIQUE (job_seeker_id, backend, address)
);

CREATE TABLE incoming_reply (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    dispatch_id         TEXT REFERENCES dispatch(id) ON DELETE SET NULL,
    from_address        TEXT,
    subject             TEXT,
    body                TEXT,
    received_at         TEXT,
    message_id          TEXT,
    in_reply_to         TEXT,
    classification      TEXT,                    -- FR-422
    classification_confidence REAL,
    extracted_slots     TEXT,                    -- JSON (FR-423)
    draft_response      TEXT,
    handled             INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_reply_seeker ON incoming_reply(job_seeker_id, handled);

-- ---------------------------------------------------------------------------
-- PRIVATE: post-application pipeline (FR-421..425)
-- ---------------------------------------------------------------------------
CREATE TABLE pipeline_card (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    opportunity_id      TEXT NOT NULL REFERENCES opportunity(id) ON DELETE CASCADE,
    application_package_id TEXT REFERENCES application_package(id) ON DELETE SET NULL,
    stage               TEXT NOT NULL DEFAULT 'sent',  -- sent|replied|interview|offer|closed
    stage_dates         TEXT,                    -- JSON stage -> ISO date
    next_action         TEXT,
    next_action_due     TEXT,
    notes               TEXT,
    outcome             TEXT,                    -- accepted|rejected|withdrawn|no_response
    -- FR-425 learning variables
    variables           TEXT,                    -- JSON: email style, template, contact type, send time...
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX idx_card_seeker ON pipeline_card(job_seeker_id, stage);

CREATE TABLE mock_interview_session (
    id              TEXT PRIMARY KEY,
    job_seeker_id   TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    opportunity_id  TEXT NOT NULL REFERENCES opportunity(id) ON DELETE CASCADE,
    transcript      TEXT,                        -- JSON turns
    feedback        TEXT,                        -- JSON per answer
    weak_spots      TEXT,                        -- JSON
    status          TEXT NOT NULL DEFAULT 'open',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX idx_mock_seeker ON mock_interview_session(job_seeker_id, opportunity_id);

CREATE TABLE negotiation_brief (
    id              TEXT PRIMARY KEY,
    job_seeker_id   TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    opportunity_id  TEXT NOT NULL REFERENCES opportunity(id) ON DELETE CASCADE,
    suggested_ask   TEXT,
    arguments       TEXT,                        -- JSON
    fallbacks       TEXT,                        -- JSON
    market_data     TEXT,                        -- JSON
    pdf_path        TEXT,
    created_at      TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- PRIVATE: dream-job intelligence (FR-381, FR-382)
-- ---------------------------------------------------------------------------
CREATE TABLE gap_analysis (
    id                  TEXT PRIMARY KEY,
    job_seeker_id       TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    dream_job_model_id  TEXT REFERENCES dream_job_model(id) ON DELETE CASCADE,
    composite_profile_id TEXT REFERENCES composite_profile(id) ON DELETE CASCADE,
    gaps                TEXT NOT NULL,           -- JSON: gap, closing action, effort, decisive opportunities
    created_at          TEXT NOT NULL
);

CREATE TABLE stepping_stone_path (
    id              TEXT PRIMARY KEY,
    job_seeker_id   TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    campaign_id     TEXT REFERENCES campaign(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    steps           TEXT NOT NULL,               -- JSON sequence of roles/companies
    rationale       TEXT,
    created_at      TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Monitoring (FR-401..403)
-- ---------------------------------------------------------------------------
CREATE TABLE watchlist_entry (
    id              TEXT PRIMARY KEY,
    job_seeker_id   TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    company_id      TEXT NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    check_interval_days INTEGER NOT NULL DEFAULT 7,
    last_checked_at TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    UNIQUE (job_seeker_id, company_id)
);

CREATE TABLE notification (
    id              TEXT PRIMARY KEY,
    job_seeker_id   TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,               -- new_vacancy|signal|follow_up_due|reply|digest
    title           TEXT NOT NULL,
    body            TEXT,
    payload         TEXT,                        -- JSON
    read_at         TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_notif_seeker ON notification(job_seeker_id, read_at);

CREATE TABLE digest (
    id              TEXT PRIMARY KEY,
    job_seeker_id   TEXT NOT NULL REFERENCES job_seeker(id) ON DELETE CASCADE,
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    content         TEXT NOT NULL,               -- JSON
    recommended_action TEXT,
    emailed_at      TEXT,
    created_at      TEXT NOT NULL
);

-- SHARED: event and community radar (FR-462)
CREATE TABLE event (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    starts_at       TEXT,
    ends_at         TEXT,
    location        TEXT,
    country         TEXT,
    latitude        REAL,
    longitude       REAL,
    organiser       TEXT,
    url             TEXT,
    speakers        TEXT,                        -- JSON
    linked_company_ids TEXT,                     -- JSON
    source          TEXT,
    collected_at    TEXT NOT NULL
);
CREATE INDEX idx_event_start ON event(starts_at);

-- ---------------------------------------------------------------------------
-- Jobs, LLM audit, admin (FR-185, FR-361..364, NFR-701, NFR-702)
-- ---------------------------------------------------------------------------
CREATE TABLE job_run (
    id              TEXT PRIMARY KEY,
    job_seeker_id   TEXT REFERENCES job_seeker(id) ON DELETE CASCADE,
    campaign_id     TEXT REFERENCES campaign(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,               -- collection|profiling|financial|scoring|generation|monitoring|browser
    adapter_key     TEXT,
    status          TEXT NOT NULL DEFAULT 'pending', -- pending|running|paused|done|failed|cancelled
    progress_done   INTEGER NOT NULL DEFAULT 0,
    progress_total  INTEGER,
    error_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    checkpoint      TEXT,                        -- JSON, for resume (NFR-401)
    estimated_seconds INTEGER,
    started_at      TEXT,
    finished_at     TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_jobrun_campaign ON job_run(campaign_id, status);

CREATE TABLE llm_call (
    id              TEXT PRIMARY KEY,
    job_seeker_id   TEXT REFERENCES job_seeker(id) ON DELETE CASCADE,
    campaign_id     TEXT REFERENCES campaign(id) ON DELETE CASCADE,
    task            TEXT NOT NULL,
    prompt_template TEXT,
    prompt_version  TEXT,
    model           TEXT,
    provider        TEXT,
    entity_type     TEXT,
    entity_id       TEXT,
    prompt_text     TEXT,                        -- redacted after retention (FR-364)
    response_text   TEXT,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_eur        REAL NOT NULL DEFAULT 0,
    latency_ms      INTEGER,
    status          TEXT NOT NULL DEFAULT 'ok',
    error           TEXT,
    redacted_at     TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_llmcall_campaign ON llm_call(campaign_id, created_at);

CREATE TABLE source_catalogue (
    adapter_key         TEXT PRIMARY KEY,
    display_name        TEXT NOT NULL,
    source_type         TEXT NOT NULL,           -- job_board|ats|directory|registry|website|linkedin|compensation|news|events
    coverage_countries  TEXT,                    -- JSON
    coverage_industries TEXT,                    -- JSON
    query_capabilities  TEXT,                    -- JSON
    access_method       TEXT NOT NULL,           -- api|http|browser
    rate_limit_rps      REAL,
    cost_per_call_eur   REAL NOT NULL DEFAULT 0,
    tos_status          TEXT NOT NULL DEFAULT 'permitted', -- permitted|restricted|prohibited (IR-101)
    legal_notes         TEXT,
    enabled             INTEGER NOT NULL DEFAULT 1,
    requires_ack        INTEGER NOT NULL DEFAULT 0,
    acknowledged_at     TEXT,
    extraction_success_rate REAL,                -- NFR-403
    last_success_at     TEXT,
    updated_at          TEXT NOT NULL
);

CREATE TABLE audit_event (
    id              TEXT PRIMARY KEY,
    job_seeker_id   TEXT,
    actor           TEXT,
    action          TEXT NOT NULL,
    entity_type     TEXT,
    entity_id       TEXT,
    detail          TEXT,                        -- JSON
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_audit_entity ON audit_event(entity_type, entity_id);

CREATE TABLE app_setting (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Full-text search over the shared knowledge base (FR-345)
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE company_fts USING fts5(
    company_id UNINDEXED, name, normalised_name, business_summary, products_services
);
CREATE VIRTUAL TABLE vacancy_fts USING fts5(
    vacancy_id UNINDEXED, title, description, company_name_raw
);
