# Employer kind — what the resolver actually did

*Integration and verification run, 9 September 2026, against the installed
corpus in `data/dreamjob.db` (1,430 company rows, 1,216 of them with at least
one vacancy, 2,703 vacancies).*

The question this feature answers is: **is the organisation named on a posting
the employer, or an intermediary posting on behalf of an employer it does not
name?** Everything the product does well downstream — the company profile,
five years of filed accounts, the values match, the letter about why you want
to work *there* — is computed against the wrong organisation when the answer
is "intermediary", and looks authoritative while being wrong.

One number matters more than any other here, so it goes first:

> **No direct employer was tagged as an agency on either labelled set.
> Precision 1.00 on both. One direct employer in the wider corpus was
> mislabelled — WattFox, three vacancies — and it is named in §3.4.**

The honest headline is that the resolver is **precise and incomplete**:
it answered 267 of 1,216 employers (22%), it was right about every one of
them on the labelled sets, and it said "I cannot tell" about the other 949
rather than guessing. The proposal's `P 1.00 / R 0.95` is not reproduced: the
measured recall is **0.78–0.79**, and §3.3 explains exactly why, because the
gap is a design decision rather than a defect.

---

## 1. What was integrated

Seven slices landed concurrently. Bringing them up as one system took the
following, all of which is in the working tree:

### 1.1 Migrations 110–113

All four apply in order on an empty database (`001 … 100, 110, 111, 112, 113`).

Two of them had **drifted from what was installed**: 110 and 113 were applied
at 07:41 and then edited by their owning slices while the other six were still
in flight. The installed database was missing 110's two role-repair triggers
and carried 113's first-draft `employer_resolve_run` (no `visited`, `reused`,
`needs_review`, `vacancies_covered`, `by_kind`, `by_rung`, `by_reason`, and
none of its CHECKs) — the resolver would have failed on `no such column` the
first time a pass tried to journal itself.

Neither migration is committed or released and all three tables were empty, so
they were **rebuilt from the files** rather than patched forward: the three
tables dropped, ledger rows 110/113 removed, `migrate()` re-run. The installed
schema is now byte-for-byte identical to a fresh migrate, verified by diffing
`sqlite_master` against a clean probe database, and the recorded checksums
match the files (`110 27255541d0555e2e`, `111 57ca4c5b44544922`,
`112 d173175b8c9b4b41`, `113 754948a05d56a19c`).

### 1.2 What the concurrent work had broken, and what fixed it

| Symptom | Cause | Fix |
|---|---|---|
| 3 errors in `test_mail_dispatch.py` — `UNIQUE constraint failed: job_seeker.email` in a module that isolates itself correctly and passes alone | `pyproject` sets `tmp_path_retention_policy = "failed"`, so a passing test's `tmp_path` is deleted and pytest hands the *same numbered path* to a later test; `db/connection.py` caches one connection per thread **per path string**, so the second test wrote into the first one's deleted inode, which still held its rows | an autouse fixture in `tests/unit/conftest.py` that forgets the connection cache around every test. Microseconds, and it removes the whole class of failure |
| 3 failures in `test_mail_dispatch.py` — a follow-up came back `queued` when the test said `sent` | `mail/dispatcher._deliver` stamped `sent_at` from `datetime.now()` while the FR-325 rails had been evaluated at the caller's `now`. The pacing ledger then disagreed with the guard that had just passed, and the next send was refused as `rate_limited` against a time nobody had sent at | `now` threaded through `_deliver` and its three call sites; `sent_at`, `follow_up_due_at` and `follow_up_sent_at` all stamped from the moment the rails were checked |
| 7 failures in `test_documents.py` | the same connection-cache leak, order-dependent | fixed by the same fixture |
| `classify.employer_kind` routed to the strong model for any caller that did not pass `prefer_strong=False` | task id absent from `TASK_CHEAP` | added |
| an operator could not configure employer-kind staleness (`set_staleness_policy` rejects unknown record types) | no `employer_kind` key in `DEFAULT_STALENESS_DAYS` | added at 180 days, matching the website rung's own expiry |
| a fresh clone had **none** of `backend/dreamjob/pipeline/data/*` — the skills taxonomy, the board registry, the agency name lexicon — because an unanchored `data/` rule swallowed them | `.gitignore` (already anchored by the signals slice); the files were still untracked | `git add backend/dreamjob/pipeline/data/` (8 files) |
| `adapters/jobboards/eures.py:46` described NACE section N as "staffing agencies" | Rev 2 label on a Rev 2.1 facet | corrected to "professional/scientific/technical activities" |
| two ruff errors (`F841` in `test_board_registry.py`, `F401` introduced by the dispatcher edit) | — | fixed |

**Result: `1262 passed, 0 failed, 5 deselected` (`-m "not llm"`), ruff clean over
`backend/dreamjob` and `tests`, `npm run build` clean.** Before this run the
same suite was `1249 passed, 10 failed, 3 errors`.

### 1.3 What was wired that nothing had wired

The frontend slice built five components and correctly reported that **nothing
imported them** — they compiled and rendered in isolation but were unreachable
from any screen. The product slice likewise reported that the opportunities
router did not join the badge. Both are now done:

* `db/repositories/opportunities.py` — `BADGE_COLUMNS`, `BADGE_JOIN` and the
  correlated `badge_correction_column()` spliced into `_LIST_SELECT`, exactly
  as the schema slice specified. A page of fifty rows costs **no extra query**
  (NFR-502), and the correction stays a correlated scalar so a company with
  both a private and a promoted correction does not double its row.
* `pipeline/employer_product.badge_from_row()` — new. Renders the list badge
  from those joined columns without building a `Verdict`, because `Verdict`
  refuses to exist without evidence and a list projection has none. A company
  with no verdict comes back as *not researched*, never blank: a blank cell
  reads as "employer", which is the assumption the whole axis exists to stop.
* `api/routers/opportunities._present()` — carries `employer` on every row,
  in the **reader's** locale. (First cut read the language off the posting and
  put a Dutch badge on an English screen; fixed.)
* `OpportunitiesPage.jsx` → `EmployerCompanyLine` in the company column.
* `OpportunityDetailPage.jsx` → `EmployerKindBadge` in the header,
  `UndisclosedEmployer` and `PostingOnBehalfNote` above the company card.
* `CompanyDetailPage.jsx` → `EmployerKindPanel` above the tabs.
* `AdminPage.jsx` → an "Employer kind" tab rendering `EmployerCoveragePanel`.

---

## 2. The corpus pass

One pass, whole queue, ordered by vacancy count.

| | measured |
|---|---|
| companies | **1,210** (the whole queue: every company with a name and ≥1 vacancy; 6 more had been resolved in a smoke test, so 1,216 carry a verdict) |
| wall clock | **47.8 minutes** (2,868.8 s), concurrency 8 |
| LLM calls | **266** — `classify.employer_kind`, deepseek-chat, 100% success |
| tokens | 1,185,585 in / 67,038 out; mean 4,457 input tokens per call |
| **cost** | **EUR 0.363** |
| pages fetched | 1,116, of which **294** to the Belgian register at 0.5 rps |
| failures | **0** |

Against the design's estimate of 45–60 minutes and under EUR 3: **inside both**,
and the cost is an order of magnitude below the ceiling because only the 350
companies with a domain ever reach the paid rung.

The brief's minimum was the top 200 by vacancy count; the whole corpus was run
instead. For the record, the top 200 hold 1,476 of 2,703 vacancies (54.6%),
not "most of the corpus" — the tail is long and flat.

### 2.1 Coverage by rung

| rung | outcome | companies |
|---|---|---|
| 0 knowledge base | handed down (nothing on record) | 1,216 |
| 0b free signals | handed down (**never decides, by design**) | 1,216 |
| 1 registry (KBO) | **decided** | 24 |
| 1 registry | handed down | 1,191 |
| 1 registry | unavailable | 1 |
| 1b EURES section-O probe | unavailable (**the rung does not exist**) | 1,200 |
| 2 website + LLM | **decided** | 246 |
| 2 website | handed down (unusable answer) | 96 |
| 2 website | unavailable (no domain to read) | 858 |

### 2.2 What the verdicts say

| | employers | vacancies |
|---|---|---|
| **agency** (or job board) | 56 | 402 |
| **direct employer** | 211 | 624 |
| **cannot tell** | 949 | 1,677 |
| decided, total | **267 (22%)** | **1,026 (38%)** |

By the rung that decided: registry 21, website 246.

### 2.3 Why it could not tell — the whole of it

| reason | employers | vacancies | what would settle it |
|---|---|---|---|
| `no_domain` | **853** | 1,548 | a website for this employer |
| `robots` | 54 | 54 | nothing — the site forbids reading it |
| `off_domain_redirect` | 22 | 36 | the real website |
| `ambiguous_self_description` | 20 | 39 | a person reads the quotes on both sides |

**One number explains the coverage of this feature.** A company that has a
domain gets an answer 73% of the time (254 of 350). A company that does not
gets one 1.5% of the time (13 of 866, all from the Belgian register).

The bottleneck is not the classifier, the model, the prompt or the budget. It
is that **866 of 1,216 employers in this corpus have no website on record.**
Deriving domains — the FR-301 contact ladder already does it, and it is free of
LLM cost — is worth more than every other improvement to this feature
combined.

---

## 3. Accuracy

### 3.1 The six the brief names

| employer | wanted | got | rung | confidence |
|---|---|---|---|---|
| GitLab | employer | **employer** | website | 0.90 |
| NOEL FRANKLIN BV | agency | **agency** (temp_agency) | registry | 0.97 |
| ADECCO PERSONNEL SERVICES NV | agency | **agency** (temp_agency) | registry | 0.97 |
| KONVERT HR NV | agency | **agency** (temp_agency) | registry | 0.97 |
| FORUM JOBS | agency | **agency** (temp_agency) | registry | 0.97 |
| **100G BV** | agency | **cannot_tell(no_domain)** | website | 0.00 |

Five of six. 100G BV is a **miss, not an error**: the register resolved it to
0803.543.941 and found codes 62/63/70 with no 78.x (the register is silent, not
negative), the company has no domain on record, and its own site `100g.be`
renders zero characters of text — the website slice measured that
independently. The free signals put it at `probable` (score 4.5,
`suspected_agency`) and the ladder is not allowed to act on that. See §3.3.

### 3.2 The confusion matrices

**Set A — `tests/fixtures/agency_labels.json`, the labelled set that ships.**
43 employers, each with the source of its label (a register code, a hand label
recorded in the proposal, or the employer's own adverts). 42 matched a corpus
company; `ACCENT JOBS FOR PEOPLE` has no row under that name.

|  | predicted agency | predicted not-agency |
|---|---|---|
| **truth agency** (19) | **15** | 4 |
| **truth direct** (23) | **0** | 23 |

**precision 1.00 · recall 0.79**

Of the 23 direct employers, 7 were positively decided `direct` and 16 are
`cannot_tell` — correct in the sense that matters (none is badged as an
agency), but only 7 are *known* to be direct.

**Set B — the 50 largest employers by vacancy count**, which is the set the
proposal measured. 49 labelled: the 42 above plus 8 added for this run with
their basis named in the evaluation script (FOD BOSA, Celonis, SumUp, Voize,
Valuenet, the Antwerp municipal education authority, think about IT GmbH,
Vulpia — all public bodies or named product companies, none a borderline call).
`Accent Jobs NV` (11 vacancies) is excluded: the fixture labels a differently
named company row.

|  | predicted agency | predicted not-agency |
|---|---|---|
| **truth agency** (18) | **14** | 4 |
| **truth direct** (31) | **0** | 31 |

**precision 1.00 · recall 0.78**

The four false negatives are the same in both sets, and all four are
`cannot_tell(no_domain)`:

| employer | vacancies | free-signal band | why no verdict |
|---|---|---|---|
| HOUSE OF RECRUITMENT SOLUTIONS BV | 27 | probable (3.0) | two `ENT` rows in the register with the same name; the seat did not separate them, and there is no domain |
| 100G BV | 26 | probable (4.5) | register has no 78.x; no domain; `100g.be` renders nothing |
| EDITX BV | 19 | probable (4.5) | register has 62.x only; `editx.eu` renders "loading…" |
| Vermeesch, Kiona | 8 | possible (2.5) | a person-named agency; no register hit, no domain |

### 3.3 Why recall is 0.78 and not 0.95 — the design, not a bug

The proposal measured `probable+ P 1.00 · R 0.95` on **the free-signal
detector's bands**. The shipped ladder does not let those bands decide
anything. `docs/Agency_Research_Design.md` §2 is explicit about it:

> **0b Free signals** (queue order only) … Proves: **Nothing.** Sets priority
> and a `suspected` hint.

Only two rungs ever write a verdict: the Belgian register (24 companies) and
the website classifier (246). Three of the four false negatives were scored
`probable` by the free signals — the band the proposal's own table says should
be "shown as *probably an agency*, same product path as certain" — and the
ladder stored `cannot_tell` instead.

**That is a deliberate, defensible trade and it is the single decision that
separates the proposal's number from this one.** Letting the signals decide at
`probable+` would take recall from 0.78 to roughly 0.94 on both sets. Whether
precision would survive is measurable but *not* measured here: the signals
slice measured `probable+ P 1.00 · R 0.55` on 43 hand-labelled employers with
the name lexicon switched off, and the proposal measured `P 0.97` on its
103-employer set with one false positive (Greenpocket, a startup with five
intern adverts). One false positive at `probable` is one good company hidden
from a job seeker, which is the error this feature exists to prevent — so the
conservative choice is the right default, and changing it is a product
decision, not an integration fix. It is recommendation #2 in §6.

### 3.4 The one direct employer that *was* mislabelled

Every one of the 56 agency verdicts was read by hand. Outside the labelled
sets, exactly one is wrong:

> **WattFox** (3 vacancies) — tagged `agency` / `job_board`, confidence 0.80,
> via the website rung. It is an **energy-comparison portal** that connects
> consumers with installers. Its advert is
> *"Als Werkstudent Kundenservice … bei WattFox trägst du maßgeblich zu
> **unserem** Erfolg bei"* — an unambiguous direct hire for WattFox itself.
> The classifier read "connects providers with consumers" as intermediation of
> *jobs*. **This is the failure mode that hides a good company from a job
> seeker, and it is live in the corpus today.**

Two more were checked and are **correct**, which is worth recording because
both look wrong at first glance:

* **CONNECT CONSULTING BV** (0.80) — its adverts open *"Voor onze klant, …
  zoeken wij een Financial Controller"*. Singular client clause. Correct.
* **Jobgether** (0.90, job_board) — its adverts open *"This position is listed
  on behalf of a partner company"*. Correct.

**The obvious mitigation does not work, and it is worth saying so.** WattFox
sits at 0.80, and raising the website rung's decided-`agency` floor to 0.85
looked like a free fix — until it was measured. There are exactly four
sub-0.85 agency verdicts in the corpus, and **three of them are correct**:

| employer | conf | a quote the classifier stored |
|---|---|---|
| CONNECT CONSULTING BV | 0.80 | *"Wij zorgen voor een project bij de klant…"* |
| GS RECRUITMENT BV | 0.80 | *"…tailored job matching…"*, *"both candidates and clients"* |
| CLBR Consulting BV | 0.80 | *"Rekrutering en HR-advies voor ondernemers…"* |
| **WattFox** | 0.80 | *"Angebotsvergleichsportale … rund um nachhaltige Energie"*, *"Für Anbieter"*, *"Für Verbraucher"* |

A 0.85 floor would drop three true positives to remove one error. What
actually separates WattFox is visible in the table: **its stored quotes
contain no employment vocabulary at all** — no candidate, no vacancy, no
client role, no placement. It is a consumer marketplace, and the classifier
mapped "connects providers with consumers" onto the wrong axis. A rule that a
decided `agency` verdict must quote at least one sentence about *hiring* would
catch it and cost nothing; a confidence floor would not. That is the fix worth
making, and it belongs to the website slice rather than to this integration
run.

One related inconsistency, recorded because two gates read the same host and
disagreed: the website rung read real recruitment copy from
`https://gsrecruitment.com` (content hash `9c7fbdd1…`) while the hardened
identity gate in §4.3 called the same domain a for-sale placeholder. They
cannot both be right, and the evidence — three verbatim recruitment sentences
with a stored content hash — is on the classifier's side.

### 3.5 The structural limit: the tag is per **employer**, the badge is per **posting**

`Consultport` is correctly tagged an agency — it is a marketplace for freelance
consultants. Its advert in this corpus is *"Head of Sales — Consultport is a
global platform…"*: Consultport hiring its own Head of Sales. The badge on
that row says "employer not named" when the employer is Consultport.

The proposal anticipated this exactly, and put the answer in per-posting
columns — `posting_on_behalf`, `employer_named_in_posting`,
`employer_named_sentence`, `employer_descriptors`, `offering_code`. The schema
slice assigned them to "migration 111". **Migration 111 was taken by the
orchestration slice for the ladder trail, and the per-posting columns were
never written by anybody.** `vacancy` has none of them.
`employer_product.apply_posting()` falls back to detecting an on-behalf clause
in the description text, and by design it can only move a posting *up* toward
"agency", never down toward "direct" — so an agency's own internal hires stay
badged. Affected employers in this corpus include Consultport, Zenjob,
Manpower, Unique and USG Professionals whenever they advertise a role for
themselves.

---

## 4. The namesake cleanup

### 4.1 The three the brief names: cleared, and verified cleared

Migration 112 took all three back, and the state was checked directly:

| domain | company | evidence | company.domain now | contacts |
|---|---|---|---|---|
| `adequat.com` | Adéquat Belgium NV | the site is *"Adéquat, services linguistiques inc."*, a Canadian translation bureau | NULL | 0 |
| `brightplus.com` | Bright Plus NV | BrightBio, a Finnish coatings maker; the title *"Home - brightplus.com"* names the company only by repeating the domain | NULL | 0 |
| `think-about-it.com` | think about IT GmbH | redirects off-domain to a Hyundai error page | NULL | 0 |

All three carry a `company_domain_revocation` row with the reason, the
evidence sentence and `gate_version = v2`; none appears in
`derived_confirmed` any more (264 → 261). Bright Plus NV was independently
confirmed an agency by the register afterwards (NACE 78.200), so removing its
namesake domain cost no coverage.

### 4.2 The whole derived-domain set, re-read through the hardened gate

All **261** remaining `derived_confirmed` domains were re-read (dry run,
41 seconds — the pages were still in the 24-hour egress cache from the registry
slice's own run, so this is the same evidence, not a second set of requests):

| | all 261 | **one-word names (170)** | multi-word (91) |
|---|---|---|---|
| confirmed | 196 (75%) | **124 (73%)** | 72 (79%) |
| flagged for review | 51 | 38 | 13 |
| would be cleared | 14 | 8 | 6 |

**The brief asks for "the 127 one-word-name derived domains". The measured
number is 170 of 261** (stripping the legal form). The design note says 180 of
264; the difference is the three revocations plus a slightly different
stripping rule. 127 does not correspond to anything measurable in the corpus
today, so the measured figure is reported instead.

### 4.3 Nothing was cleared, and here is why

The registry slice measured "zero correct domains cleared" on a 60-domain
sample. **Over all 261 that does not hold.** Of the 14 the gate would clear,
at least three are demonstrably correct, verified by fetching the pages:

| domain | gate says | the page actually says |
|---|---|---|
| `docuware.de` | "the page does not name the company: 'docuware'" | `<title>Dokumenten-Management und Workflow-Automation mit DocuWare</title>` — it **does** name it. Rule 2 (a one-word name must *equal* the title, not appear in it) breaks on the German convention of putting the brand at the end of a descriptive phrase |
| `trevi.be` | "the page is a placeholder ('a vendre')" | *"TREVI \| Réseau d'agences immobilières en Belgique"* — an **estate agency**, whose site is necessarily full of *"à vendre"* |
| `synergie.com` | "the page does not name the company: 'synergie'" | the French Synergie group's own HR site; the brand is not in the title, which is descriptive French |

Three of the four checked non-parked rejections were wrong; the ones that are
right (`wesearch.eu` → *"Aftermarket.eu :: domain wesearch.eu"*,
`sanity.co.uk` → a bare-domain title) are parked-domain pages. **The gate's
precision on the reject decision is roughly 0.79, not 1.00**, and running
`reverify_derived_domains(clear=True)` today would take three good websites
away from three real companies. It was therefore **not run.** The dry run is
recorded and the 14 belong in the review queue, not in a revocation.

---

## 5. The NACE Rev 2.1 correction

Confirmed in `pipeline/discovery.py`, measured by running
`eures_partitions()` and by re-running it with the section list monkeypatched
to the pre-fix state:

* `NACE_SECTIONS` = A…S, 19 letters. **N is in the sweep. O is in the sweep.**
* `EXCLUDED_NACE_SECTIONS` = {T, U, V} only — households as employers of
  domestic staff, extraterritorial bodies, and the 22nd letter the Rev 2.1
  facet carries that the taxonomy does not name.
* `SECTION_PAGE_WEIGHT` = {"O": 0.5} — a yield lever, not an agency control.

| plan | partitions | requests | minutes at Crawl-delay 10 |
|---|---|---|---|
| BE+NL, N excluded (before) | 126 | 504 | 84.0 |
| BE+NL, N restored, O flat | 133 | 532 | 88.7 |
| **BE+NL, as shipped (O at 0.5)** | **133** | **518** | **86.3** |
| BE only, N excluded (before) | 54 | 594 | 99.0 |
| **BE only, as shipped** | **57** | **555** | **92.5** |

**BE+NL costs 14 more requests (+2.8%, +2.3 minutes) and sweeps one more
section.** BE alone costs **39 requests fewer** than before while sweeping one
more section, because halving O's pages more than pays for restoring N. The
600-request budget is unchanged and still under-spent (518 of 600).

This reproduces the orchestration slice's figures exactly. The reasoning is
worth restating because it is the thing most likely to be re-litigated:
excluding a NACE section is not a way of not collecting agencies. NOEL
FRANKLIN's 56 rows arrive under C, F, N **and** O, 55 of them under C alone.
Agency membership is a per-employer tag; a partition exclusion is only a way of
not seeing an employer.

---

## 6. The interface, walked

Headless Chromium, signed in through the application's own form as the
verification persona **thibault.casteleyn@example.com**. Screenshots in
`tests/e2e/out/screenshots/employer_kind/`.

| file | what it shows |
|---|---|
| `01_ranked_list_badge.png` | 18 employer-kind badges on the first page of the ranked list; the four NOEL FRANKLIN rows read **"Agency: NOEL FRANKLIN BV · Via agency · employer not named"** with "Employer: not named" beneath. Direct employers carry no badge — the normal case does not shout |
| `02_evidence_popover.png` | the popover the badge opens: the rung in words, confidence 97%, **four verbatim register quotes**, the NACE code, the source link, the date |
| `03_correction_modal.png` | "This is wrong: NOEL FRANKLIN BV" — the two choices, the required "How do you know?", the optional evidence URL, and the paragraph stating that it applies to this seeker immediately and to nobody else until promoted |
| `04_opportunity_undisclosed_employer.png` | the opportunity screen with the badge beside the kind badge and the undisclosed-employer block |
| `05_company_employer_panel.png` | the company screen: "Who is hiring", the verdict, *how* it was established, and every quote under "Why we say this" |
| `06_admin_coverage_panel.png` | the administration coverage panel: employers answered, nobody-looked-yet, share of postings accounted for, flagged for review, the answer table, which rung answered, and where there is no answer and why |
| `07_scoring_refuses_the_company_dimensions.png` | **the point of the whole feature.** After re-scoring one NOEL FRANKLIN row, the *Company* sub-score is **gone** from the sub-score list, and the rationale reads: *"The employer is not disclosed: this vacancy was posted by NOEL FRANKLIN BV on behalf of an employer it does not name, so the company dimensions were not assessed."* Directive fit dropped 58 → 45 with *"posted by a staffing agency; you prefer direct employers"* |

Two things about the walk, stated plainly:

* The administration screen is administrator-only and the verification persona
  is an ordinary job seeker. Rather than promote the account under test, the
  admin tab was walked as **the same person's `+e2e04` administrator alias**,
  which `tests/e2e/test_04_followup.py` creates with the harness's own
  documented password. The persona's own privileges were not changed.
* The correction lifecycle was exercised through the API on GitLab — a
  deliberately wrong answer recorded, checked to be `scope: private` with
  `applies_to: you`, checked not to have touched the shared verdict row, then
  withdrawn. GitLab reads `employer / direct` again and
  `employer_kind_correction` is empty.

**Console errors during the walk: two 401s** on `/api/logs/client` and
`/api/overview/journey`, both fired in the moment between signing out and
signing in again. No error came from any employer-kind surface.

---

## 7. What does not work

Ordered by how much it costs a job seeker.

1. **WattFox is badged as an agency and is not one** (§3.4). One direct
   employer, three vacancies, hidden behind "employer not named" today. The
   obvious mitigation — a confidence floor — was measured and rejected: it
   would drop three correct agency verdicts to remove this one. The fix that
   fits the evidence is to require a decided `agency` verdict to quote at
   least one sentence about hiring; WattFox's four quotes are about energy
   offers, providers and consumers, and contain no employment vocabulary.
2. **78% of employers have no answer**, 853 of them because there is no
   website on record (§2.3). The corpus is 38% covered by vacancy volume. This
   is not a classifier problem and no amount of model spend fixes it.
3. **The free signals are not allowed to decide** (§3.3), which is the whole
   of the recall gap against the proposal. Three of four false negatives sat at
   `probable`. This is a documented design choice; revisiting it is a product
   decision with a measurable precision cost.
4. **An agency's own internal vacancies are badged "employer not named"**
   (§3.5), because the per-posting columns the proposal specified were never
   migrated — migration 111 went to the ladder trail instead. `vacancy` has no
   `posting_on_behalf`, `employer_named_in_posting`, `employer_descriptors` or
   `offering_code`.
5. **The gates are not wired.** `kind_repo.companies_by_role()` exists and is
   called by exactly one place: the API's own listing endpoint. The company
   profiling, financial-analysis and speculative-opening workers do **not**
   consult the employer role. No damage has materialised — of 56 agencies only
   FORUM JOBS carries a `business_summary` (built before the tag existed) and
   there are zero speculative openings at agencies — but that is because those
   passes have not been re-run, not because anything stops them.
6. **Existing opportunity rows keep a company sub-score computed against the
   agency** until they are re-scored. The refusal is correct on re-score
   (screenshot 07) but there was no backfill; the proposal's N13 re-score is
   not done.
7. **The apply e-mail still frames the agency as the employer** —
   *"I am applying for the Manufacturing Engineer role at NOEL FRANKLIN BV"*.
   `documents/intro_email.py`, `llm/prompts/apply_email.md` and
   `consistency.check_intermediary_material` are untouched. The motivation
   document *is* correct; the covering e-mail is not. The employer badge was
   deliberately **not** wired into the Apply Browser for this reason: a badge
   next to a letter that contradicts it is worse than neither.
8. **The domain re-verification cannot be run with `clear=True`** (§4.3). Its
   reject precision is about 0.79 and it would take `docuware.de`, `trevi.be`
   and `synergie.com` away from three real companies. Rule 2 needs a
   containment fallback for a one-word brand inside a descriptive title, and
   the placeholder rule needs to stop reading an estate agent's *"à vendre"*
   as a parked domain.
9. **The EURES section-O probe (rung 1b) does not exist.** The ladder records
   it as `unavailable` for 1,200 companies, which is the truthful thing to say
   about a rung nobody walked. `PROVIDER_PATHS` expects
   `dreamjob.pipeline.employer_eures_rung:eures_sector_rung`.
10. **GB (Companies House) and NL (KvK) registry paths are code-complete and
    untested** — no API keys in this environment. `registry_rung` returns
    `unavailable` with that reason rather than pretending. FR (Sirene) is not
    implemented. DE has no free activity register, and Germany is 442 of the
    corpus's names.
11. **Two writers still share `company_employer_kind`.** The ladder writes
    through `employer_resolution.write_verdict` (a column-introspecting upsert)
    and `employer_registry_rung.run_registry_pass` writes through
    `employer_kind.record_verdict`. Both produce consistent rows and migration
    110's triggers repair the role either way, so nothing is broken today —
    but two writers of one table should not survive the next merge.
12. **`register_correction` promotion by operator has no screen.** The
    two-seeker consensus path and the read-only review queue exist; the
    operator "confirm" control does not.
13. **48 verdicts are flagged for review** — mostly discarded quotes the
    verifier could not find on any page read, which is NFR-402 working
    correctly, but nobody has looked at them.

---

## 8. Reproducing this

```bash
# schema
PYTHONPATH=backend python3 -m dreamjob.db.migrator

# tests and lint
PYTHONPATH=backend python3 -m pytest tests/unit -q -m "not llm"   # 1262 passed
python3 -m ruff check backend/dreamjob tests                       # clean
cd frontend && npm run build                                       # clean

# the corpus pass (48 min, EUR 0.36)
PYTHONPATH=backend python3 -c "
from dreamjob.pipeline.employer_resolver import run_resolve_many
print(run_resolve_many(2000, concurrency=8))"

# coverage, as the panel reads it
curl -b cookies.txt http://127.0.0.1:8000/api/employers/coverage
```

The evaluation script, the walk script and the re-verification dry run used for
this report are working files, not part of the tree. The measurements they
produced are all reproducible from the database and the endpoints above.
