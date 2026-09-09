# Interim and staffing-agency postings: proposal

*Design decision, 2026-09-09. Synthesised from four independent evaluations of
`data/dreamjob.db` (read-only, 2,741 vacancy rows) and of the live EURES and
KBO endpoints, together with the earlier note `docs/Agency_Research_Design.md`.
Where the evaluations disagreed, the resolution and its reason are in
Appendix A; every headline count was re-run against the database while this
was written. Nothing here has been implemented; the throwaway probes named in
Appendix C are not the implementation.*

---

## 1. The problem

Interim, staffing and selection agencies post vacancies on behalf of employers
they do not name, and Dream Job — whose premise is understanding the employer:
profiling it, reading five years of its accounts, judging its ability to pay,
inferring what it will hire for, writing a letter about why you want to work
*there* — has no notion of an intermediary anywhere in its schema, prompts,
scoring or documents, so every company-centred stage treats the agency as the
employer. Measured on the live corpus this is not a fringe case: **17–20% of
all real postings (450–535 of 2,687) and 33–37% of the Belgian EURES slice
(367–406 of 1,098 named rows) are agency postings, rising to 80–92% of Actiris
rows**, while a keyword match on the employer name finds 9–13%; and a
proportional pull from VDAB/EURES, where 87–92% of last week's Flanders rows
sit in the staffing section, would make a Belgian campaign closer to nine
agency rows in ten. The realised damage today is one approved application
package (NOEL FRANKLIN BV, Manufacturing Engineer) whose e-mail, motivation
letter and briefing describe an unnamed Roeselare machine-builder as if it were
the agency, and the latent damage is the 210 agency vacancies at 34
"reachable" agencies for which the Apply Browser will generate the same package
on one click.

| Measure | Value | Arithmetic |
|---|---|---|
| Vacancy rows / e2e fixtures / real | 2,741 / 54 / **2,687** | 2,741 − 54 |
| Real rows with an employer name / without | **2,649** (1,176 names) / 38 | 2,687 − 38 |
| Real rows by adapter | EURES 1,136 · Arbeitnow 1,277 · Greenhouse 225 · Actiris 49 | sums to 2,687 |
| Agency rows, hand-labelled (three labellers, three label sets) | **450–535** = 17–20% | 476/2,649 = 18.0%; 486/2,649 = 18.3%; 535/2,687 = 19.9% |
| Agency employers | 77–96 names, of which ~65 are beyond doubt | |
| Name-keyword floor | 233–338 rows (43–64 names) = 9–13% | the brief's 233 used a shorter list |
| EURES (BE) named rows that are agency postings | **367–406 of 1,098** = 33–37% | 38 of 1,136 EURES rows carry no employer |
| Actiris | 39–45 of 49 = 80–92% | |
| Arbeitnow (DE) | 57–70 of 1,277 = 4.5–5.5% | German Personalberatung |
| Greenhouse (a company's own board) | 0 of 225 | direct by construction |
| Live EURES, NUTS BE2, last week | 60,861 of 66,002 rows in section O (92%); a last-month facet gave 57,482 | staffing is section **O** (see §2.5) |
| Raw EURES postings with `positionOfferingCode = temporarytohire` | 736 of 2,247 unique = 33% | stored as `permanent` today (§3) |

The range 17–20% is three labellers reading the same rows with slightly
different boundaries (whether a 'recruitment reserve' or a payroll firm
counts); what would settle it is one shared label set for the 122 employers
with ≥ 4 postings, reviewed by the product owner — that set is also the
acceptance fixture in §7.

---

## 2. Detection

### 2.1 The test every detector has to pass

Volume, title diversity, family spread and city spread do not separate a large
distributed employer from an agency. What does is whether **the employer
described in the advert is the employer named on it**: a real employer
introduces itself, by name, in roughly the same words every time; an agency
cannot, because every advert is a different, unnamed company. That is the
"coherence" the brief was after, and it is measurable per employer without any
taxonomy — as *voice*, not as title statistics.

| | GitLab (225 postings) | NOEL FRANKLIN BV (56) |
|---|---|---|
| Distinct titles / ratio | 213 / 0.95 | 56 / 1.00 |
| Title-derived function groups | 13 (entropy 2.74) | 13 (3.26) |
| Cities named in text | 3 | 12 |
| → every diversity measure | *agency-like* | *agency-like* |
| Employer's own name in its adverts | 100% | 0% |
| First-person employer voice ("we are…") | 100% | 20% |
| Singular-client phrase ("Onze klant…") | 0 of 225 strict (2 loose) | **55 of 56** |
| Mean 6-word-shingle Jaccard between adverts | 0.303 (shared self-description) | **0.007** (every advert a different company) |
| Posts on its own ATS board | yes (ats.greenhouse) | no |
| Register | — (US) | KBO 0700.275.068: NACE-BEL **78.100 / 78.200 / 78.300** |
| EURES section O | — | yes (1 row fetched under O; live probe 50/50) |
| **Score (§2.3)** | **−5.5 → direct-likely** | **+11.5 → certain** |

The same voice signals put 100G BV — 26 postings across internal sales,
marketing, mould design, purchasing and production team lead, no agency word in
its name, no client phrase, 26/26 `directhire`, KBO codes 62/63/70 — at
anonymous 1.00 and Jaccard 0.003, and the two federal ministries and Smals,
which a title-spread test flags, at direct-likely by their legal form and
their self-naming.

### 2.2 Signals, with what was measured

Per employer, aggregated over its postings (n = postings). Precision and
recall are at employer level on hand-labelled sets: Eval 1's 50 largest
employers (20 agencies / 30 direct) and its 103 employers with ≥ 4 postings
(35 agencies); cross-checked on Eval 2's 43 (18/25) and Eval 4's 66 (29/37).

**Registry and structured signals (strong, cheap, language-independent)**

| # | Signal | Fires when | Points | Precision | Recall | Notes |
|---|---|---|---|---|---|---|
| R1 | Register says staffing | KBO NACE-BEL 78.100/78.200/78.300 in either regime; Companies House SIC 781xx/782xx/783xx; KvK SBI 78 — **after the hardened name gate** (§6.2 N3) | +3 | 1.00 (0/6 direct; 12/12 in the earlier note) | 0.83–0.86 of *correctly resolved* agencies (15/18; 12/14) | Misses selection firms registered as consultancies: 100G, EDITX, CONESSENCE. Phonetic resolution picked the wrong entity 6 of 30 times without the gate |
| R2 | EURES files the employer under section O | Any of the employer's rows was fetched under the O partition (free, from `provenance`), or ≥ 0.3 of page-1 exact-name rows in a live `EMPLOYER` search with `sectorCodes=['O']` | +3; **+1** when KBO's main activity is 77/79/80/81/82 | Live: 1.00 on 16 probed. Provenance: 29 of 36 employers under O are agencies (0.81) before the division rule | Live: 0.82 (9/11) | Catches 100G (O-share 1.0), which no other machine signal does. CleanLease (77), ISS (81), Petit Forestier (77) are O without being agencies |
| R3 | Temp-to-hire offering code | share of `temporarytohire` + `temporary` ≥ 0.5, n ≥ 3 | +3 | 1.00 (0 FP in 30, 25 and 37 direct employers) | 0.24–0.44 | Interim houses only (FORUM JOBS 38/38, ADECCO 19/20, KONVERT 18/21, ABSOLUTE@WORK 18/20). **Never** `contract`, `contracttohire`, `seasonal`, `oncall`: they are public fixed-term appointments, hotels and an equestrian centre (FP 3/37; P 0.52 when included) |
| R4 | Agency stem in the name | substring: interim, uitzend, recruit, staffing, talent, jobs, personalberat/-dienst/-vermittl, headhunt, payroll, outplacement, zeitarbeit, @work, jobmatch; plus a maintained brand list (Adecco, Randstad, Manpower, Start People, Accent, Konvert, Actief, Unique, Synergie, Bright Plus, SD Worx Staffing, Michael Page, Hays, Robert Walters…) | +2 | 1.00 on all labelled sets | 0.50–0.57 | Misses NOEL FRANKLIN, 100G, CONESSENCE, EDITX, VIND, EHRS |
| R4b | Ambiguous whole word | hr, people, work, career, search, select, employ | +1, **never alone** | corpus FPs: Swiss Life Select, SelectLine, Digital Career Institute, Vindevogel | — | counts only with another signal |

**Voice signals (the coherence test; regexes and shingle sets, recomputed on ingest)**

| # | Signal | Fires when | Points | Precision | Recall | Notes |
|---|---|---|---|---|---|---|
| T1 | Singular-client phrase | share of postings with a hiring-framed **singular** third party: `onze klant` (not `klanten`, not `de klant`), `in opdracht van`, `voor een <bedrijfstype> in <plaats>`, `notre client (est\|recherche)`, `our client (is\|'s team\|based)`, `on behalf of a partner company`, `unser Mandant/Kunde ist`, `für ein etabliertes/renommiertes Unternehmen`, `Direktvermittlung/Arbeitnehmerüberlassung/Personalvermittlung`, `vermittelt an` — ≥ 0.3 | +3 | 1.00 (four labellers, four pattern sets) | 0.31–0.44 | The discriminator is grammatical number: every false positive of a loose pattern was plural or customer usage (Trusteq "für unsere Kunden", Hoorcentrum "advies aan de klant", Trading212 "our client base"). Drop `je komt terecht in een` (municipalities) and `with a Business…` (celonis) |
| T1′ | same, ≥ 0.1 | | +1.5 | 0.93–0.95 | 0.50–0.71 | |
| T2 | Anonymous voice | share of postings with neither a first-person employer voice (`wij zijn`, `we are`, `über uns`, `nous sommes`, …) nor the employer's own name in the text ≥ 0.6, n ≥ 3 | +1.5 | 0.93 (FP: Smals) | 0.65 | 100G 1.00, CONESSENCE 1.00, NOEL FRANKLIN 0.80; GitLab, Deliveroo, Korian 0.00 |
| T3 | No shared self-description | mean pairwise Jaccard of 6-word shingles over up to 12 descriptions < 0.02, n ≥ 5 | +1 | 0.89 (FP: terse public bodies — Scholengroep 8, Smals) | 0.85 | Template agencies (Taxtalente: 32 identical adverts, 0.965) miss here and are caught by T1/R4 |

**Negative signals**

| # | Signal | Points |
|---|---|---|
| D1 | Employer's own name in ≥ 60% of its descriptions, no client voice, no agency stem | −2.5 |
| D2 | First-person employer voice ≥ 0.6, no client voice | −1 |
| D3 | Anti-agency disclaimer in the advert ("no agencies") | −2 (2 rows in the corpus; negligible but exact) |
| D4 | Public or non-profit legal form: VZW/ASBL, FOD/SPF, Gemeente/Commune, Stad/Ville, OCMW/CPAS, AV, Universiteit, Ziekenhuis, Scholengroep, Ministerie… | −3 |
| D5 | Posts on its own ATS board (`ats.*` adapter) | −2 |
| D6 | Register resolved with no 78 code | −1 (weak: CONESSENCE, EDITX, 100G are agencies without one) |

Cap: an employer with < 3 postings cannot exceed *possible* on T-signals
alone; R-signals are not capped (a one-posting agency with a stem name and a
78 code is *certain*).

**Per-posting flag, separate from the employer score.** `posting_on_behalf`
is true when *this posting's own text* carries a strict T1 phrase. It switches
the document and scoring path for that posting when its employer is not
direct-likely; for a direct-likely employer it is stored and shown as a note
("this posting mentions a client") but does not switch the path — GitLab has
loose hits and mixed employers exist (Smals "Relations Partner Detachment",
CONESSENCE recruiting a recruiter for itself), and an employer-level statistic
cannot decide a single posting.

### 2.3 Score, bands and what each band licenses

Additive; evidence stored as a JSON list of the signals that fired with their
measured share.

| Band | Score | What it licenses | Top-50 (20 A / 30 D) | 103 labelled (35 A) |
|---|---|---|---|---|
| **certain** | ≥ 5 (two independent strong signals: 3 + 3, or 3 + 2) | shown as agency, acted on without review | P 1.00 · R 0.70 (14/20); 286 of 375 agency rows | P 1.00 · R 0.60 (21/35; 322 rows; 0 FP) |
| **probable** | 3 to < 5 | shown as "probably an agency"; same product path as certain | probable+: P 1.00 · R 0.95 (19/20; FN CONESSENCE) | probable+: **P 0.97 · R 0.97** (34/35; FP Greenpocket, a startup with five intern adverts and no self-description) |
| **possible** | 1.5 to < 3 | shown as "employer type not verified"; **not acted on either way** | possible+: P 1.00 · R 1.00 | possible+: P 0.95 · R 1.00; the band on its own is a coin flip (2 TP / 1–2 FP) |
| unknown | −1.5 < s < 1.5 | no badge; treated as direct | | |
| **direct-likely** | ≤ −1.5 | treated as direct | 30/30 direct employers | |

Without any registry or O-share evidence (text, name and offering code only —
the whole story for German employers): probable+ P 1.00 · R 0.80 on the top
50 (FN 100G, CONESSENCE, VIND, EHRS, all recovered by R1/R2). Independent
cross-checks of the same three signal families on other label sets: R 0.72
(21/29, 1 FP in 37) and R 0.78 (14/18, 0 FP in 25). The figures above were
measured with a title-lexicon "≥ 3 industries" +1 that this proposal drops
(§2.4); it can only move employers within *possible*/*unknown*, never across
an acted-on boundary, and §7 A2 re-measures anyway.

Reference employers, recomputed without that +1:

| Employer | Signals that fire | Score | Band |
|---|---|---|---|
| NOEL FRANKLIN BV (56) | R1 +3 · R2 +3 · T1 55/56 +3 · T2 0.80 +1.5 · T3 0.007 +1 | **+11.5** | certain |
| FORUM JOBS NV (38) | R1 78.200 +3 · R2 +3 · R3 38/38 +3 · R4 "jobs" +2 | ≥ +11 | certain |
| KONVERT HR NV (21) | R1 78.200 +3 · R2 +3 · R3 18/21 +3 · R4b "hr" +1 · T3 +1 | +11 | certain |
| ADECCO PERSONNEL SERVICES NV (20) | R1 +3 · R2 +3 · R3 19/20 +3 · R4 brand +2 | ≥ +11 | certain |
| VIND NV (12) | R1 78.100/78.200 +3 · R2 (12 rows under O) +3 | ≥ +6 | certain |
| 100G BV (26) | R2 live 1.0 +3 · T2 1.00 +1.5 · T3 0.003 +1 · D6 −1 | **+4.5** (+1.5 until the live O-probe runs) | probable (possible before N4) |
| EHRS (7) | R1 +3 · T3 +1 | +4 | probable — writes in the client's first person ("Versterk het team van ASTRID") |
| CONESSENCE BV (17) | T2 1.00 +1.5 · T3 +1 · D6 −1 | +1.5 | possible — brand list or website read only (§2.5) |
| GitLab (225) | D1 −2.5 · D2 −1 · D5 −2 | **−5.5** | direct-likely |
| FOD BOSA (21) | D4 −3 (+ at most T3 +1) | ≤ −2 | direct-likely; posts *for* named federal services (§4.3) |
| Trusteq GmbH (16) | T1 0 (plural "unsere Kunden" excluded) · D2 −1 | ≤ −1 | unknown / direct — a consultancy is an employer |

### 2.4 Signals rejected, and why

| Signal | Measured | Reason to reject |
|---|---|---|
| Title diversity (n ≥ 10–20, ratio ≥ 0.9) | P 0.58–0.82; flags GitLab, FOD BOSA, Landsverdediging, Deliveroo, TransPerfect, Alan | the trap in the brief |
| Function-family / industry spread from titles | P 0.44–0.60, R 0.15; GitLab 7–13 groups, NOEL FRANKLIN 13 | most agencies are specialised (FORUM JOBS industrial, EDITX IT, CONESSENCE finance, Taxtalente tax); large employers hire legal, HR, finance and sales too |
| Seniority span | P 0.58 | noise |
| City / NUTS-3 spread | P 0.75; FPs are multi-site employers (CleanLease 7 regions, Korian 5, Hoorcentrum Aerts 5, Defence 11) | and `vacancy.location` is a single value for 1,098 EURES rows |
| ISCO 2-digit spread (from ESCO codes) | P 0.85, R 0.55; FPs are the two ministries | the best of the spread family; kept out of v1, may return as a +1 once ESCO codes are stored (§6.1 C2) |
| `function_family` coherence | **not computable**: NULL on every EURES and Actiris row (1,335 rows); the brief's "fn-families = 1" column is that NULL, not a measurement | |
| Third-person employer voice alone ("Wie zoeken zij?") | P 0.33 | tie-breaker at most; CONESSENCE's only textual tell |
| Skipping EURES section N at collection | removed 0 agency rows (§2.5) | |

### 2.5 Two facts about the data that the detector depends on

**EURES's sector facet is NACE Rev 2.1, in which staffing is section O, not
N.** The raw search response's `NACE_CODE` facet has 22 codes a–v; NACE Rev 2
has 21 sections A–U. Rev 2.1 splits J into J (publishing, broadcasting,
content) and K (telecommunications, computing), so every later letter shifts:
N = professional, scientific and technical (69–75); **O = administrative and
support services (77–82), including 78 employment activities**; P = public
administration. Evidence: for the 24 employers whose KBO entity resolved
correctly, Rev 2.1 letters explain 72% of the sections their rows were fetched
under against 18% for Rev 2 (KONVERT 78.2 → O; ICTJOB 63/82/85 → K, O, Q;
Ziekenhuis aan de Stroom 56/68/86 → I, M, R; NOEL FRANKLIN's KBO list
71.121/25.530/70.200/43.211/78.x = C, F, N, O and its 56 rows arrived under C
56 / F 55 / O 1); a live `EMPLOYER` search with `sectorCodes=['O']` returns
9 of 11 labelled agencies 50/50 and every direct control 0/50. The facet is
not a pure copy of the register — 100G is O on VDAB and 62/63/70 in KBO — so
R2 is an independent signal, not a proxy for R1.

Consequences: `pipeline/discovery.py` excludes N "because that is where the
staffing agencies sit" — under the letters EURES actually uses, that skipped
engineering consultancies, law firms and architects while the O partition was
collected to its 500-row cap and is 29 agencies out of 36 employers. Zero
corpus rows came from N; 148 rows / 36 employers came from O. Because a
vacancy is filed under every section on its employer's list, excluding O
would not remove agencies either (NOEL FRANKLIN arrives under C and F) and
would remove genuine 77/81 employers: **O-membership is a per-employer tag,
never a partition exclusion.** The earlier finding "section N = 86,014 of
232,496 rows" measured professional services (in which many agencies also
register 70.22) and should be re-labelled in `docs/Data_Gathering_Plan.md` N2
and `tests/unit/test_planning_discovery.py`.

**The contract-type normaliser stores temp-to-hire postings as permanent.**
`positionOfferingCode` is present on every raw EURES entry (`directhire`
1,196, `temporarytohire` 736, `contract` 122, `contracttohire` 81,
`temporary` 42, `selfemployed` 30, `oncall` 20 of 2,247 unique postings), but
`adapters/vacancy_source.py::_CONTRACT_MAP` has no key for the compound codes,
`_flatten_codes` splits only on `_-/`, and the concatenated
`positionScheduleCodes` "fulltime" then maps to `permanent`. Result: of 149
stored `temporarytohire` rows, 126 are `permanent`, 22 `freelance`, 1
`interim`; FORUM JOBS is 37 permanent / 1 freelance for 38/38 temp-to-hire. The
FR-145 default `contract_types = [PERMANENT]` therefore *matches* Belgian
interim postings today. R3 needs the raw code kept, and §4.1's directive
argument assumes this is fixed (§6.1 C1–C2).

### 2.6 The ceiling, stated

Content signals alone reach 72–80% of agencies. With the Belgian register
and the EURES O-probe the measured recall is 95–97% on one labeller's sets.
What remains: selection firms with no client phrase, no agency word, no 78
code and no O filing — CONESSENCE (17 rows) and EDITX (19) in this corpus —
which only a maintained brand list or the website read of
`Agency_Research_Design.md` §3 finds (and EDITX's site is JS-only, so the
list). Germany (708 rows, 442 names) has no free activity register: text and
name only, and 145 single-posting German employers carry no statistics at
all. A "direct employers only" promise can therefore never be a clean list;
§4.1 sets the default accordingly.

---

## 3. What breaks today

| # | Stage | Measured | Consequence |
|---|---|---|---|
| 1 | **Approved package** `6a373d7c` (NOEL FRANKLIN BV, Manufacturing Engineer; 1 of 30 real packages) | status approved, consistency pass, leak-scan pass, 75 claims checked. Email: "apply for the Manufacturing Engineer position **at NOEL FRANKLIN BV** … contribute to your team's process optimization", to jobs@noelfranklin.be. Motivation `why_fit_company`: "The company values innovation… **the no-nonsense culture and short communication lines**" — quoted from the advert paragraph headed *"Onze klant: een internationale en vooruitstrevende machinebouwer in Roeselare"*. Briefing: "Company profile — Website noelfranklin.be". | FR-322 cannot catch it: `consistency.run_checks` puts `company.name` in the allow set and the company+vacancy in the leak-scan context, so every claim is consistent with the record it was given. The letter praises the agency for the client's culture; the briefing prepares the seeker for the wrong interview. |
| 2 | **Blast radius in the Apply Browser** | `apply_contact_resolution` for the 96 flagged agencies: 34 reachable (NOEL FRANKLIN `derived_confirmed`, MANPOWER, START PEOPLE, SYNERGIE, MICHAEL PAGE, UNIQUE, Accent…) carrying **210 vacancies**; 35 unreachable; 21 not attempted | 210 packages of the same shape are one click away; the recipient is right (the agency is the channel), the framing is wrong |
| 3 | **Opportunities and scores** | 70 real vacancy opportunities, 8 (11%) at flagged agencies. NOEL FRANKLIN's opportunities: `score_company` 45.0 (neutral prior), overall 49.1–61.5; `dream_fit` 55.9 with culture cue "senior team with production ownership" = *met* because "the company's own language mentions production" | culture matches computed against the wrong organisation's words; the 0.45 neutral prior puts an unknown employer mid-list by fiat |
| 4 | **Deal-breaker "no agencies"** | the dream-job model emits `{"constraint": "agencies", "hard": true, "detectable_from": ["company type"]}` (`tests/unit/test_enrichment.py:634`); `dream_criteria` token-matches it against title, description and `business_summary` → always *unknown*; the rejection placeholder "I do not want agency work" maps to `company_type.size_bands / stages`, which cannot express it | the one preference the product already elicits can never fire |
| 5 | **Company profiling** | 3 real companies profiled; 1 is FORUM JOBS NV, whose `sector_codes` already carry **78.200** from KBO — read by nothing. `build_profile` would crawl the agency's site and `_job_ad_block` hands 8 recent adverts to the model as "job advertisements published by this company" (FR-384 culture evidence): for NOEL FRANKLIN, 8 different clients (finance/admin 14, production 17, sales 7, construction 4, logistics 2 of 56) | every FR-222 field would describe Adecco/Konvert/Noel Franklin; FR-384 culture would be a blend of a flour mill, a steel yard and a fund administrator |
| 6 | **Financial analysis** | 0 analyses yet; `financial.campaign_companies` and `financial_worker` take `companies_missing_filings(min_years=5)` — every agency qualifies; `write_rationales` rejects any rationale that does not cite figures; `estimate_from_signals` writes an estimated ability-to-pay from the agency's size | the most authoritative-looking output in the product, produced for the wrong legal entity; `company_attractiveness` then ranks all of the agency's postings by that entity's trajectory |
| 7 | **Speculative openings** | 0 of 173 at agencies — only because `campaign_company_ids` was empty for the real campaign; `candidate_companies` has no guard and orders by `company_attractiveness` | "what will Konvert need to hire in 6–12 months" answered with roles at the agency, labelled as openings at "the company" |
| 8 | **Contract type** | 149 temp-to-hire rows stored as 126 permanent / 22 freelance / 1 interim; 218 EURES `freelance` rows are mostly `contract`/`selfemployed` | FR-261 mis-states the contract to the seeker; the FR-145 permanent default matches interim work |
| 9 | **Collection** | `EXCLUDED_NACE_SECTIONS = {N, T, U}` with the comment "N is where the staffing agencies sit"; 0 rows from N; O collected to cap (500 records), 29/36 employers agencies | the team believes agencies are kept out at collection; a third of the EURES slice says otherwise, and real professional-services employers were skipped |
| 10 | **Dedup** | `vacancy_similarity` weights company at 0.25, so two agency names cap at 0.75 < 0.92: cross-agency duplicates never merge (5 verbatim pairs: HOUSE OF RECRUITMENT SOLUTIONS re-posting KONVERT, Actief, Bright Plus — "Voor onze klant in Waver is Konvert op zoek naar"); Smals under three names (SMALS VZW, "Smals – …" via ICTJOB, via EDITX) = 14 rows for 5 roles | 46 rows (1.7%); small, but the seeker may apply through three intermediaries for one job |
| 11 | **Nameless EURES rows** | 38 rows with no employer; 11 name Tricobel in an "A PROPOS" paragraph, others name Equans in the title | not an agency case — an extraction gap; noted for §6.2 N12 |

---

## 4. What the product should do

The organising decision: **an intermediary is a third state of the employer,
not a data-quality filter and not a kind of opportunity.** An agency posting is
a real, advertised vacancy (so it must not borrow the FR-263 speculative badge,
which would assert something false); what is missing is the employer. Every
surface gets one new axis, `employer_role ∈ {direct, agency, board,
unverified}`, derived from the band in §2.3 (certain/probable → agency, or
board when the entity is a job board such as ICTJOB — `AGGREGATOR_DOMAINS`
already lists ictjob.be; possible → unverified; unknown/direct-likely →
direct), with its evidence, and one per-posting field for what the posting
itself says about the employer.

### 4.1 Directives (FR-143, FR-145)

Add `company_type.intermediaries: direct_only | prefer_direct | no_preference`,
**default `prefer_direct`**, provenance `default`. Two axes stay separate: who
the employer of record is (FR-143) and what the contract is (FR-145) — NOEL
FRANKLIN's 56 postings are `directhire` permanent jobs *via* an agency, which
is not an interim contract, and "staffing" is the agency's industry, not the
job's, so FR-142 `industries_exclude` is the wrong place too.

Why `prefer_direct` and not `direct_only`:

1. With the contract mapping fixed, the existing `contract_types =
   [PERMANENT]` default already removes interim contracts for seekers who did
   not ask for them; what remains from agencies is recruitment-and-selection
   into permanent jobs — real jobs a seeker may want and cannot opt into if
   never shown.
2. `direct_only` cannot deliver the clean list it promises (§2.6): making it
   the default would give false comfort.
3. The product's register is to show what it does not know, not to hide it.
4. Cost asymmetry: hiding a route to a real job is paid by the seeker; a
   labelled row costs a glance.

Semantics: `direct_only` excludes `agency` and `board` rows from the list (via
`is_excluded`, counted in the explain) and keeps `unverified` rows with their
badge; `prefer_direct` keeps everything and scores the preference in directive
fit (§4.4); `no_preference` does nothing. The hard deal-breaker "agencies"
that the dream-job model already emits maps to `direct_only`. FR-285: when the
seeker adds `INTERIM` to `contract_types`, suggest `no_preference`; when they
reject an opportunity with the "agency" bucket, suggest `direct_only`.

### 4.2 Ranked list (FR-282, FR-284)

- An `EmployerBadge` component beside `KindBadge` (`frontend/src/components/ui.jsx`),
  its own non-phase token (outlined/hatched, distinct from speculative purple),
  rendered in every view that renders `KindBadge`. Strings in §4.8.
- Company column for an agency row: **"Agency: NOEL FRANKLIN BV"** with a
  second line **"Employer: not named"** (or "Employer named in posting: FOD
  Justitie — not confirmed").
- A list filter "Direct employers only" next to the kind filter.
- The dream-fit meter gains a fourth bucket, **"cannot assess: N"**, so a
  role-only 90% is never read as a full 90%.
- No badge for `direct`; `unverified` shows "Employer type not verified" in
  a neutral tone — it is information, not a warning.

### 4.3 Opportunity screen

Two cards where today there is one:

- **Agency** — name, what kind of agency (from `service_model`: temp agency /
  recruitment & selection / job board), the evidence list in plain words
  ("KBO NACE 78.200 temporary employment agency", "38 of 38 postings are
  temp-to-hire", "55 of 56 adverts begin 'Onze klant'"), and its reviews *as
  an agency*. One control, "This is wrong", per `Agency_Research_Design.md`
  §8 (private correction, promoted by operator or consensus).
- **Employer** — "Not named by the agency", followed by *what the posting
  says* as verbatim sentences with their source: "internationale en
  vooruitstrevende machinebouwer in Roeselare"; contract as the offering code
  states it (direct placement / temp-to-hire); region from NUTS-3 and text.
  When the posting names a third party (`employer_named_in_posting`), show it
  as "named in the posting, not confirmed" until §6.2 N10 confirms it, then
  as the employer with its profile.
- The profile, financial and competitor sections render **"Not available:
  the employer is not named"** — the rule `briefing.py` already applies to
  missing financials — rather than disappearing or showing the agency's.

For relays that name the true employer in the title — FOD BOSA ("Penitentiair
bewakingsassistent – FOD Justitie", 21 rows), ICTJOB ("Smals – IT Domain Team
Lead", 70% of its 23 rows) — the same field carries the named entity; the
named entity may itself be an agency (Randstad Digital, Talencia, Egov Select
appear as ICTJOB prefixes) and goes through the detector before anything
attaches to it.

### 4.4 Scoring (FR-281, FR-282, FR-383)

| Component | Today | Decision |
|---|---|---|
| `company` sub-score | `company_attractiveness` falls to a 0.45 neutral prior when nothing is known (70 real rows at 45.0) | for `agency`/`board`: **`None` with reason "employer not named — cannot assess"**, displayed as "—", excluded from the total by `_weighted`'s renormalisation (the other six components renormalise over 0.86). Not the agency's figures (a confident number about the wrong entity), not 0.45 (unknown outranks every researched company below 45 by fiat and the choice is invisible) |
| the seeker's preference | nowhere | scored in **directive fit**: `_company_type_score` gains an `intermediary` check, weight 1.0 — 0.0 under `prefer_direct` for an agency row with the reason "posted by a staffing agency; you prefer direct employers", 0.5 for `unverified` ("employer type not verified"), absent under `no_preference`. That is where preferences live, where FR-282 shows the reason and where FR-285 learns. Exclusion alone would let "unknown beat known-bad" and fill the top of a Belgian list with agency rows |
| dream fit (`dream_criteria`) | company-characteristic and culture criteria read `company.*` of the linked company; deal-breakers token-match | for agency rows: never read `company.*`; assess `company_characteristics` and `culture_values` **against the advert text only**, with the explanation naming the advert ("the advert says: familiebedrijf, regio Deinze"); anything the advert does not state gets a new status **`cannot_assess`** ("the employer is not named"), distinct from `unknown` because it is unknowable until the recruiter names the employer. Role, responsibility, contract and location criteria stay live. The deal-breaker whose constraint matches the agency bucket (agency, agencies, interim, uitzend, intérim) is **`violated`** for an agency row — the channel *is* known; other company-type deal-breakers are `cannot_assess`, never violated, never met |
| compensation | company-derived estimates possible | stated ranges only (NOEL FRANKLIN states "tot €3800 bruto"); no company-based estimate |
| reachability | agency domain | unchanged: the agency recruiter is the legitimate contact |
| financial, speculative, competitors | unguarded | never run for `agency`/`board` (§4.7) |
| rejection buckets | "agency" → `company_type.size_bands / stages` | "agency" → `company_type.intermediaries` |

### 4.5 Generated documents (FR-322, FR-329, FR-330, NFR-206, CR-405)

For an agency row (or a `posting_on_behalf` posting at a non-direct employer):

- **CV**: unchanged — tailored to the posting's requirements.
- **Intro / apply e-mail**: to the recruiter, about the role, availability
  and region: "I am writing about the Manufacturing Engineer position you are
  recruiting for in Roeselare" — never "at NOEL FRANKLIN", never "your
  team's", never a client name. `intro_email.py`'s fallback bodies ("the
  {role} role at {company}") get an intermediary variant.
- **Motivation & fit**: keep `why_job`, `fit_job`, `objections`,
  `talking_points`; **replace `fit_company`** (today built from
  `company.values_culture/stage/sector/trajectory/size/ownership` — the
  agency's) with *"What the posting says about the employer"* (verbatim
  descriptors) and *"Questions for the recruiter"* (who the employer is, why
  the role is open, interim or direct hire, when the name is disclosed). The
  prompt's hard rule 2 ("every company statement traces to the supplied
  company record") is exactly what licensed the NOEL FRANKLIN letter; an
  `intermediary_note` goes through the same slot as `speculative_note` and
  the rule becomes "no statement about the employer beyond what the posting
  says".
- **Briefing**: agency profile (kind, specialisation, NACE, reviews as an
  agency); "Employer: not named" with descriptors; sector notes for the
  interview; process notes (agency screening, then client interview);
  financial and competitor sections that say "not available: employer not
  named". FR-329's regenerate-on-demand is the mechanism for the moment the
  recruiter names the client: re-run against the real company.
- **Guard**: a `check_intermediary_material(parts, agency, posting_text)` in
  `documents/consistency.py`, strict like `check_generated_material` for
  speculative kinds: (a) agency-as-employer phrasings ("at {agency}", "join
  {agency}", "{agency}'s team/culture/values", "bij/chez/bei {agency}") are
  findings of severity high; (b) any organisation name
  (`capitalised_phrases` + `_looks_corporate`) absent from posting ∪ agency
  name ∪ the seeker's own facts is a leak, reusing `scan_leakage` with the
  posting as provenance; (c) the disclosure note must be present. The
  Apply Browser cannot approve a package with an open finding of this kind.

### 4.6 Contacts and the Apply Browser (FR-301–FR-306)

`apply_contacts.py` and `contacts.py` do not change: the ladder reaches the
agency recruiter, who is the right recipient, and the agency's stated channel
is the application target in 100% of agency rows. What changes is around the
recipient: the Apply Browser row carries the badge before "generate"; the
package uses the §4.5 path; nothing addresses the agency as the employer. Two
adjacent items belong to the same pass: the namesake gate hardening of
`company_named_on_page` / `confirm_domain` (`Agency_Research_Design.md` §3.2:
brightplus.com, adequat.com, climate.com stand for the wrong organisations
today) is a prerequisite for trusting any derived domain, agency or not; and
`AGGREGATOR_DOMAINS` feeds `employer_role = board`.

### 4.7 Collection, adapters and the knowledge-base gates

- `eures.py::_one()` keeps `positionOfferingCode`, `locationMap` (NUTS-3 list)
  and `jobCategoriesCodes` (ESCO URIs) — today all three are dropped, and
  they are the interim badge, the region for distance directives and the
  occupation code for scoring.
- Contract mapping (§2.5): `temporarytohire`/`temporary` → interim;
  `contracttohire`/`contract` → fixed_term; `selfemployed` → freelance;
  `directhire` unmapped so the prose decides, defaulting to permanent as
  today; the offering code is normalised *before* the schedule code.
- `discovery.py`: re-label the section vocabulary as Rev 2.1, include N,
  keep T/U/V out; the per-row facet is never used as the employer's NACE.
- Gates: `company_profile.build_profile` skips the job-ad block and labels
  the profile "agency, not the hiring employer"; `financial.campaign_companies`
  / `financial_worker`, `speculative.candidate_companies` and the competitor
  pass exclude `agency`/`board` and report the exclusion count;
  `speculative_openings.md` gets a hard rule.
- Consultancies (Trusteq, Eraneos, Westernacher, Rügamer & Steiner, Towa —
  109 postings) stay `direct`: the employer is known and profilable, the
  workplace is not stated. Their plural "unsere Kunden" is deliberately
  outside T1. A later FR-143 value `consultancy` needs its own signal (NACE
  62.02/70.22 plus plural on-behalf language) and is not in this plan.

### 4.8 The wording

Badge — en "Via agency · employer not named" · nl "Via bureau · werkgever niet
genoemd" · fr "Via agence · employeur non nommé". Named variant: "Via agency ·
employer named in posting, not confirmed". Unverified: "Employer type not
verified". Board: "Via job board".

Opportunity note (`DISCLOSURE_NOTES` shape, en/nl/fr): *"This vacancy was
posted by NOEL FRANKLIN BV, a recruitment agency, for an employer it does not
name. We have not profiled that employer, read its accounts or judged its
ability to pay, because we do not know who it is. The score rests on the role
only; the company-related criteria are marked 'cannot assess'. The posting
says: production company, region Roeselare, international group. Ask the
recruiter who the employer is before an interview — then regenerate the
briefing."*

Directive help (`help/content.js` `/directives`): *"Agencies post real jobs
for employers they do not name. 'Direct employers only' hides postings we can
identify as agency-posted — about three in four of them; we cannot identify
the rest from the advert alone. 'Prefer direct employers' keeps them,
labelled, and ranks them below a direct employer of equal fit. Choose 'No
preference' if interim or agency work is what you want; if you add interim
contracts under Work arrangement we will suggest it."*

Glossary "Agency posting": *"A vacancy advertised by a staffing, interim or
recruitment agency on behalf of an employer that is not named in the advert.
The job is real; the employer is unknown to us until the recruiter names it.
Everything we normally say about a company — its accounts, trajectory,
values, likely openings — is marked 'cannot assess' for these postings rather
than guessed."*

---

## 5. What we will not do

### 5.1 We will not name a guessed end client. The rule and the argument.

Client identity on a posting takes exactly three values:

- **`undisclosed`** — the default, and the truth for 83–99% of agency rows
  depending on how "named" is counted (§Appendix A). Forbids any employer
  name in letters, profiles, financials and speculative openings. Enforced in
  the generator's input contract (`employer_disclosed = false`) and by the
  §4.5 guard, not by prompt wording.
- **`named_in_posting`** — the posting text names an organisation in a
  client-referring clause ("voor onze klant DHL Supply Chain, een sterk
  logistiek bedrijf in Boortmeerbeek") or a "Name – …" title prefix. Stored
  with the quoted sentence; shown as "named in the posting, not confirmed";
  attaches nothing. The LLM extractor confirms the clause means *employer*
  and not *customer of the employer*, and the named party runs through the
  detector (Randstad Digital is a name, and an agency).
- **`confirmed`** — the named entity passed the existing `apply_contacts`
  confirmation gate and a registry resolution under the hardened name gate.
  Only this value attaches a company profile, accounts and a letter about
  "why there".

Why there is no fourth value "inferred with probability p", and why the
threshold is a rule rather than a number:

1. **The inference does not converge.** The eight richest client descriptions
   in the corpus (prefab concrete near Komen; skylights in Mouscron; a
   machine-builder in Roeselare; a fund manager in Roeselare with 30+ staff;
   frozen vegetables in Mouscron…) were mapped to a NACE-BEL code and a
   postcode and run against the KBO activity search: **0 of 8 resolved to one
   company** — candidate sets of 12, 16, 2, 16, 14, 20+, 0 and 13, and the 0
   shows the code/postcode read from prose is itself a guess one time in
   eight. The best case (38.320 at 7700) leaves two firms the prose cannot
   honestly separate.
2. **There is nothing to calibrate against.** Across ~500 agency rows, a
   client-referring clause names a real employer 1–5 times; body-identical
   cross-posting between an agency and a direct employer — the one mechanism
   that could attach a name without guessing — fired **0 times in 535**
   (every identical pair was agency-to-agency or a name variant of one
   company). A probability threshold on description matching would be an
   invented number.
3. **The cost is asymmetric and the reader is expert.** A wrong name goes
   into a letter read by the agency recruiter who knows the client — the same
   class of fabrication as an invented e-mail address, and worse than saying
   nothing, because it is confident. A sector-and-town guess ("the tyre
   centre in Zwevegem") is a fabricated company in a document a real person
   reads. The measured downside of refusing is a missing paragraph; the
   downside of guessing is CR-405.
4. **What the record does contain is enough for what the score needs.**
   Title and duties 100%, skills 68%, ESCO occupation 99%, offering type 89%,
   NUTS-3 region 94% (in the raw), verbatim client sector/culture description
   30%, the agency's own registry identity — enough for profile fit, dream
   fit on the role, and directive fit. Only the company dimension is missing,
   and §4.4 says so.

At most the product may offer the KBO shortlist for a described client as
"companies in this sector and area", labelled as such, never as "the
employer" — and that is not in this plan.

### 5.2 Other refusals

1. **No diversity, family-spread, seniority-span or city-spread signal**, at
   any weight (§2.4). The first two destroy GitLab, the ministries and every
   multi-site employer; the failure mode is hiding a real company.
2. **No `O` in `EXCLUDED_NACE_SECTIONS`**, and no more calling the N-skip an
   agency control (§2.5).
3. **No folding of consultancies into the agency flag** (§4.7).
4. **No hiding by default**: `prefer_direct`, not `direct_only` (§4.1).
5. **No 0.45 neutral company score, no agency balance sheet under a client
   posting**, no financial, speculative or competitor run on an intermediary
   (§4.4, §4.7).
6. **No loosening of `VACANCY_MATCH_THRESHOLD` or of the 0.25 company weight**
   to merge cross-agency duplicates; a *link* ("same posting via another
   channel") is the most that is safe (§6.2 N11).
7. **No cross-posting inference now**: 0 of 535 today on a 0.5% sample of
   Belgian EURES; re-measure after a dense regional collection before
   building it.
8. **No shared relabelling from one seeker's correction** without promotion
   (`Agency_Research_Design.md` §8): the machine's verdict at least carries a
   quote.
9. **No per-vacancy text rule reclassifying an employer**: GitLab has 28
   loose hits in 225 adverts; the employer-level majority is what keeps it
   out (§2.2).
10. **No new external dependency for v1**: the ESCO→ISCO resolution (311 API
    calls) and the website LLM rung are phase 2 and 3 respectively.

---

## 6. Implementation plan

Estimates are working days for one developer who knows the codebase.
Migration numbering continues from `100_apply_browser.sql`; the `110` in the
earlier note was a placeholder.

### 6.1 Config and one-line changes — about 1.5 days, do first

| # | Where | Change | Why | Effort |
|---|---|---|---|---|
| C1 | `adapters/vacancy_source.py::_CONTRACT_MAP` | add `temporarytohire`, `temporary` → interim; `contracttohire`, `contract` → fixed_term; `selfemployed` → freelance; leave `directhire` to prose | 736 of 2,247 raw EURES postings say temp-to-hire and are stored as permanent | 0.25 (with a recorded-response fixture) |
| C2 | `adapters/jobboards/eures.py::_one()` | normalise `positionOfferingCode` first, then the schedule; return `offering_code`, `nuts_codes` (from `locationMap`), `esco_codes` (from `jobCategoriesCodes`) | R3, distance directives, the interim badge; all three are discarded today | 0.5 |
| C3 | `pipeline/discovery.py:97–110`, `eures.py:45,379`, `docs/Data_Gathering_Plan.md` N2, `tests/unit/test_planning_discovery.py:309` | section letters are NACE Rev 2.1: O = 77–82 (staffing), N = 69–75; include N, exclude T/U/V; rename the test; 7 × 19 = 133 partitions | §2.5 | 0.25 |
| C4 | `pipeline/data/agency_names.json` | stems, ambiguous whole words, national brand list, and the corpus's silent agencies (100G, EDITX, CONESSENCE, VIND, EHRS, Kingfisher, TECHNICAL HR EXPERTS, Taxtalente) with a `source` field each | R4/R4b; the brand list is maintained from the detector's own *certain* output | 0.25 |
| C5 | `pipeline/apply_contacts.py::AGGREGATOR_DOMAINS` | expose as an input to `employer_role = board` | ICTJOB (23 rows) is a job board with an employer row | 0.1 |
| C6 | `pipeline/knowledge_base.py::DEFAULT_STALENESS_DAYS` | `employer_kind`: 365 for a registry verdict, 180 for a text/behaviour verdict | refresh policy | 0.1 |

### 6.2 Code — in dependency order

| # | What | Depends on | Effort | Phase |
|---|---|---|---|---|
| **N1** | **Migration `101_employer_kind.sql` + repository.** Table `company_employer_kind` as designed in `Agency_Research_Design.md` §6 (`kind`, `service_model`, `confidence`, `method`, `evidence` JSON, `identity_evidence`, `established_at`, `expires_at`, `anomalies`) plus `score REAL`, `tier TEXT` (certain\|probable\|possible\|unknown\|direct_likely) and the derived `employer_role`; `employer_kind_correction` from §8 of that note. Vacancy columns: `offering_code`, `nuts_codes` JSON, `esco_codes` JSON, `posting_on_behalf` INT, `employer_named_in_posting` TEXT, `employer_named_sentence` TEXT, `employer_descriptors` JSON. Opportunity: `employer_company_id` (nullable, phase 2). Nothing about a person (RK-08). `db/repositories/employer_kind.py`: upsert-with-audit, read, queue, per-employer aggregates query | C2 | 1.0 | 1 |
| **N2** | **`pipeline/employer_kind.py` — the detector.** Per-employer aggregates (R3, R4/R4b, T1/T1′, T2, T3, D1–D6, R2-from-provenance, R1-from-`sector_codes`), the additive score, bands, evidence JSON; per-posting `posting_on_behalf`, `employer_descriptors` (verbatim client sentences), `employer_named_in_posting` (proper name with legal form in a client clause, or "Name – " prefix). Runs on ingest for the employer of each written vacancy and as a KB pass. Fixtures: the labelled sets from the four evaluations merged into `tests/fixtures/agency_labels.json` (≥ 122 employers) and a `tools/employer_kind_eval.py` that prints P/R per band | N1, C4 | 3.0 | 1 |
| **N9** | **Gates.** `company_profile.build_profile` (skip job-ad block, label), `campaign_companies` in `company_profile.py` and `financial.py`, `financial_worker`, `speculative.candidate_companies` (+ `SpeculativeReport.excluded_intermediaries`, prompt rule), competitor pass | N2 | 1.0 | 1 |
| **N5** | **Scoring.** `company_attractiveness` → `None` + reason; `_company_type_score` intermediary check; `dream_criteria` advert-only evidence and `cannot_assess`; agency deal-breaker; `_REJECTION_BUCKETS`; explain payloads; unit tests on opportunity `be523580` | N2 | 1.5 | 1 |
| **N6** | **Directive.** `CompanyTypeDirectives.intermediaries`, proposal default + provenance, `is_excluded` for `direct_only`, FR-285 suggestions, DirectivesPage control, help text | N5 | 1.0 | 1 |
| **N7** | **Presentation.** `employer_kind.presentation()` (labels/notes en/nl/fr, evidence in words) joined in `api/routers/opportunities.py:120` and the Apply Browser list; `EmployerBadge` + token; list filter; opportunity-screen cards; "not available" sections; fourth meter bucket; companies page | N2 | 2.0 | 1 |
| **N8** | **Documents.** `motivation.py` (`fit_company` → what-the-posting-says + questions; `intermediary_note` slot; prompt rule), `intro_email.py` / `apply_email.md` wording, `briefing.py` sections, `consistency.check_intermediary_material` strict, `package.py` wiring, labels en/nl/fr; regression test that regenerates package `6a373d7c` | N7 | 3.0 | 1 |
| **N13** | **Backfill.** Re-normalise contract types and offering codes from the 127 raw EURES documents; run N2 over the corpus; re-score affected opportunities; mark package `6a373d7c` for regeneration | N2, N5 | 0.5 | 1 |
| **N3** | **Registry pass (BE; GB/NL when keys exist).** `employer_kind.registry_pass` on `KBOAdapter`: hardened `pick_search_result` (normalised names equal, every queried token present, ENT rows only, stopword-only matches never count, one-token names need a municipality cross-check, legal-persons-only variant of `NAME_SEARCH_FORM` for bare tokens such as "100G"); `parse_company_page` returns `{regime, version, code}` so 78.2/78.1 rules can tell NSSO from VAT lists; writes `legal_id`, `sector_codes`; SIC 781/782/783 and SBI 78 mapping. 458 BE names × ~4 s ≈ **32 min once**, at the adapter's 0.5 rps. Includes the shared namesake-gate v2 in `apply_contacts` (1.0 of the 2.5) | N1 | 2.5 | 1b |
| **N4** | **EURES O-probe.** One `EMPLOYER` POST with `sectorCodes=['O']` per BE employer with ≥ 3 postings and no R1/R2 evidence yet, page-1 exact-name share, cached on the verdict row. ≤ 458 × 10.5 s ≈ **80 min once** under europa.eu's Crawl-delay. This is what takes 100G from *possible* to *probable* without a list | N1, N3 | 1.0 | 1b |
| **N10** | **Named-employer confirmation.** When `employer_named_in_posting` is set: confirm through `apply_contacts.confirm_domain`/`company_named_on_page` (gate v2) and N3; run N2 on the named entity; on `confirmed` set `opportunity.employer_company_id` and re-run scoring and documents against it | N3, N8 | 1.5 | 2 |
| **N11** | **Same-posting links.** For `agency`/`board` rows, description hash or token-Jaccard ≥ 0.5 with title similarity ≥ 0.92 within 28 days → a link ("also posted via …"), never a merge; strip "Name – " prefixes for board relays | N2 | 1.0 | 3 |
| **N12** | **Nameless EURES rows.** Extract the employer from an "A PROPOS / Over ons" block or the title (Tricobel 11, Equans) | C2 | 0.5 | 3 |
| — | Website LLM rung (`Agency_Research_Design.md` §3, ≈ 4 d) | N3 | — | 3: only if the brand list proves unmaintainable; its measured incremental catch is CONESSENCE-class firms with readable sites |

### 6.3 Sequence and totals

- **Phase 1 — closes the realised and the latent path.** C1–C6 (1.5) → N1
  (1.0) → N2 (3.0) → N9 (1.0) → N5 (1.5) → N6 (1.0) → N7 (2.0) → N8 (3.0) →
  N13 (0.5) = **14.5 days**. After N2 + N9 (day 7) no agency is profiled,
  analysed or speculated on; after N8 (day 14) no agency package can be
  approved.
- **Phase 1b — recall from 72–80% to 95%+ on Belgian employers.** N3 (2.5)
  + N4 (1.0) = **3.5 days**; N3 can start on day 2 in parallel, and its
  namesake gate is worth shipping alone.
- **Phase 2 — attach a confirmed named employer.** N10 = 1.5 days.
- **Phase 3 — hygiene.** N11 + N12 = 1.5 days.

**Total ≈ 21 days; 18 for phases 1 and 1b.** Nothing costs tokens except the
regenerated agency packages; the two registry passes are one-off and cached
for a year.

---

## 7. Acceptance criteria

Each is phrased so a reviewer can run it on `data/dreamjob.db` after N13.

| # | Check | Pass condition |
|---|---|---|
| A1 | **Reference employers.** `SELECT c.name, k.tier, k.score, k.employer_role FROM company c JOIN company_employer_kind k ON k.company_id = c.id WHERE c.name IN (…)` | NOEL FRANKLIN BV, FORUM JOBS NV, KONVERT HR NV, ADECCO PERSONNEL SERVICES NV, ABSOLUTE@WORK BV, VIND NV: `certain`, score ≥ 5. GitLab: `direct_likely`, score ≤ −1.5 (expected −5.5). FOD BOSA, Ministerie van Landsverdediging, SMALS VZW, Vulpia, Korian, Hoorcentrum Aerts, Trusteq GmbH, Eraneos, CleanLease NV, ISS FACILITY SERVICES NV: never `probable` or `certain`. 100G BV: `possible` after phase 1, `probable` after N4. CONESSENCE BV and EDITX BV: `probable` only via the brand list — documented, not hidden |
| A2 | **Labelled set.** `python -m dreamjob.tools.employer_kind_eval tests/fixtures/agency_labels.json` | probable+ precision ≥ 0.95 and recall ≥ 0.80 without R1/R2, recall ≥ 0.90 with; certain precision 1.00; no direct employer with ≥ 20 postings above `possible`; the tool prints the confusion matrix per band |
| A3 | **Contract types.** `SELECT contract_type, COUNT(*) FROM vacancy WHERE offering_code = 'temporarytohire' GROUP BY 1` | one row: `interim`, 149 (or the current count). FORUM JOBS NV: 38 interim. `SELECT COUNT(*) FROM vacancy WHERE source_adapter = 'board.eures' AND offering_code IS NULL AND …raw exists` = 0 |
| A4 | **Package regeneration** for opportunity `be523580` (NOEL FRANKLIN, Manufacturing Engineer) | email contains none of "at NOEL FRANKLIN", "your team", "join NOEL FRANKLIN"; motivation has no `why_fit_company`, has "What the posting says" with the verbatim *Onze klant* sentence and "Questions for the recruiter"; briefing has "Employer: not named" and the financial section reads "not available"; consistency report has 0 intermediary findings. Then inject "I admire NOEL FRANKLIN's engineering culture" into the email part: `check_intermediary_material` raises, severity high, and the Apply Browser refuses approval |
| A5 | **Scoring** for the same opportunity via the explain endpoint | `score_company` NULL, reason "employer not named — cannot assess"; with `prefer_direct` the directive-fit reasons include "posted by a staffing agency"; with `no_preference` they do not; with `direct_only` the row is absent and the explain counts it as excluded; the dream-fit meter shows `cannot_assess ≥ 1` with every `company_characteristic` in it; a seeker whose dream-job record carries the "agencies" deal-breaker sees it `violated` |
| A6 | **Gates.** Run the profiling, financial and speculative workers on a campaign whose provenance includes NOEL FRANKLIN and FORUM JOBS | `company.business_summary` unchanged (or labelled agency and built without the job-ad block); `financial_analysis` has 0 rows for `employer_role = 'agency'`; `SpeculativeReport.excluded_intermediaries ≥ 2` and no speculative opportunity at an agency |
| A7 | **List and browser.** Open `/opportunities` and `/browser` | every row of an `agency` employer carries the badge; "Direct employers only" removes `agency`/`board` rows and keeps `unverified` rows with their badge; the Apply Browser row shows the badge before "generate"; the 210 reachable-agency vacancies all render it |
| A8 | **Directive default.** Create a fresh proposal | `company_type.intermediaries = prefer_direct`, provenance `default`; adding `INTERIM` to `contract_types` produces the `no_preference` suggestion; a rejection "I do not want agency work" lands in the `intermediaries` bucket |
| A9 | **Collection vocabulary.** `pytest tests/unit/test_planning_discovery.py` and `grep -rn "staffing" backend/dreamjob/pipeline/discovery.py backend/dreamjob/adapters/jobboards/eures.py` | partitions include N (133 for BE+NL); no comment or docstring claims N filters agencies; the plan doc's N2 line is corrected |
| A10 | **No invented employers.** Regenerate every package at an `agency` employer; extract organisation names from all parts | every name is in posting text ∪ agency name ∪ the seeker's CV facts — 0 exceptions |
| A11 | **Named-in-posting.** `SELECT employer_named_in_posting, employer_named_sentence FROM vacancy WHERE employer_named_in_posting IS NOT NULL` | ≤ ~80 rows; each sentence contains the name verbatim; a human review of the list (an afternoon) finds no sector-and-town guesses; after N10, only rows with a confirmed entity carry `opportunity.employer_company_id` |
| A12 | **Registry pass.** After N3: `SELECT COUNT(*) FROM company WHERE legal_id IS NOT NULL AND country = 'BE'` | ≥ 350 of 458 BE employers resolved; DE BRANDT NV, TOURING NV, KORIAN BELGIUM, SMALS not resolved to a namesake (the six wrong picks from the evaluation are the regression fixture); registry/text disagreements listed for review and near zero |
| A13 | **Privacy.** `PRAGMA table_info` on the new tables/columns | no column holds a person's name, address or e-mail (RK-08, FR-306) |

---

## Appendix A — disagreements between the evaluations, resolved

| Question | Views | Resolution |
|---|---|---|
| How large is the agency share? | 8% (name list, the brief) · 18% certain+probable / 24% with possible · 18.3% union / ~12% precise core · 19.9% hand-labelled · 17–20% | **17–20% of real rows, 33–37% of named Belgian EURES rows.** The spread is labelling boundary, not measurement error; the shared label set in §7 A2 settles it |
| Which EURES section holds staffing? | "N = staffing" (planner, Fable's earlier finding, Eval 4's reading) vs "O under NACE Rev 2.1" (Eval 1) | **O.** Rev 2.1 letters explain 72% of fetch sections vs 18% for Rev 2; the live O-probe is 9/11 agencies vs 0 direct; Eval 2 independently found O "dominated by agencies" and Eval 4's own facet shows o 57,482 ≫ n 30,608. Eval 4's "agencies arrive under the client's sector" is contradicted by NOEL FRANKLIN's 56 rows (fund controller included) all arriving under C: the facet follows the *posting employer's* sector list |
| Is "coherence" computable? | "not on EURES rows — `function_family` is NULL; title groups tie GitLab and NOEL FRANKLIN at 13" vs "yes, as voice: Jaccard 0.303 vs 0.007, named 100% vs 0%" | **Both right about their measure.** Title-derived coherence is rejected (§2.4); voice coherence is the core of the detector (T2, T3, D1, D2) |
| Precision of the offering-code signal | P 0.52 (Eval 3) vs 1.00 (Evals 1, 2, 4) | **1.00 for `temporarytohire + temporary`** only; Eval 3 counted every non-`directhire` code and picked up public fixed-term appointments |
| How often is the end client named? | 0.2% (client clause only) · ~1% (proper name after voor/pour/for) · ~5% (incl. ICTJOB prefixes) · 17% (any third-party legal name anywhere) | **Three different definitions, all correct.** In a client-referring clause: 1–5 rows. As a relay title prefix: 40–60 rows, a share of them other agencies. Loose proxy: 82 rows, an upper bound including customers and partners. The product acts on the strict definitions (§5.1); N2's extractor plus a one-afternoon review (A11) settles the count |
| Company sub-score for an agency row | neutral with a reason · "not applicable" · `None` excluded + preference in directive fit | **`None` + directive fit** (§4.4): the only option that neither invents a number nor lets unknown beat known-bad |
| FOD BOSA | agency (website classifier: werkenvoor.be) · direct public employer · named intermediary | **Direct-likely public body whose postings name the true employer** in the title; handled by `employer_named_in_posting`, not by the agency flag. The seeker's "no agencies" must not hide federal jobs |
| Consultancies (Trusteq, Eraneos, Rügamer & Steiner…) | agency (Rügamer, one evaluation) vs employer (three) | **Employer.** Rügamer & Steiner is a Personalberatung by its own text and stays flagged by T1; the IT consultancies are employers whose plural "unsere Kunden" is excluded by design |
| Per-posting client-phrase rule | "keep as an override" vs "not safe: GitLab 28/225 loose hits" | **Strict patterns only, and it never reclassifies a direct-likely employer** (§2.2) |
| Default directive | hide agencies by default (implicit in "filter") vs `prefer_direct` | **`prefer_direct`** for the four reasons in §4.1 |
| Where the website LLM rung fits | the earlier note's rung 2, 96.4% on 55 readable sites | **Phase 3, optional.** Registry + O-probe + voice reach 95%+ on Belgian employers; the rung's incremental catch is CONESSENCE-class firms with readable sites, and the two silent ones here (100g.be, editx.eu) have no readable text. Its namesake-gate work (§3.2 of that note) is adopted now, in N3 |
| The earlier note's "free signals decide nothing" | measured on one signal (client phrase) over seven employers | **Superseded by the combination:** the single phrase is R 0.31–0.44; name + offering + voice together are P 1.00 / R 0.80 at probable+ without any registry |
| Migration number | 101 (evaluations) vs 110 (earlier note) | **101** — next after `100_apply_browser.sql` |

## Appendix B — numbers that would change the plan if wrong

1. **Precision of `probable` on a larger, product-owner-reviewed label set.**
   0.97 on 103 employers by one labeller; if it falls below 0.90, `probable`
   should be shown as *unverified* and only `certain` acted on. Settle with
   A2 on the merged fixture.
2. **Whether the EURES facet follows the posting employer's sector list.**
   72% explained on 24 employers. Settle by joining N3's KBO codes for all
   458 BE employers to their fetch sections.
3. **Precision of O-membership after the 77/79–82 rule.** 29/36 raw. Settle
   with the same join.
4. **The named-in-posting rate.** 1–17% by definition; the product's promise
   depends only on the strict one. Settle with A11.
5. **Cross-posting with direct employers.** 0/535 on a 0.5% sample. Settle
   after one dense regional collection (plan N2) before building N11's
   cross-employer variant.
6. **The German share.** 4.5–5.5% by text and name alone; there is no
   register to check. If the Arbeitnow slice grows, the text ceiling matters
   more.
7. **The proportional agency share of a Belgian campaign.** 87–92% of Flanders'
   weekly rows carry section O; if the collection stops partitioning 50 rows
   per (region × section), the ranked list is mostly agency rows and
   `prefer_direct`'s penalty becomes the dominant sort key — which is the
   intended behaviour, but worth seeing once.

## Appendix C — evidence

Scratchpad (`/private/tmp/claude-502/-Users-nstephane-Dev-AI-Data-Science-training-dreamjob/ba765716-77c6-4e31-8026-66b71a865222/scratchpad/`):
`extract_eures.py`, `eures_raw.json`, `textpat*.py`, `namepat.py`,
`features.py`, `kbo_probe.py` / `kbo_probe.json`, `esco_fetch2.py`,
`eures_live.py`, `eures_sectorN.py`, `eures_O.py` / `eures_O.json`,
`evaluate*.py` (`eval6.out` holds the tier tables); `agency_probe.py`,
`agency_probe2.py`; `corpus.json`, `clientdesc.py` / `.json`,
`crosspost2.py` / `.json`, `labels.py`, `kbo_probe.sh`, `kbo_name.sh`,
`kbo_detail.sh`, `kbo_*.html`; `probe1.py` … `probe9.py`.

Database checks made for this document (read-only): row counts by adapter;
EURES plan items per section (O and Q at the 500-record cap, N absent);
employers with a row fetched under O (36 employers / 148 rows — listed in
§2.5); contract-type distribution of EURES rows (permanent 836, freelance
218, interim 40, fixed_term 9, NULL 33).

Code referenced: `backend/dreamjob/pipeline/scoring.py` (`DEFAULT_WEIGHTS`
:92, `_weighted` :191, `_company_type_score` :557, `company_attractiveness`
:703 with the 0.45 prior at :756, `dream_criteria` :904, deal-breakers
:1047, `_REJECTION_BUCKETS` :1656), `speculative.py` (`KIND_LABELS` :86,
`DISCLOSURE_NOTES` :99, `check_generated_material` :182, `presentation`
:201, `candidate_companies` :516), `directives.py` (`ContractType` :150,
`CompanyTypeDirectives` :292, defaults :1225–1232), `discovery.py`
(`NACE_SECTIONS` :106, `EXCLUDED_NACE_SECTIONS` :110), `company_profile.py`
(`_job_ad_block` :445, `campaign_companies` :927), `financial.py`
(`campaign_companies` :1156, `financial_worker` :1255), `apply_contacts.py`
(`AGGREGATOR_DOMAINS` :165, `company_named_on_page` :319, `resolve_company`
:786), `documents/motivation.py` (:203–216, :359–364), `briefing.py` (:234),
`consistency.py` (`run_checks` :835, allow set :850), `adapters/vacancy_source.py`
(`_CONTRACT_MAP` :298, `_flatten_codes` :323), `adapters/jobboards/eures.py`
(`_one` :517), `adapters/registries/kbo.py` (`NAME_SEARCH_FORM` :53,
`NAME_MATCH_FLOOR` :64, `pick_search_result` :309),
`api/routers/opportunities.py` :120, `frontend/src/components/ui.jsx`
(`KindBadge` :138), `pages/OpportunitiesPage.jsx` (kind filter :137, meter
:404, rejection placeholder :1262), `styles/theme.css` :93, `help/content.js`
(`/directives` :135, `GLOSSARY` :452), `tests/unit/test_planning_discovery.py`
:309, `tests/unit/test_enrichment.py` :634.
