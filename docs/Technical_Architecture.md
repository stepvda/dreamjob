# Dream Job — Technical Architecture

| | |
|---|---|
| Document | Technical Architecture (TA) |
| Version | 1.0 |
| Date | 8 September 2026 |
| Status | Describes the implemented system |
| Specifies | Dream Job SRS v0.3, technology constraints CR-407–CR-410 |
| Companion | [Functional Design](Functional_Design.md) · [DPIA](DPIA.md) |

---

## 1. Constraints and what follows from them

The SRS fixes the stack (§2.5, CR-407–CR-410). Everything below is a consequence
of taking those constraints seriously rather than working around them.

| Layer | Fixed choice | What it forces |
|---|---|---|
| Frontend | React SPA | Route-level code splitting; the API is the only contract |
| Backend | Python | FastAPI, asyncio, in-process job runner |
| Database | SQLite | WAL mode, **one writer**, SQL confined to a repository layer |
| LLM | DeepSeek | OpenAI-compatible transport, so a local model substitutes without code change |
| Browser | CDP via Playwright | Attach-only; no credential handling anywhere in the codebase |

> **The most consequential constraint is SQLite.** One writer means the job
> runner is in-process and cooperative rather than a worker pool, and it means
> writes are serialised deliberately instead of contended for. That is a
> reasonable trade for a single-node product and it is the assumption to revisit
> first if multi-user concurrency ever becomes real (CR-408 anticipates exactly
> this).

---

## 2. System shape

```
┌──────────────────────────────────────────────────────────────────────┐
│  React SPA          22 screens · lazy-loaded · 88 KB initial (gzip)  │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  /api  (session cookie, client-bound)
┌───────────────────────────────▼──────────────────────────────────────┐
│  FastAPI            18 routers · 301 routes                          │
│                     current_seeker is the isolation boundary         │
├──────────────────────────────────────────────────────────────────────┤
│  Pipeline           intake · enrichment · planning · collection ·    │
│                     profiling · financial · opportunities · scoring ·│
│                     contacts · generation · post-application         │
├───────────────┬──────────────┬───────────────┬──────────────┬────────┤
│  LLMClient    │ EgressClient │ SourceAdapter │  JobRunner   │ crypto │
│  budget       │ robots       │ 24 adapters   │  resumable   │ AES-   │
│  redaction    │ rate limit   │ plan/fetch/   │  pause/      │ GCM    │
│  injection    │ cache        │ parse/        │  cancel      │ per    │
│  fencing      │ raw capture  │ normalise     │  checkpoint  │ seeker │
│  audit        │              │               │              │        │
├───────────────┴──────────────┴───────────────┴──────────────┴────────┤
│  Repositories       the only place SQL is written                    │
├──────────────────────────────────────────────────────────────────────┤
│  SQLite (WAL)  71 tables · 79 indexes  │  Filesystem: raw docs,      │
│  private ▸ per job seeker              │  generated CVs and PDFs,    │
│  shared  ▸ knowledge base              │  uploads, exports           │
└──────────────────────────────────────────────────────────────────────┘
        │              │              │              │
   DeepSeek API   Job boards /    Registries    Gmail OAuth /
   (or local)     ATS / sites     NBB, KBO…     Resend
```

**Scale.** 172 Python modules / 72,820 lines · 113 JS modules / 34,082 lines ·
17 test files / 13,406 lines · **507 tests** · 10 migrations · 21 versioned
prompt templates.

---

## 3. The five chokepoints

The architecture rests on funnelling five concerns through exactly one place
each. That is what makes the corresponding requirements *enforceable* rather
than aspirational — each is a property of one module, not a convention 172
modules have to remember.

| Chokepoint | Module | Enforces |
|---|---|---|
| Every SQL statement | `db/repositories/*`, `db/connection.py` | CR-408 portability, FR-344 isolation |
| Every outbound HTTP request | `egress/client.py` | FR-182 robots + rate limits, FR-183 raw capture, IR-102 |
| Every LLM call | `llm/client.py` | NFR-104 budget, NFR-205 injection defence, CR-410 redaction, FR-362 routing, FR-364 audit |
| Every external source | `adapters/base.py` | NFR-601 extensibility, IR-101 terms status |
| Every long-running task | `jobs/runner.py` | FR-185 control, NFR-401 resumability |

### 3.1 Persistence — `db/`

```python
read_tx()    # concurrent readers under WAL
write_tx()   # one global lock; SQLite serialises anyway, so queueing
             # in-process turns "database is locked" into an orderly wait
```

Repositories return plain dicts, never cursors. JSON columns encode and decode
at the boundary. Migrations are forward-only `NNN_name.sql` files with a
recorded checksum; editing an applied migration is a warning, not a silent
divergence.

**Schema shape.** 71 tables split by scope:

- **Private** — carry `job_seeker_id`: profile and versions, conflicts, skills,
  evidence, personas, enrichment findings, composite profile, dream job model,
  directives, campaigns, plan items, opportunities, application packages,
  dispatches, replies, pipeline cards, mock interviews, gap analyses, exports.
- **Shared** — the knowledge base, carrying **no** link back to a job seeker:
  companies, financial years and analyses, competitor links, hiring signals,
  vacancies, raw documents, events, source catalogue.
- **Restricted** — `contact`, which is shared *except* when collected through
  browser automation, where `shareable = 0`, `owning_campaign_id` is set and
  `retention_until` applies (NFR-303).

Full-text search over companies and vacancies uses FTS5, kept in sync by the
knowledge-base writer.

### 3.2 Egress — `egress/client.py`

One async client for all outbound HTTP.

- **robots.txt** parsed and cached per domain; a disallowed fetch raises rather
  than proceeding.
- **Per-domain pacing** with exponential back-off on 429/503 and relief on
  success. Concurrency capped by semaphore (NFR-102 asks for at least 20).
- **Response cache** with a configurable TTL, so a repeat campaign does not
  re-fetch unchanged pages.
- **Raw capture (FR-183, DR-102):** every response is written to disk under a
  content-hash path and registered in `raw_document`, so extractors can be
  re-run when they improve.

### 3.3 LLM — `llm/client.py`

```python
llm.complete_json(
    "extract.vacancy",              # task id → routing, budget, audit
    system=..., user=...,
    untrusted={"page": html},       # fenced, never concatenated into instructions
    entity_type="vacancy", entity_id=vid,
)
```

- **Routing (FR-362, NFR-306).** Administrator settings beat `.env`; a per-task
  model override beats the cheap/strong split; privacy-sensitive tasks —
  composite profile, tailored CV, motivation document — route to a local
  OpenAI-compatible endpoint when one is configured. Settings are cached 30
  seconds and invalidated on save, so a change on the admin screen takes effect
  on the next call rather than the next restart.
- **Budget (NFR-104).** Checked before each call, debited after. `should_degrade()`
  lets the pipeline shed optional work — speculative openings for low-ranked
  companies first — instead of stopping mid-campaign.
- **Injection defence (NFR-205).** Scraped text is fenced in labelled untrusted
  blocks, fence delimiters neutralised, known injection phrasings annotated
  rather than removed (the model still sees the page, visibly marked). A system
  preamble states the rule. Output is validated before use.
- **Redaction (CR-410, FR-106, FR-127).** Do-not-disclose fields and
  special-category data are stripped before anything leaves the machine.
- **Audit (FR-364).** Task, template, version, model, provider, tokens, cost,
  latency and the related record are written to `llm_call`, with a scheduled
  redaction sweep.

Prompts are versioned `.md` templates with a metadata header (NFR-602); the
version travels into `llm_call.prompt_version`, so a regression in generated
output can be tied to the prompt revision that caused it.

### 3.4 Adapters — `adapters/base.py`

Four methods, and the pipeline calls nothing else:

```
plan(directives, composite, caps) → [PlanItem]      native query form
fetch(item)                       → [RawRecord]     through EgressClient
parse(raw)                        → [dict]          deterministic, LLM fallback
normalise(parsed, raw)            → NormalisedRecord knowledge-base columns
```

Each adapter declares coverage, capabilities, rate limit, cost and
terms-of-service status; `sync_catalogue()` writes that into `source_catalogue`
at boot, where the planner reads it (FR-161, FR-164) and the administrator can
override it (FR-363).

**24 adapters:** 7 ATS · 7 job boards · 5 registries · 1 directory · 1 website
crawler · 3 news/events.

Extraction prefers JSON-LD `JobPosting` where present, then deterministic
selectors, then LLM extraction. Success rate is tracked per adapter so layout
breakage surfaces within one campaign (NFR-403).

### 3.5 Jobs — `jobs/runner.py`

In-process asyncio. Each job checkpoints after every unit of work, so a crash
loses at most the page in flight (NFR-401). `checkpoint_barrier()` is where a
worker yields and honours pause, cancel and per-target skip. On boot, jobs left
running by a restart are marked resumable.

Deliberately not a broker: a broker buys nothing on a single node and breaks the
one-writer invariant.

---

## 4. Security

| Concern | Mechanism |
|---|---|
| Data at rest (NFR-201) | AES-256-GCM, keys derived per job seeker by HKDF from one master key. Deleting a job seeker makes their ciphertext unreadable even if a copy survives. |
| Passwords (NFR-202) | Argon2id, with a policy requiring three of four character classes |
| MFA (NFR-202) | TOTP, secret stored encrypted |
| Sessions (NFR-202) | Random token; only the HMAC is stored; bound to a UA+IP fingerprint |
| Third-party credentials (NFR-203) | **Never stored.** Browser automation attaches to a session the user owns |
| Mail tokens (NFR-204) | Encrypted, scoped to send + bounce-read, revocable from the UI |
| Prompt injection (NFR-205) | Content isolation, instruction/data separation, output validation |
| Leakage (NFR-206) | Generated documents scanned against the job seeker's own provenance set |
| Audit (NFR-702) | Append-only trail of who approved and sent what, with the versions used |

**Known gaps**, stated rather than glossed: passkeys are not implemented (the
password+MFA branch is); the SQLite file itself is not encrypted, so
whole-database confidentiality rests on disk encryption — SQLCipher would add a
dependency.

---

## 5. Frontend

**React 18 + Vite + react-router.** Five dependencies in total: react, react-dom,
react-router-dom, vite, @vitejs/plugin-react. No UI kit, no chart library, no
date library, no state manager — charts are inline SVG, icons are inline SVG,
state is hooks plus one session context.

**Route-level code splitting.** 22 lazy chunks behind a shared shell:

```
initial   262 KB  (88 KB gzip)   shell + design system + router
per page  9–50 KB (3–14 KB gzip) loaded on navigation
```

Down from 858 KB unsplit — the administration area and the PDF previews no
longer load before the profile page can paint (NFR-101).

**Design system.** One `app.css` of tokens and layout, one `theme.css` that
re-points the tokens to the spectrum palette. Colour is structural: each of the
five journey phases owns a hue, so a violet screen is always about the profile
and an amber one is always about applying.

All six hues verified at **WCAG AA (≥4.5:1)** for small text against both the
surface and their own tint, in light and dark:

```
LIGHT        on white   on tint        DARK       on surface   on tint
Profile        5.37       4.63         Profile       6.39        5.92
Plan           5.19       4.56         Plan          7.10        6.25
Discover       5.13       4.51         Discover      8.40        6.99
Apply          5.05       4.52         Apply         8.51        7.49
Follow up      5.25       4.55         Follow up     7.19        6.63
```

Body text stays near-neutral; colour carries identity and state and never has to
be read. Speculative openings hold a hue outside all five phases, so FR-263
survives on colour alone. `prefers-reduced-motion` disables animation.

**Help is structural, not optional.** All copy lives in `help/content.js` — 18
screen entries and 22 glossary terms — so it is reviewable as a whole and
translatable as a unit (NFR-501). A screen cannot ship without help because the
drawer resolves its content from the route. **386 inline concept tips** across
the screens; `?` opens the panel anywhere.

---

## 6. Deployment

```bash
cp .env.example .env
python3 -m dreamjob.security.crypto --generate-key   # master key + session secret
pip install -r requirements.txt
cd frontend && npm install && cd ..
./scripts/dev.sh                       # API :8000, SPA :5173 with /api proxied
```

Production is the same process serving the built SPA from `frontend/dist`.
Migrations run at startup. The periodic scheduler (watchlist, digest,
follow-ups, contact retention, log redaction) starts from the API or runs under
launchd/cron.

`scripts/browser.sh` launches a Chromium-based browser with a remote debugging
port and a dedicated profile, for the collection steps that need a logged-in
session.

---

## 7. Data flow: one campaign

```
directives + composite profile + dream job model
        │
        ├─▶ knowledge base ──── reuse report (what is fresh enough already)
        │
        ▼
   plan per source ──▶ [user reviews, excludes, launches]
        │
        ▼
   JobRunner ──▶ adapters ──▶ EgressClient ──▶ raw_document
        │                          │
        │                          ▼
        │                     parse / normalise / dedup
        │                          │
        ▼                          ▼
   company profiling ────▶ shared knowledge base (companies, vacancies,
   financial analysis            financials, signals, competitors)
        │
        ▼
   opportunity synthesis  (vacancies + speculative openings)
        │
        ▼
   scoring  ── deterministic sub-scores + LLM rationale
        │
        ▼
   ranked list ──▶ [user selects]
        │
        ▼
   contacts ──▶ validation ──▶ generation ──▶ consistency check
        │                                            │
        │                                     [user approves]
        ▼                                            ▼
   audit trail ◀────────────────────────────── dispatch ──▶ replies
                                                              │
                                                              ▼
                                                    pipeline · segments ·
                                                    redirection advice
```

Every stage persists its artefacts and can be re-run independently (NFR-603).

---

## 8. Performance

| Requirement | Approach |
|---|---|
| NFR-101 interactive views < 2s at p95 | 79 indexes; FTS5 for search; deterministic sub-scores avoid an LLM call per row; lazy routes keep first paint small |
| NFR-102 ≥20 concurrent HTTP workers, serialised writes | Semaphore-bounded async egress; one global write lock under WAL |
| NFR-103 typical campaign < 4 hours | Caps bound the work; ATS JSON is cheap; **not yet measured against a real campaign** |
| NFR-104 bounded LLM spend | Pre-flight check, post-call debit, graceful degradation |
| CR-406 browser automation is slow | Accepted by design: tens to low hundreds of targets, announced up front |

---

## 9. Testing

**507 tests, 13,406 lines.** Each runs against a migrated temporary database and
without network access; LLM and HTTP paths are stubbed or skipped.

The tests worth naming are the ones that encode a requirement rather than a
function:

- A private table added by a **later migration** is still exported and erased
  (FR-108) — coverage is derived from the live schema, so a new table cannot be
  silently missed.
- Manual ranking survives recalculation (FR-284).
- A second job seeker's data appears nowhere in an export (FR-463).
- A redirection proposal naming an unseen segment is dropped, and effects are
  recomputed from the stored figures (§9.3 of the FDD).
- A professional history in a regulated domain is **not** stripped as
  special-category data, and a personal assertion **is** (FR-127).
- Administrator model settings actually change the routing decision (FR-362).
- Browser automation persists no cookie or token (NFR-203).

**Verified end-to-end against the real source documents**: LinkedIn export and
CV parsed and merged, 18 conflicts surfaced, photo extracted, 52 skills
normalised, and the dream-job model generated through the live DeepSeek API.

---

## 10. Extension points

| To add | Do this | Touching |
|---|---|---|
| A job board or registry | Subclass `SourceAdapter`, `@register_adapter` | One file |
| A jurisdiction's filings | A registry adapter + extractor mapping | Two files |
| A CV template | A module under `documents/templates/` | One file |
| A mail backend | Implement `MailBackend` | One file |
| A language | Add to the prompt templates and `help/content.js` | Content only |
| A local model | Set `DREAMJOB_LOCAL_LLM_*` and list the task groups | Configuration |
| PostgreSQL | Replace `db/connection.py` and the repository bodies | The layer built for it |

---

## 11. Requirement traceability

**154 of 157 requirements (98%)** are cited in the implementation — 103/104
Must, 43/44 Should, 8/9 Could. Regenerate the matrix with:

```bash
python3 scripts/traceability.py
```

The three uncited requirements and the known limitations are listed in the
Functional Design, §12.

---

*Companion documents: [Functional Design](Functional_Design.md) ·
[DPIA](DPIA.md) · [README](../README.md)*
