# Dream Job — Functional Design

**AI-assisted job discovery and application platform**

| | |
|---|---|
| Document | Functional Design (FDD) |
| Version | 1.0 |
| Date | 8 September 2026 |
| Status | Describes the implemented system |
| Specifies | Dream Job SRS v0.3 (`Dream_Job_Requirements.docx`) |
| Companion | [Technical Architecture](Technical_Architecture.md) · [DPIA](DPIA.md) |

---

## 1. Purpose and reading guide

The SRS says *what* Dream Job must do. This document says *how the product
behaves* — the screens, the states, the decisions the system makes on its own,
and the decisions it deliberately leaves to the job seeker. It is the reference
for acceptance testing and for anyone who has to explain the product to a user.

Requirement identifiers from the SRS are cited inline (FR-xxx, NFR-xxx, CR-xxx).
Section 12 maps every requirement to the module that implements it.

Where the implementation makes a judgement the SRS left open, it is called out
in a box like this:

> **Decision.** What was chosen, and why.

---

## 2. The product in one page

Most job search tools react to what has been published. Dream Job starts from
who the job seeker is and what they actually want, maps the relevant slice of
the market — including companies that have not advertised — judges each
company's capacity and likelihood to hire, and then removes the friction of
applying.

The system is opinionated about scope: it collects what the directives justify
rather than downloading the world.

Three properties shape every screen:

1. **Nothing is invented.** Every statement in a generated CV or email traces
   to a fact in the profile. A factual-consistency check runs before anything
   can be approved (FR-322, CR-405).
2. **Nothing is sent automatically.** Scoring is advisory; the job seeker takes
   every decision to apply (NFR-305). There is no path through the product in
   which a message reaches a stranger without a human approving it.
3. **Nothing is hidden.** Sub-scores, provenance, sample sizes, the AI call log,
   the cost so far — all of it is on screen. A user who disagrees with the
   ranking can see exactly what produced it.

---

## 3. Actors

| Actor | Description |
|---|---|
| **Job seeker** | Primary user. Supplies profile and directives, reviews rankings, approves and triggers applications. |
| **Administrator** | Maintains adapters, monitors campaigns, manages model selection and budgets. In a single-tenant deployment this is the same person. |
| **LLM service** | DeepSeek by default; any OpenAI-compatible endpoint, including a local one for privacy-sensitive steps (CR-409, NFR-306). |
| **External sources** | Job boards, ATS portals, company directories, financial registries, company websites, compensation sites, news and event feeds. |
| **Hiring contact** | Recipient of an introduction email. Not a user, but a data subject whose rights the product must honour (NFR-302). |

> **Decision (OQ-04).** Built as a **single-node, multi-job-seeker**
> installation. Private data is isolated per job seeker at the query level; the
> knowledge base is shared. This supports a household or a small coaching
> practice without the operational weight of a hosted service. See §11.

---

## 4. The journey

The SRS describes a ten-stage pipeline (§2.3). The interface groups it into the
five phases users actually think in, and gives each a colour that stays fixed
throughout the product:

```
  PROFILE ──────→ PLAN ──────→ DISCOVER ──────→ APPLY ──────→ FOLLOW UP
  violet          blue         teal             amber          rose

  Profile intake  Directives   Company profiles Hiring contacts Responses
  Composite       Campaign     Opportunities    Documents       Pipeline
  Dream job       Collection   Ranking          Dispatch        What works
```

Cool at the start (working out what you want), warm at the end (where messages
actually leave the building).

**The "Where I am" screen** renders this as a live map. Each of the sixteen
stages reports one of five states — done, running, ready, **blocked and why**,
or not started — with real counts, and links to the screen that advances it.
A blocked stage never just says "not done"; it says what would unblock it,
because the commonest way to get lost in a sixteen-stage pipeline is not knowing
the next move. A one-line version of the same map sits at the top of every
working screen.

---

## 5. Phase 1 — Profile

### 5.1 Intake (FR-102, FR-103)

Two documents go in.

**The LinkedIn export.** The section structure of LinkedIn's "Save to PDF" is
the base schema of the profile (FR-102): Contact, Summary, Top Skills,
Languages, Experience, Education, Certifications, Publications, Projects,
Honors, Volunteering, Recommendations. The parser is deterministic first — the
export places contact details in a narrow left column that interleaves with the
body text, so extraction works from word x-positions rather than reading order —
with LLM extraction as the fallback for passages the splitter cannot segment.

**The CV**, as PDF or DOCX. Sections, dates, employers and titles are extracted,
**and so is the photo**, which the tailored CV needs later.

*Verified against the real documents:* the product owner's LinkedIn export and
CV produce 8 experience entries, 27 top skills, 4 languages, 3 publications, a
1,163-character summary, and an extracted 300×300 photo.

### 5.2 Conflict resolution (FR-103)

The two documents disagree, and the system says so rather than silently picking
one. Every disagreement over a date, title or employer becomes a **conflict**
the job seeker resolves — LinkedIn value, CV value, or a manual override.

*Verified:* the real pair produces **18 conflicts**, including the employer
naming (`NGA Human Resources` vs `Alight Solutions (formerly NGA Human
Resources)`) and the strategy role's dates (LinkedIn `2014-11 → 2023-10` vs CV
`2016 → 2020`). Both are genuine ambiguities that would otherwise reach a CV.

Unresolved conflicts are shown as urgent, because everything downstream inherits
them.

### 5.3 Versions, skills, evidence, personas

- **Versions (FR-105).** Every save creates a new version. Campaigns record the
  version they used, so an old ranking stays explainable.
- **Skills (FR-107).** A bundled ESCO-style taxonomy of several hundred
  software, data and HR skills with synonyms, LLM normalisation for the rest.
  Proficiency, years of experience and last-used year are derived from the
  experience timeline. *Verified:* 52 normalised skills from the real profile.
- **Evidence (FR-441).** Repositories, publications, talks, certificates,
  attached to skills and achievements, cited with verifiable links in tailored
  CVs.
- **Personas (FR-442).** One job seeker can search as an architect and as a
  product leader, each with its own emphasis, dream-job statement and directive
  defaults. The knowledge base stays common.

### 5.4 Do not disclose (FR-106)

Flagged fields are excluded from every generated document **and stripped before
any prompt leaves the machine** (CR-410). The screen states both plainly. This
is the mechanism for a date of birth, a photo, or a current salary.

### 5.5 Composite profile (FR-121–FR-127)

An LLM synthesises the user-supplied data and verified online findings into the
canonical representation used for matching and generation: narrative summary,
career trajectory, core and adjacent competencies, seniority, domains,
achievements, public footprint, inferred preferences, constraints.

**Every statement carries its source** — user input, LinkedIn export, CV, or a
specific URL — and the screen makes each one traceable and editable (FR-125).

**Online enrichment** (FR-122) searches for the job seeker using name,
employers, locations and declared handles as anchors: personal site,
repositories, newsletters, publications, talks, press.

**Identity matching is the risk that matters here** (FR-123, RK-02). A namesake's
career must never reach a CV. Every finding is scored on multiple independent
signals, each recorded separately:

| Signal | What it compares |
|---|---|
| Name variants | Spelling, ordering, diacritics, initials |
| Employer overlap | Organisations named on the page against the profile |
| Location | Cities and regions |
| Timeline consistency | Dates on the page against the career span |
| Cross-links | Whether the page links to a profile already confirmed |
| Photo similarity | Perceptual hash against the profile photo |

Findings are classified **confirmed / probable / doubtful**. Only *confirmed*
merges automatically (FR-124). Probable and doubtful are presented with their
evidence for the job seeker to confirm or reject, and **a rejection is permanent** —
the finding is never proposed again.

Enrichment can be switched off entirely (FR-126), in which case no request
leaves the machine and the composite profile is built from the documents alone.

**Special-category data** (FR-127) is never stored — health, political opinions,
religion, sexual orientation, trade-union membership.

> **Decision.** The filter distinguishes the *person* from the *vocabulary*. An
> earlier keyword sweep deleted "healthcare data company", "medical devices",
> "Political Science" and "human rights platform" from ordinary professional
> histories — protecting nobody while destroying exactly the careers that need
> representing. Detection now runs three tiers: an occupational sense wins
> outright; an assertion about a person ("diagnosed with", "member of the X
> party") is removed; a bare category noun is removed unless the sentence is
> plainly technical.

### 5.6 Dream job (FR-109, FR-128)

A free-text field, in the job seeker's own words and language, with no length
limit: the work itself, the organisation, the people, the impact, the
conditions, and what to avoid.

An LLM turns it into a **dream job model**: target roles, role families,
responsibilities, company characteristics, culture and values cues, deal-breakers,
and implicit preferences the job seeker did not state. It is shown for
confirmation, versioned, and used as a first-class input to planning, discovery,
speculative openings and scoring.

*Verified against a real statement:* deal-breakers come back structured with a
`hard` flag and a `detectable_from` list naming which opportunity fields can
evidence them — which is what makes them usable in scoring rather than
decorative.

---

## 6. Phase 2 — Plan

### 6.1 Directives (FR-141–FR-149)

Directives bound the campaign. They are **structured controls, not free text**
(FR-141), because they have to translate reliably into each source's own query
language. The single free-text field is "notes to the AI".

| Group | Covers |
|---|---|
| Job content (FR-142) | Titles with synonyms, function family, seniority range, must-have and nice-to-have skills, industries of interest and exclusion, management scope, keywords to avoid |
| Company type (FR-143) | Size bands, stage, trajectory, ownership, explicit include/exclude lists |
| Location (FR-144) | Geocoded areas with radius, countries, relocation willingness, maximum commute by mode |
| Work arrangement (FR-145) | On-site / hybrid with minimum remote days / remote, FTE percentage, contract type, travel tolerance |
| Compensation (FR-146) | Minimum package, currency, equity treatment |

Compensation is used **for filtering and scoring only**. An explicit
`disclose_in_content` flag, default off, is what any generating code must check
before a figure can appear in a document.

**Discretion mode (FR-385)** is for job seekers who are currently employed. It
excludes the current employer, its group entities (matched on name prefix and
shared domain, not just exact name) and any flagged company; excludes contacts
likely to expose the search; and suppresses LinkedIn actions that signal job
hunting. It is indicated in a bar across the whole interface, not buried in
settings.

### 6.2 Campaign planning (FR-161–FR-166)

The planner reads the source catalogue, selects sources the directives and
coverage metadata justify (FR-164 — no Asian boards for a Benelux search), and
has an LLM generate each source's **native query form** grounded in the
composite profile, the directives and the dream job model.

**Before anything runs**, the job seeker sees the whole plan: sources, queries,
expected volume, estimated duration, estimated cost — and can edit or exclude
any source (FR-163).

**Knowledge-base reuse (FR-342)** is reported concretely before launch: per
entity type, how many records are reused versus scheduled, and the time and cost
avoided. Staleness defaults follow FR-343 — vacancies 7 days, company websites
90 days, filings 1 year.

### 6.3 Collection (FR-181–FR-186)

Twenty-four adapters over a common four-step contract:

| Type | Count | Sources |
|---|---|---|
| ATS | 7 | Greenhouse, Lever, SmartRecruiters, Ashby, Recruitee, Personio, Workday |
| Job boards | 7 | EURES, VDAB, Jobat, StepStone, Indeed, Welcome to the Jungle, generic |
| Registries | 5 | NBB, KBO/BCE, Companies House, KvK, SEC EDGAR |
| Directories | 1 | OpenCorporates |
| Website | 1 | Bounded company-site crawler |
| News / events | 3 | RSS, event radar, social |

> **Decision.** ATS endpoints are the highest-value sources and are implemented
> first. They are public, keyless, structured JSON, and they are the freshest
> statement of what a company is actually hiring for. A board aggregates
> yesterday's postings; `boards-api.greenhouse.io` is the company's own list.

Collection runs as **resumable background jobs** with per-adapter progress,
error counts, pause, resume and cancel. A crash loses at most the page in flight.
Explicit caps bound every job — pages, companies, people, wall-clock duration —
and an administrator's per-source cap is a ceiling a campaign cannot raise.

**Sources whose terms prohibit automated access are disabled by default** and
require an explicit administrator acknowledgement (IR-101). Indeed and StepStone
ship in that state.

### 6.4 Browser automation (FR-201–FR-208)

LinkedIn and Glassdoor defend against automation. Rather than circumventing
that, the system attaches to a browser **the user launched and logged into
themselves**, over the Chrome DevTools Protocol.

It never requests, captures, stores or replays credentials, cookies or session
tokens (FR-202, NFR-203). A dedicated profile directory keeps the user's normal
browser untouched.

- **Scope is the plan's target list only** (FR-205). An explicit allowlist of
  URLs is enforced; open-ended crawling is refused.
- **Duration is announced before starting** (FR-204) and refined during
  execution, computed from the same pacing distribution the executor uses.
- **It stops immediately** on a captcha, challenge or rate-limit page (FR-203).
- The user can watch, pause, and skip individual targets (FR-206).

> **CR-401 is unavoidable and is presented as such.** LinkedIn's user agreement
> prohibits automated access *including through a session the user logged into
> themselves*. The design reduces technical friction; it does not remove the
> contractual restriction, and the account may be restricted. The warning is
> shown before the first run and the acknowledgement recorded. The system
> refuses to run without it. **This is the job seeker's decision to make, and
> the product's job is to make sure they make it knowingly.**

---

## 7. Phase 3 — Discover

### 7.1 Company profiling (FR-221–FR-226)

A bounded crawl of the company's own site, prioritised by a URL scorer so a
30-page budget is spent on the pages that matter: about, products, customers and
references, team and leadership, careers, news, locations, values.

The result is a **standardised profile with a fixed schema** (FR-222), displayed
in a consistent layout: identity and legal identifiers, business summary, sector
codes, size, locations, structure and departments, key people, references,
financial summary, hiring signals, competitors, sources. Every field carries
confidence and provenance (NFR-402); low-confidence fields are visibly flagged.

For larger companies a **departmental map** (FR-223) shows business units,
functions, locations and, where known, the head of each — which is what turns
"apply to this company" into "apply to this department".

Company pages are the exact injection vector NFR-205 is about, so crawled text
reaches the model as fenced, explicitly-labelled untrusted data.

Profiles are **shared across job seekers** and refreshed incrementally (FR-226).

### 7.2 Competitors and signals (FR-224, FR-225, FR-402)

Competitors are found by combining signals — shared sector codes, shared
customers, press co-mentions, similar product descriptions, directory
classifications — because no single one is reliable enough to reach the three
suggestions per company the acceptance criteria ask for.

**Hiring signals** are recorded with dates and sources: headcount growth, new
offices, funding rounds, product launches, recent postings. **Timing
intelligence** (FR-402) detects events that typically precede hiring and
recommends an application window; opportunities where the moment is favourable
are flagged in the ranked list.

### 7.3 Financial analysis (FR-241–FR-246)

Five years of filings per company, from the registry for its jurisdiction.

Extracted per year: revenue, gross margin, EBIT, EBITDA where derivable, net
result, equity, cash, total debt, headcount, personnel costs, capex. XBRL and
CSV are handled structurally; PDF filings through table extraction with LLM
assistance for the awkward ones.

Computed: revenue and headcount CAGR, margin trends, **personnel cost per FTE**
(the pay-level proxy), liquidity and solvency ratios, and a trajectory
classification with confidence.

Then two 0–100 scores — **ability to pay** and **investment capacity** — each
with a rationale that **cites the figures used**. A rationale that names no
numbers is treated as a defect.

Belgian abbreviated accounts frequently omit turnover. Where filings are
unavailable or incomplete the profile falls back to secondary signals and is
**clearly marked estimated** (FR-245) rather than presenting a guess as a
figure. Balance-sheet totals and sign conventions are reconciled and
inconsistencies flagged rather than silently accepted (NFR-404).

> **Decision (OQ-01).** Belgium is first-class (NBB + KBO), with the UK
> (Companies House), the Netherlands (KvK), the US (SEC EDGAR) and
> OpenCorporates implemented to the same contract. France (Infogreffe) and
> Germany (Bundesanzeiger) are **not** implemented; companies in those
> jurisdictions fall through to the FR-245 estimated path. Adding them is a new
> adapter and no pipeline change.

### 7.4 Opportunities (FR-261–FR-265)

Every collected vacancy is normalised into an opportunity. For every interesting
company **without** a matching vacancy, an LLM generates **speculative
openings** (FR-262): roles the company is likely to need or able to create in
the next 6–12 months, from its profile, hiring signals, financial capacity,
department map, competitors' hiring, and the job seeker's own profile. Each
carries a plausibility score and a rationale.

> **FR-263 is a hard rule that reaches into the email text.** A speculative
> opening is visibly distinct in every view — its own colour, outside the five
> phase hues, plus an explicit badge — and the introduction email is written as
> a spontaneous application that **never asserts a vacancy exists**.

Compensation estimates (FR-264) come with a confidence and a source list, drawn
first from posted ranges on similar vacancies in the collected corpus, which is
the most defensible source available.

### 7.5 Scoring and ranking (FR-281–FR-285, FR-383)

Seven weighted sub-scores: profile fit, dream-job fit, directive fit, company
attractiveness, compensation fit, plausibility (speculative only), and
reachability.

> **Decision.** Sub-scores are computed **deterministically** wherever the
> question is factual — skill overlap, distance, band membership, financial
> scores. The LLM is used for the semantic dream-job component and for the
> written rationale. This makes the ranking reproducible, cheap, and explainable
> in terms a user can argue with.

Every score is explainable: the sub-scores and a one-paragraph rationale are on
screen (FR-282).

The **dream-job fit meter** (FR-383) is deliberately separate from the overall
score, listing criteria met, partially met and violated — because a role can
suit a CV perfectly and still not be what the person wants.

**User control (FR-284).** Check, pin, drag to re-order, mark not interested
with a reason. **Manual order overrides the computed ranking and survives
recalculation**, implemented as a stable rank that recomputation never touches.
Feedback re-tunes that job seeker's weights and suggests directive refinements
(FR-285).

---

## 8. Phase 4 — Apply

### 8.1 Hiring contacts (FR-301–FR-306)

Priority: the hiring manager of the relevant department where identifiable, then
talent acquisition, then a generic careers mailbox. Candidates are ranked and
presented with role, source and confidence.

**Addresses are found or inferred** (FR-303) from the company website, press
pages, or pattern inference from other addresses on the same domain — and the
method is recorded, because an inferred address deserves less confidence than a
published one.

**Every address is validated before use** (FR-304): syntax, MX lookup,
disposable and role-address detection, SMTP verification where the receiving
server permits, and catch-all detection. Results are valid / risky / invalid /
unknown; **invalid is never used**. Many providers blackhole verification, so an
inconclusive probe is recorded as *unknown*, never as *valid*. Probes are
rate-limited and cached per address (FR-305).

**Introduction routes** (FR-302, FR-461) are ranked by strength: first-degree
contacts, alumni, former colleagues now at the company, shared communities. The
message to the *intermediary* is a different artefact from the application
itself, and the product keeps them distinct.

**Data protection is on the screen, not in the settings.** Only professional
contact details are stored (FR-306). An objection blocks an address
**permanently and for everyone** on the installation (NFR-302), enforced in the
query the generator uses rather than only in the interface. Contacts collected
through browser automation carry a retention deadline and are never shared
between job seekers (NFR-303).

> **Decision (OQ-03).** Browser-collected contacts are **not** shared, matching
> the SRS's proposed default. **(OQ-05)** Third-party lookup services are behind
> a config flag, off by default; the built-in DNS and SMTP validation needs no
> third party and no data-processing agreement.

### 8.2 Generated material (FR-321–FR-331)

Four artefacts per selected opportunity:

| Artefact | Purpose | Sent? |
|---|---|---|
| **Tailored CV** | Emphasises relevant experience, using only true profile facts | Attached |
| **Company & job briefing** (FR-329) | Interview preparation: full company profile, five-year financials with charts, signals, competitors, the opening in full, the contact, likely interview topics, questions to ask | **Never sent** |
| **Motivation & fit** (FR-330) | Why this job, why you fit it, why you fit the company, anticipated objections with answers, rehearsable talking points | **Never sent** |
| **Introduction email** | Addressed to the hiring contact | Sent |

CVs are produced in DOCX and PDF from configurable templates, in the language of
the opportunity (nl / fr / en / de), **including the photo** where the job seeker
has one and has not marked it do-not-disclose.

**The factual-consistency check (FR-322, RK-03) gates approval.** Claims are
extracted and matched against profile facts. The date, employer and title checks
are **deterministic**, because that is what actually catches a hallucinated
employer; an LLM judge contributes as one signal, not as the arbiter. A failed
check visibly blocks approval. A leak scan (NFR-206) flags content attributable
to no source in this job seeker's own provenance set.

**Review and approval (FR-324).** Preview, edit, regenerate with an instruction,
approve or discard. Bulk approval opens a **mandatory summary of exactly what
goes to whom** — recipient, company, role, and whether the opening is real or
speculative.

### 8.3 Dispatch (FR-325–FR-327)

Two backends behind one interface:

| | Gmail OAuth | Resend |
|---|---|---|
| Sends as | The job seeker's own mailbox | `stephane@stepvda.com` |
| Replies | Land in the inbox; detected by IMAP | No inbox; webhook events |
| Credentials | Refresh token, encrypted per job seeker, revocable | API key |
| Scopes | `gmail.send` + `gmail.readonly` for bounce reading | — |

> **Note.** The Resend backend is complete but the API key has not yet been
> supplied. It fails with an actionable message and the status screen reports
> "not configured" rather than showing a broken state.

**Guard rails are enforced server-side** (FR-325, RK-05): a configurable rate, a
daily cap, and send windows expressed **in the recipient's time zone**, derived
from the company's country. Only validated addresses are used; objected contacts
are refused.

Every send is logged with recipient, timestamp, attachments, message-id and
delivery status, and recorded in an immutable audit trail with who approved it
(NFR-702). Bounces are detected by parsing DSN structure rather than
string-matching subjects. Follow-up reminders fire after a configurable silence
(FR-327).

---

## 9. Phase 5 — Follow up

### 9.1 Recording responses

> **Extension beyond the SRS**, at the product owner's request. Automatic
> detection only covers replies arriving in a pollable mailbox. In practice many
> do not: a recruiter phones, answers on LinkedIn, or replies through an ATS
> portal — and Resend has no inbox at all.

Any response can be **entered by hand** and is then treated exactly like a
detected one: classified, moving the pipeline card, feeding the learning.

The person entering it also states what they think happened. **That stated
outcome overrides the model's classification** — they were there. Corrections
after the fact are one click, because a misread rejection quietly distorts every
rate it is counted in.

Recording a rejection is given exactly the same visual weight as recording good
news. It is data, and the analysis needs it.

### 9.2 The pipeline board (FR-421–FR-424)

Five stages — sent, replied, interview, offer, closed — moved automatically by
reply detection where possible and by hand otherwise.

Replies are classified (FR-422) as interest, information request, interview
invitation, rejection, referral or auto-reply, and the appropriate response is
**drafted in the same thread** using the briefing and motivation documents as
context. Drafts wait for the job seeker; nothing sends itself.

Where a reply proposes interview times (FR-423), the slots are extracted —
including relative dates in four languages — checked against the connected
calendar, and the confirmation drafted, with the briefing attached to the
calendar entry on approval.

**Mock interviews** (FR-424) run turn by turn: the LLM interviews using the
briefing, gives feedback on each answer against the motivation document, and
ends with a list of weak spots. Sessions are stored and repeatable.

### 9.3 What works, and where to redirect

> **Extension beyond the SRS**, at the product owner's request: *if a lot of
> rejections are received for a certain type of job, the AI should advise on how
> to redirect the type of job or company to get higher acceptance rates.*

FR-425 analyses the variables of *how* an application was written. This adds the
question a job seeker actually asks after a month: **which kinds of job and
company answer me, and which ignore me.**

Outcomes are segmented across nine dimensions a directive can change: function
family, seniority, company size band, company stage, sector, work arrangement,
country, advertised-versus-speculative, and language.

Every rate is reported with its sample size and a Wilson confidence interval.
**Sample size is given the same visual weight as the percentage**, because a 50%
reply rate from 2 applications and one from 40 are the same number and entirely
different evidence.

Three rules govern the advice:

1. **The numbers come first, the model second.** Segment rates are computed
   deterministically. The model is handed that table and asked to turn it into
   advice — it is never asked to find the pattern, because a language model
   asked to spot trends in a table will find one whether or not it is there.
2. **Proposals are validated against the data.** A proposal naming a segment the
   job seeker never applied to is dropped, and the figures are re-attached from
   the real analysis rather than trusted from the model.
3. **Nothing is applied automatically.** Accepting a proposal writes a **new
   directive-set version**; the previous one is untouched and can be returned
   to.

Advice refuses to speak below roughly six resolved applications, and says so.
A proposal that conflicts with the dream-job statement is **flagged, not
hidden** — the data does not overrule what the person said they want; it only
reports what wanting it is costing.

*Verified:* 8 data-engineering applications with 5 replies against 9
data-science applications with 1 reply and 8 rejections — the weak segment is
identified, the stronger one proposed, and an invented segment is rejected.

### 9.4 Monitoring (FR-401–FR-403)

Watched companies are rechecked on a configurable interval for new vacancies,
signals, news and filings; matching vacancies are added to the ranked list
automatically. A weekly digest carries new opportunities, replies, follow-ups
due, watchlist changes and **one** recommended next action.

---

## 10. Dream-job intelligence (FR-381–FR-384, FR-443, FR-444)

- **Gap analysis (FR-381).** What separates the composite profile from the dream
  job model — skills, experience, certifications, languages, leadership scope,
  visibility. Each gap names a concrete closing action, an estimated effort, and
  **the opportunities where that gap was decisive**, computed from which
  opportunities lost points on that dimension rather than asserted.
- **Stepping stones (FR-382).** When nothing clears the dream-job threshold,
  two or three sequences of intermediate roles reachable now that plausibly lead
  there. Opportunities are taggable *destination* or *stepping stone*.
- **Values match (FR-384).** The company's derived values against those in the
  dream-job statement, with mismatches surfaced as explicit warnings on the
  company profile and in the motivation document.
- **LinkedIn suggestions (FR-443).** Headline, About, skills, keywords — with
  the keyword advice grounded in term frequency across the actual collected
  vacancies, not generic advice. Presented as editable text; the system never
  modifies LinkedIn.
- **Negotiation brief (FR-444).** Ability-to-pay, personnel cost per FTE, market
  data and the job seeker's own directives, into a suggested ask with arguments
  and fallbacks.

---

## 11. Isolation, administration and export

**Isolation (FR-101, FR-344).** Private tables carry a job-seeker id and every
query filters on it. Shared knowledge-base rows carry **no link back** to the
job seeker whose campaign produced them.

> **Open item.** An administrator is itself a job seeker row and can read any
> campaign dashboard and the AI call log, including other job seekers' prompt
> text. Correct when the operator is the sole data controller; it needs a policy
> decision before a shared installation. Recorded in the DPIA.

**Administration (FR-361–FR-364).** Campaign dashboard with per-source records,
errors, token consumption and cost; model selection, budgets and prompt
templates; adapter enable/disable with the IR-101 acknowledgement flow; the AI
call log with a scheduled redaction sweep.

**Erasure and export (FR-108, NFR-301).** Deleting a job seeker removes every
private row and every file on disk while leaving shared market data intact. The
table list is **derived from the live schema**, so a table added by a later
migration cannot be silently missed. Full data export is one call.

**Campaign export (FR-463).** A PDF bundle and a machine-readable JSON package —
ranked list, company profiles, briefings, motivation documents, application
history — respecting do-not-disclose flags and containing no other job seeker's
data.

---

## 12. Requirement traceability

**154 of 157 requirements (98%) are cited in the implementation**: 103 of 104
Must, 43 of 44 Should, 8 of 9 Could. The full matrix — requirement to file — is
generated from the source and reproduced in the Technical Architecture, §11.

Not cited in code, by nature:

| Id | Requirement | Why |
|---|---|---|
| NFR-103 | Campaign completes non-browser stages within 4 hours | A performance target, measurable only against a real campaign |
| NFR-304 | A DPIA shall be produced before production use | A document, delivered as [DPIA.md](DPIA.md) |
| NFR-503 | Usable on a laptop; tablet-acceptable | Implemented in CSS media queries, which the matrix does not scan |

### Known limitations

Carried forward honestly rather than closed:

- **Passkeys (NFR-202)** are not implemented; the requirement's "strong
  passwords with MFA" branch is. MFA is opt-in per account.
- **Database-level encryption (NFR-201)** is partial. Credentials and secrets are
  encrypted with per-job-seeker keys; the SQLite file itself relies on
  disk-level encryption. SQLCipher would add a dependency.
- **France and Germany** have no financial-registry adapter (§7.3).
- **Employer-review collection** for FR-384 has no adapter; the consuming path
  exists and is unfed.
- **Actiris** (Brussels) is not implemented among the Belgian public employment
  services; VDAB is.
- **Microsoft 365 mail and calendar** are not implemented; Gmail and Google
  Calendar are. FR-325 is an either/or.
- **DuckDuckGo search** returns a bot-check page from datacentre ranges, so
  FR-122 degrades to declared and handle-derived URLs in such an environment.

---

## 13. Acceptance criteria status

| SRS §8 scenario | Status |
|---|---|
| Import LinkedIn PDF and CV, resolve conflicts, obtain composite profile and dream job model | **Verified on the real documents** — 18 conflicts surfaced, photo extracted, model confirmed |
| Planted homonym presented for confirmation, never merged once rejected | Implemented; permanent rejection enforced |
| Directives through structured controls, plan reviewed, source excluded, launch | Implemented |
| Scope-capped LinkedIn run, duration within ±25%, stops on challenge | Implemented; **not exercised against live LinkedIn** |
| 20 companies with profiles, five-year financials, 3 competitors, 2 speculative openings each | Implemented; keyed registries degrade without credentials |
| Ranked list with explainable scores; manual order survives recalculation | Implemented and tested |
| Five opportunities: contact, tailored CV, briefing, motivation, email, bulk approve, dispatch, bounce | Implemented; dispatch needs the Resend key or Gmail OAuth |
| Second job seeker reuses profiles, sees no private data of the first | Implemented and tested |
| Gap analysis with 3 gaps, 2 stepping-stone paths, fit meter | Implemented |
| Discretion mode excludes employer, group entities and contacts everywhere | Implemented |
| Watchlist, injected vacancy, notification, digest | Implemented |
| Interview-invitation reply, calendar check, drafted confirmation, mock interview | Implemented; calendar needs OAuth credentials |
| Export bundle respects flags, contains no other job seeker's data | Implemented and tested |

---

*Companion documents: [Technical Architecture](Technical_Architecture.md) ·
[DPIA](DPIA.md) · [README](../README.md)*
