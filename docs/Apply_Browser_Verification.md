# Apply Browser: verification against real data

Date: 2026-09-09
Scope: integrating the four slices that built the Apply Browser, loading a real
job seeker into it, and measuring what it actually produces — so that every
number on the screen can be checked rather than taken on trust.

---

## 1. Sign in

| | |
|---|---|
| Application | **http://127.0.0.1:5173** |
| E-mail | **thibault.casteleyn@example.com** |
| Password | **Dreamjob-Verify-2026!** |
| Screen | sidebar → **APPLY → Apply browser**, or go straight to `/apply` |

Thibault Casteleyn is the fictional persona in `tests/e2e/persona/out/persona.json`
— an IT developer in Ghent. The address is in the RFC 2606 reserved domain, so
nothing addressed to him can reach a real inbox. The account was created through
the API exactly as the sign-up screens would: registered, CR-410 consent granted,
LinkedIn export and CV uploaded, both planted conflicts resolved, composite
profile built, dream-job model built and confirmed.

Job seeker id `7a3e21ee83b642da95acae96e4fb9663`, campaign *Apply Browser
verification* (`5b2e8ed8e6db4ace8b2a00edb79c9bc7`).

**Nothing was sent.** `DREAMJOB_MAIL_DRY_RUN` is on and enforced in the mail
backend registry, not in the interface. Section 6 is the proof.

## The numbers, in one place

| | |
|---|---:|
| Vacancies in the corpus | 2,740 at 1,215 companies |
| **Vacancies with a usable apply contact** | **1,047** (target was 500; it stood at 751 when this session started) |
| Companies resolved | 301 reachable, 417 unreachable-with-a-reason |
| Opportunities in this seeker's Apply Browser | **70**, at 41 companies, every one with a contact |
| **Application packages fully generated** | **30** — e-mail + CV DOCX + CV PDF + briefing PDF + motivation PDF + FR-322 check |
| FR-322 verdicts on those | 17 pass, 13 fail |
| **Token cost of the 30 packages** | **EUR 1.1634** — 1,159,095 input + 873,658 output tokens |
| **Per package** | **EUR 0.0388**, ~67,800 tokens |
| Messages assembled in dry run | 5 |
| Messages **sent** | **0** |

---

## 2. Integration

The four slices were individually green. Putting them together needed one wiring
change and turned up three defects — all of them in a seam between slices, or on
a path that only a real document exercises.

**Baseline, before I changed anything:** `pytest tests/unit` → 1001 passed;
`ruff check backend/dreamjob` → clean; `npm run build` → built, `ApplyBrowserPage`
code-split at 47 kB; migrations already at `100_apply_browser`, re-running the
migrator reports "nothing (already up to date)".

**After the changes below:** `pytest tests/unit` → **1003 passed**, `ruff` clean,
frontend build clean, API restarted and healthy.

### 2.1 The screen was generating packages the long way (wiring)

`POST /api/apply/packages/generate` ran its own serial loop over
`documents/package.generate`. `pipeline/apply_packages.py` — written for exactly
this screen, in the same set of slices — was called by nothing.

The route now delegates to `apply_packages.generate_many` and hands back *that*
job rather than wrapping it in a second one. What the screen gains: four packages
in flight instead of one, a complete package reused instead of paid for twice,
the per-package cost read back from the FR-364 call log, the NFR-104 degradation
ladder, and the FR-322 check run over the CV **and** the e-mail. A run that asks
for only some of the four artefacts still takes the old path. `GenerateIn` gained
one field, `regenerate` (default `false`), which is what makes re-running a
30-package batch cheap.

*Measured:* the serial loop finished 2 packages in its first six minutes; the
delegated batch did 30 in about 30 minutes at concurrency 4.

### 2.2 The CV printed a Python object in its contact line (defect, fixed)

Every generated CV carried this under the name:

```
… · www.linkedin.com/in/thibaultcasteleyn · {'url':
'www.linkedin.com/in/thibaultcasteleyn', 'label': 'LinkedIn'} · {'url':
'thibaultcasteleyn.example.com', 'label': 'Personal'}
```

`contact.websites` is written as `{"url": …, "label": …}` by every producer —
`linkedin_pdf.py`, `cv_parser.py` and the `profile_intake` merge all read
`w["url"]` — but `documents/cv_generator._contact` did `str(w)` on the entry and
put the dict repr in the header of **the document that gets attached and sent**.
The unit suite never saw it because its fixture profile has `"websites": []`.

Fixed in `cv_generator._website_urls()`, which takes the address out of a mapping
and still accepts a bare string. While there: `CvContact.lines()` now prints each
address once — the LinkedIn profile was appearing twice, because `linkedin_pdf`
copies it out of `websites` into `linkedin_url` and both were rendered.

Tests: `test_the_contact_line_prints_the_address_not_the_object` (fails without
the fix). The 30 CVs generated before the fix were re-rendered from their stored
documents — same facts, same template, **no model call** — through
`documents/package.regenerate_cv_only`. All 30 PDFs were then re-read and checked:
no object reprs, no repeated address.

### 2.3 One bad model answer failed a whole package (defect, fixed)

One package in the batch died with
`AttributeError: 'list' object has no attribute 'get'`. `generate_cv` deliberately
degrades to an untailored CV on `LLMError`, `ValueError`, `KeyError` and
`TypeError` (NFR-104) — but a model that answers with a bare JSON **list**
instead of an object reached `apply_tailoring(...)` and raised `AttributeError`,
which is not in that tuple, so the failure escaped and cost the whole package.

`apply_tailoring` now refuses a non-mapping response with a `ValueError`, which
the existing handler already degrades on: the job seeker gets the untailored CV
and a note, not a lost package. Test:
`test_a_model_that_answers_with_a_list_still_produces_a_cv`. The package that had
failed was regenerated afterwards and completed with 0 errors.

### 2.4 What the model actually did during the run

DeepSeek's reasoning model was cut off at its token budget repeatedly and the
chat-model fallback in `documents/_llm.py` caught every one — 49 `generate.cv`
calls for 31 CVs, 41 `generate.motivation` calls for 30 documents. Nothing
surfaced as an error to the screen. It is the main reason a package costs
~68,000 tokens rather than ~40,000.

---

## 3. The persona's account

| step | what happened |
|---|---|
| Register | `POST /api/auth/register` → 201 |
| CR-410 consent | `llm_transfer` granted **before** the first upload; the extractor refuses the model without it |
| LinkedIn export | `persona_linkedin.pdf` → 201 in 1.0 s; photograph extracted (FR-106) |
| CV | `persona_cv.docx` → 201 in 1.3 s |
| FR-103 conflicts | **both planted conflicts found**: `experience.1.company` (LinkedIn *Cyrel* vs CV *Cyrel NV*) and `experience.4.end` (2017-08 vs 2017-05). Both resolved to the CV and applied; 0 left unresolved |
| Dream job | the persona's own statement saved (FR-109) |
| Composite profile | built in **185 s** on `deepseek-reasoner` (FR-121, FR-125) |
| Dream-job model | built in **92 s**, then **confirmed** (FR-109, FR-128) — it proposed *Senior Backend Engineer (Event-Driven Systems)* and *Senior Streaming/Data Infrastructure Engineer* |
| Directives | one set from the persona's own statement: Go / Kubernetes / Kafka, senior+, BE NL DE LU FR GB IE |
| Campaign | *Apply Browser verification*, bound to the profile version, the composite and the confirmed model |

**Where I am** now reads: profile ✓, composite ✓, dream job ✓, directives ✓,
companies ✓, opportunities ✓, scoring ✓, contacts ✓, documents ✓, dispatch
**blocked** — exactly right for a machine in dry run.

### The 70 opportunities, and how they were made

The Apply Browser lists **70 real vacancies at 41 companies** — GitLab, SumUp,
Qonto, Solactive, hellofresh, celonis, EverQuote, Implico, Doc Cirrus, Smals,
Noel Franklin and others, collected from greenhouse, EURES and arbeitnow. Every
one has a contact.

**Be aware of how they got there.** `pipeline/opportunities.synthesise_campaign`
scopes its pool to what *that campaign's own plan items* collected, so a new
campaign synthesises nothing until it has been out on the network — a collection
run over ~4,700 plan items. Rather than re-fetch a corpus the installation
already holds, I chose the pool myself — vacancies at companies that have a
usable apply contact, engineering titles, at most four per company so GitLab did
not swamp it — and ran the product's own
`normalise_vacancy` → `rejection_reason` → `upsert_synthesised` over it, then
scored through `POST /api/opportunities/recalculate`.

The product's own filter did its job on that pool: **12 of 82 were rejected as
`outside_location_directives`** (GitLab's US and India postings), leaving 70.
Scoring was deterministic (`use_llm=false`) to keep the model budget for the
packages, so all 70 carry arithmetic scores and none carries an LLM dream-fit
rationale.

---

## 4. Contacts at scale (FR-301, FR-303, FR-304, FR-305)

**1,047 of the 2,703 vacancies with a company now have somebody to write to.**
The target was 500; the figure stood at 751 when this session started.

`ensure_apply_contacts` was run as one resumable pass over the companies that had
never been walked (`limit=1400`, `max_companies=450`, `concurrency=8`,
`allow_smtp=False`, `crawl_site=True`). It was stopped once coverage was well
past the target, because it was starving the API of the database; it is resumable
and section 8.6 says what is left. `POST /api/apply/contacts/discover` was then
run from the browser's own route and completed cleanly (it found nothing to do,
which is correct: the corpus target was already met).

Nothing was invented. A company that produces nothing gets an
`apply_contact_resolution` row with `status='unreachable'` and the reason in
words, and the browser has an **Unreachable** filter so the screen can say
"searched, nothing found" rather than offer a button.

### Contacts by FR-303 method

| method | vacancies | companies | FR-304 verdicts |
|---|---:|---:|---|
| conventional mailbox — the FR-301 tier-3 careers address, inferred | 571 | 140 | risky 571 |
| website — printed on the company's own contact or careers page | 429 | 174 | risky 402, valid 27 |
| pattern inference — a named person, from the domain's own convention | 26 | 26 | risky 26 |
| press — an address found in the company's own press material | 14 | 9 | risky 14 |
| vacancy — the application address the employer stated in the posting | 7 | 4 | risky 7 |
| **total** | **1047** | | |

Of 2703 vacancies in the corpus.

### Where the domain came from

| domain source | companies | vacancies |
|---|---:|---:|
| derived_confirmed | 264 | 852 |
| company | 28 | 90 |
| vacancy_text | 9 | 45 |

### Unreachable — a finding, not a gap

| why | companies | vacancies |
|---|---:|---:|
| no candidate domain could be confirmed (MX, or the home page did not name the company) | 360 | 768 |
| the name carries no word a domain could be spelled from | 35 | 80 |
| the name is one short word — a domain would be a guess | 20 | 57 |
| addresses were found and none survived FR-304 | 2 | 7 |

### FR-305 domain probe cache

| verdict | domains |
|---|---:|
| no_mx | 1060 |
| unreachable | 306 |
| confirmed | 289 |
| rejected | 91 |
| **probed more than once** | **0** |

1,746 domains were judged and **not one was probed twice** (`probe_count > 1` is
zero). 1,060 had no mail exchanger and were therefore never fetched over HTTP at
all. All HTTP goes through `egress/` with robots.txt honoured — a disallowed or
unreadable `robots.txt` is a skip, never a workaround, and the run log is full of
those refusals.

**The confirmation gate is the whole thing.** A domain spelled from a company
name is only used once it has an MX record, its home page answers, and that page
*names the company* — links and addresses are stripped before matching, so a site
cannot confirm itself by quoting its own URL. 264 of the 301 reachable companies
were reached through a derived-and-confirmed domain; without the gate, an
inferred address would reach a real person at an unrelated company.

---

## 5. Application packages (FR-321, FR-322, NFR-104)

**30 packages, fully generated** — for 30 different opportunities across 30
companies, through `POST /api/apply/packages/generate`, the same route the
**Generate what is missing on this page** button calls.

Each one is six things: the e-mail, the tailored CV as **DOCX** and **PDF**, the
FR-329 company briefing PDF, the FR-330 motivation and fit PDF, and the FR-322
factual-consistency check. All 30 have all six; all 30 sets of files are on disk
under `data/generated/<seeker id>/<package id>/`.

The 30 are one opportunity per company, taken from the top of the browser's own
list, so the sample is 30 different employers rather than 30 postings at GitLab.
The batch ran at concurrency 4 and finished 30/30 with one error — the crash of
section 2.3 — which was regenerated afterwards, with the fix in place, and
completed cleanly. The other 40 opportunities in the selection are deliberately
left ungenerated, so the **Generate what is missing on this page** button has
something to do when you press it.

### Token cost, this account only

| task | calls | input tokens | output tokens | cost (EUR) |
|---|---:|---:|---:|---:|
| generate.motivation | 41 | 413,170 | 197,941 | 0.3012 |
| generate.cv | 49 | 155,299 | 236,882 | 0.2757 |
| cv.consistency | 36 | 226,633 | 156,907 | 0.2136 |
| generate.briefing | 31 | 165,717 | 146,900 | 0.1883 |
| generate.email | 42 | 198,276 | 135,028 | 0.1846 |
| profile.composite | 1 | 4,950 | 25,725 | 0.027 |
| profile.dreamjob | 1 | 2,212 | 12,657 | 0.0132 |
| normalise.skill | 2 | 1,602 | 112 | 0.0005 |
| **total** | **203** | **1,167,859** | **912,152** | **1.2041** |

Package generation alone: 199 calls, 1,159,095 input + 873,658 output tokens, EUR 1.1634, over 30 complete packages = EUR 0.0388 and 67,758 tokens per package.

### Packages

- status `approved`: 6
- status `draft`: 24
- FR-322 `fail`: 13
- FR-322 `pass`: 17
- all four artefacts + e-mail on disk: 30

Read off `llm_call`, the FR-364 log, filtered to this job seeker. `profile.composite`,
`profile.dreamjob` and `normalise.skill` are the onboarding of section 3, not the
packages; the per-package figures exclude them.

**The e-mail is the product owner's brief taken literally** — "a very brief
motivation letter that refers to the CV". Three short paragraphs, one point about
*them*, one about fit, then it points at the attachment, from the versioned
prompt `llm/prompts/apply_email.md` (id `apply_email`, v1.0.0). One as generated:

> Solactive's Analyze & Report team builds pipelines that turn financial data into
> client-facing documents, and the role's roughly 80/20 backend-to-frontend split
> matches how I work: deep in event-driven systems, yet able to ship the supporting
> internal frontends. …
>
> The attached CV shows the specifics — most relevant is my Kafka ingestion pipeline
> handling 1.2M messages per minute …
>
> My CV is attached.

---

## 6. Nothing was sent

Asserted directly against the installed database and the files on disk:

| check | result |
|---|---|
| `DREAMJOB_MAIL_DRY_RUN` | **true** |
| Backends wrapped by the transport guard | `gmail_oauth`, `resend` — `send()` refuses before the provider is touched |
| `dispatch` rows for this seeker with `delivery_status='sent'` | **0** |
| `dispatch` rows for this seeker with a `sent_at` | **0** |
| `dispatch` rows for this seeker | 5, all `delivery_status='dry_run'` |
| Audit events | `application.dry_run` recorded per rehearsal (NFR-702) |
| `dispatch` rows anywhere in the database with `sent_at` after this session began | **0** |

The database does hold 240 rows marked `sent`, across 60 other job seekers — the
e2e fixtures from previous days. The newest is dated **2026-09-06**; not one was
written during this session.

Five messages were assembled in full and written to
`data/generated/dry_run/7a3e21ee83b642da95acae96e4fb9663/`. Every one was parsed
back and checked part by part:

| assembled for | recipient | parts |
|---|---|---|
| EverQuote | jobs@everquote.com | `text/plain` 1,067 B + `CV-Thibault-Casteleyn.pdf` 16,880 B |
| hellofresh | jobs@hellofresh.de | `text/plain` 1,129 B + `CV-Thibault-Casteleyn.pdf` 16,647 B |
| Noel Franklin | jobs@noelfranklin.be | `text/plain` 1,186 B + `CV-Thibault-Casteleyn.pdf` 17,289 B |
| Implico | contact@implico.com | `text/plain` 1,081 B + `CV-Thibault-Casteleyn.pdf` 17,333 B |
| Doc Cirrus | presse@doc-cirrus.com | `text/plain` 726 B + `CV-Thibault-Casteleyn.pdf` 17,349 B |

**Exactly two parts each: the letter and the CV.** No briefing, no motivation
document — FR-321 holds on the wire format, not only in the interface. The screen
says so too: *"The briefing and the motivation document are not in this list, and
cannot be."*

Each rehearsal also reported why it would not have gone out even if the guard were
off: *"It is Wed 05:52 for the recipient (UTC); the send window is 08:30–17:30 on
working days"* — the FR-325 window, in the recipient's time zone.

---

## 7. What to click to see each artefact

Sign in, then **APPLY → Apply browser**.

**The page header.** A green banner states the send guard *before* either send
control is pressed: what it is (`DREAMJOB_MAIL_DRY_RUN=true`), where it is
enforced (`dreamjob.mail.dry_run`, server-side, not the interface), which
backends are wrapped, the daily cap, the pacing rule, the FR-325 send window in
the recipient's time zone, and the directory the assembled messages are written
to. Beside it: **Generate what is missing on this page**, **Find contacts for
this page**, and **Send all with attachment** (which opens a confirmation table
naming every recipient before it will run).

**The left column — "The selection".** Every job you selected, with company,
contact and how far its package has got. The chips above the list are true
server-side filters with honest counts: *Contact* (has a contact / none yet /
unreachable / not looked), *Package* (generated / not generated / approved /
sent), *Checks* (passed / failed), *Kind* (advertised / speculative). The line
under them says how many jobs match and how many are shown.

**The right column — one job, five tabs.**

| tab | what it shows | controls |
|---|---|---|
| **Email** | the message that would go out: recipient, how that address was found and its FR-304 verdict, subject, body — editable | Save, Regenerate with an instruction |
| **CV** | the tailored CV, previewed as the PDF itself, marked *"This is the file that gets attached"* | Download PDF, Download DOCX, switch template (Classic / Modern — no model call), change language |
| **Briefing** | the FR-329 company briefing, marked *"For you, never sent"* | Download PDF, Regenerate |
| **Motivation** | the FR-330 motivation and fit document, marked *"For you, never sent"* | Download PDF, Regenerate |
| **Checks** | the FR-322 factual-consistency verdict, the NFR-206 leak scan, whether a model read it too, and every finding with the claim quoted | Re-run the checks |

**The two send controls.** In the job header: **Approve for dispatch** (FR-324)
and **Send email with CV attached**. Neither send button is ever disabled: press
it and it runs the whole path — approval rule, consistency gate, guard rails in
the recipient's time zone, the message composed with the CV attached — and then
reports where it stopped instead of sending. In dry run the report reads
*"Assembled, and nothing was sent"*, names the recipient, lists the attachment,
and gives the path of the `.eml` on disk.

**The assembled messages.** `data/generated/dry_run/<seeker id>/<package
id>-<stamp>.eml`. Open one in any mail client: it is byte-for-byte what would
have gone out.

### The headless walk-through

The screen was driven end to end by Playwright, signed in as the persona, with a
screenshot at every step in `tests/e2e/out/screenshots/apply/`:

| file | step |
|---|---|
| `01-sign-in.png` | the sign-in card, filled |
| `02-apply-list.png` | `/apply` with the guard banner and the full list |
| `03-job-detail.png` | one job open, master-detail |
| `04-tab-email.png` … `08-tab-checks.png` | each of the five tabs |
| `09-send-report.png` | after pressing **Send email with CV attached** |

What it asserted, and what came back:

- the screen is headed **Apply browser**;
- the list states **"70 jobs match · showing 1–50"**, and the facet chips carry
  real counts — *Has a contact* **70**, *Generated* **30**, *Passed* **17**,
  *Failed* **13**, *Approved* **6**, *Sent* **0**;
- 50 rows render on the first page;
- the banner says **"Nothing will be sent. This machine is in dry run"** and names
  **`DREAMJOB_MAIL_DRY_RUN`**;
- both send controls are present — **Send email with CV attached** and
  **Send all with attachment**;
- all five tabs render real content, each proved by something only that tab shows:
  the e-mail body (888 characters, read out of the textarea), *"This is the file
  that gets attached"* on the CV tab, *"For you, never sent"* on Briefing and
  Motivation, and *"Factual consistency" / "Leak scan" / "claims read"* on Checks;
- pressing Send produced **"Assembled in full. Nothing was sent."**

The only console errors were the two 401s the SPA makes on boot when it probes
`/api/auth/me` for a session — the expected pair, not a defect.

---

## 8. What does not work, honestly

### 8.1 The FR-322 judge blocks 13 of 30 packages, and the Apply Browser has no way past it

**This is the first thing you will notice.** **13 of the 30 packages failed the
check** (17 passed). Open one, look at *Checks*: one or two high-severity
findings, and **Approve for dispatch** stays disabled on that row.

All 18 high-severity findings across those 13 packages come from the LLM judge,
all of them in the CV, and **every one of them lands on the tailoring language** —
the headline and the two or three bridging sentences the CV prompt is asked to
write for the target role. Three shapes, six of each:

| shape | example, with the tokens the check could not find in the profile |
|---|---|
| names the employer it is addressed to | *"aligning with **SumUp's** need for ownership of critical backend services"* — `SumUp's` |
| a Title-Case tailored headline | *"Senior Software Engineer \| **Event-Driven Systems, Kubernetes, Data & AI**"* — `Event-Driven Systems Kubernetes Data & AI` |
| a bridging sentence quoting the posting's own terms | *"Senior Software Engineer with **Enterprise Architecture Expertise**"* — `Enterprise Architecture Expertise` |

The mechanism is in `backend/dreamjob/documents/consistency.py`. A judge finding
is escalated to **high** — the severity that blocks — when
`unsupported_tokens(quote, facts)` finds a **capitalised phrase** the profile
does not contain *verbatim*. `ProfileFacts` is built by `profile_facts()` from
the profile version, the composite and the seeker, and **never** from the
opportunity. Two consequences follow mechanically:

1. the **addressee's own name** is by construction absent from the profile, so
   any tailored sentence that names the company it is written for is treated as
   an invented fact;
2. a CV **headline is Title Case by convention**, so a tailored headline almost
   always contains a capitalised phrase that does not appear word-for-word in
   the profile — even when every idea in it does.

This is not the judge hallucinating: the mechanical re-check confirms the tokens
are not in the profile. It is a collision between two requirements — FR-321 wants
the CV tailored to the company, FR-322 wants every claim traceable to the
profile — and nothing tells the check that the addressee and the posting are
*context* rather than claims.

**The gate is doing real work as well, and that is why it should not simply be
loosened.** Two of the 18 are genuine overclaims: a headline reading *"Senior
Full-Stack Engineer with TypeScript, React, and Cloud-Native Backend Expertise"*
for a persona whose dream-job statement says *"I won't take a role where I'm
expected to write React"*, and one that casts him as a *Product Owner*, which he
has never been.

**Why I did not change it.** The consistency gate decides what may be sent
(CR-405, RK-03); making it more permissive is a product decision, not an
integration fix, and `documents/consistency.py` belongs to another slice. The
narrow change would be to let `ProfileFacts` carry the opportunity's own company
name and role title as known context — so the escalation stops firing on the
addressee while still firing on a *different* company's name, and on React.

**Meanwhile:** approval with a recorded override reason exists, but on the
**Applications** screen, not here. `POST /api/applications/{id}/approve` refuses
a failed package with 409 and names the blockers; the same route accepts an
`override_reason`. The Apply Browser shows the blockers and leaves the button
disabled. The six packages approved for this verification are all ones that
passed the check on their own; nothing was overridden.

### 8.2 The NFR-302 objection sentence goes out twice

Every assembled message carries the "reply and I will not write again" sentence
**twice**, in two different wordings, one under the other:

```
--
If you would prefer not to be contacted about this, reply with "no" and I will
delete your details and not write again.

--
If you would rather not receive messages like this, reply with “no thank you”
to thibault.casteleyn@example.com and I will remove your details and not
contact you again.
```

`documents/intro_email.py` writes its own catalogue (`OBJECTION_SENTENCE`) into
`email_body` at generation, and `mail/composer.build_body` appends its own
catalogue's version at compose time. `build_body` already de-duplicates the
*signature* (`if sig and sig not in body`) but not this. The two wordings differ,
so a literal check would not have caught it.

NFR-302 is over-satisfied rather than broken, and
`apply_packages.render_email()` already reports it under `warnings`. I left it
alone because the fix is a layering decision — which module owns the sentence —
and `mail/composer.py` and `documents/intro_email.py` belong to other slices.
There is a second-order consequence worth knowing: `mail/inbox.py` strips *only*
the composer's wording from an incoming reply before reading it for an
objection, so the intro-email wording quoted back in a reply is not stripped.

### 8.3 The corpus is bigger than the brief said, and the contact ladder had nothing to stand on

The brief quoted 1,607 vacancies and 762 companies. The installed database holds
**2,740 vacancies at 1,215 companies**. More importantly, the FR-301 ladder's
steps 3-5 had nothing to read: 2,687 vacancies carry an `application_target`
and **not one of them contains an `@`** — every one is a greenhouse / EURES /
arbeitnow permalink — and almost no company had a `domain`. So nearly every
contact had to start from a domain *derived from the company name and then
confirmed* — MX, the home page must answer, and that page must name the company.
264 of the 301 reachable companies were reached that way.

### 8.4 Most contacts are a conventional mailbox, and none is FR-304 `valid`

571 of the 1,047 covered vacancies (140 companies) rest on a **conventional**
careers mailbox — the FR-301 tier-3 last resort, on a confirmed domain, but
published by nobody. It is stored honestly (`email_source_method =
'pattern_inference'`, `is_generic_mailbox = 1`, confidence 0.4, FR-304 verdict
`risky`) and reported in its own bucket, but read the 1,047 alongside the
"published or stated" figure, which is smaller: 450 vacancies at 187 companies
carry an address somebody actually wrote down (website, vacancy, press).

No verdict this pass wrote is FR-304 `valid`: the bulk path runs
`allow_smtp=False`, because several thousand SMTP probes against other people's
servers for one screen is neither FR-305 nor CR-402. The 27 `valid` rows are
pre-existing e2e fixtures — every one of them belongs to a
`Havenstad Analytics (e2e …)` company. The per-opportunity route in `pipeline/contacts.py`
still probes when a job seeker is about to write to somebody.

### 8.5 Almost every contact is a shared mailbox, so the e-mail opens "Dear Sir or Madam"

Only **53 of the 1,047 covered vacancies** are addressed to a *named* person
(53 companies); the other **994** go to a shared mailbox — `info@`, `careers@`,
`jobs@`. The generated e-mails therefore open "Dear Sir or Madam", which is
correct but weaker than the product intends.

### 8.6 The contacts pass was stopped, not exhausted

`ensure_apply_contacts` is resumable and was stopped once coverage had passed
the target with margin, because it was contending with the API for the database.
**498 companies carrying 788 vacancies** have still never been walked; re-running the
pass, or pressing **Find contacts for this page**, picks up where it left off —
`apply_contact_resolution` excludes what it has already answered.

### 8.7 SQLite is a single writer, and four packages in flight can starve the API

With `concurrency=4`, ordinary reads blocked for up to **65 seconds** during
generation (`SELECT token_budget … FROM campaign`, `SELECT * FROM session`).
Nothing failed and no request timed out, but a screen opened while a batch is
running can feel dead. The same contention made the composite-profile build take
185 s while the contacts pass was running beside it.

### 8.8 Smaller things

- **No generic job-status endpoint.** Generation and contact discovery start as
  resumable jobs, but the screen watches progress by polling the list, and the
  progress bar counts only rows on the current page. `apply_packages.progress(job_id)`
  exists and is not exposed.
- **No frontend tests.** The repository has no JS test runner and the house rules
  forbid adding a dependency, so the screen itself is covered only by the backend
  contract tests and by the headless walk-through in section 7.
- **The FR-322 override reason is not exposed here.** A blocked package shows its
  blockers; the override flow lives on `/applications`.
- **No "remove from the selection" control per row.** `POST /api/apply/select`
  would support it; `/opportunities` owns that decision today.
- **`ensure_apply_contacts` is not a scheduled job.** It runs from the screen and
  from a script, not from `jobs/runner.py`'s schedule.

---

## 9. What I changed

| file | change |
|---|---|
| `backend/dreamjob/api/routers/apply.py` | `POST /packages/generate` delegates to `pipeline/apply_packages.generate_many` and returns that job; `GenerateIn.regenerate` added; the partial-`parts` run keeps the old path |
| `backend/dreamjob/documents/cv_generator.py` | `_website_urls()` — the contact line prints an address, not a dict repr; `apply_tailoring()` refuses a non-mapping model answer with `ValueError`, so `generate_cv` degrades instead of losing the package |
| `backend/dreamjob/documents/templates/__init__.py` | `CvContact.lines()` prints each address once (`linkedin_url` is copied out of `websites` upstream) |
| `tests/unit/test_documents.py` | `test_the_contact_line_prints_the_address_not_the_object`, `test_a_model_that_answers_with_a_list_still_produces_a_cv` — both fail without their fix |

Data written for this verification: one job seeker and its profile versions, one
directive set, one campaign, 70 opportunities, 30 application packages, 5 dry-run
dispatch rows, and the `apply_contact_resolution` / `apply_domain_probe` /
`company.domain` rows the contacts pass produced. The 30 CVs were re-rendered
once after the header fix, with no model call.

## 10. How to reproduce the measurements

```bash
# coverage by FR-303 method, straight from the repository layer
PYTHONPATH=backend python3 -c \
  "import json; from dreamjob.db.repositories import apply as r; \
   print(json.dumps(r.coverage_by_method(), indent=1))"

# the packages and what they cost
sqlite3 data/dreamjob.db \
  "SELECT task, COUNT(*), SUM(input_tokens), SUM(output_tokens), ROUND(SUM(cost_eur),4)
     FROM llm_call WHERE job_seeker_id='7a3e21ee83b642da95acae96e4fb9663'
    GROUP BY task ORDER BY 5 DESC;"

# nothing was sent
sqlite3 data/dreamjob.db \
  "SELECT delivery_status, COUNT(*), SUM(sent_at IS NOT NULL)
     FROM dispatch WHERE job_seeker_id='7a3e21ee83b642da95acae96e4fb9663'
    GROUP BY delivery_status;"

# what would have gone out, byte for byte
ls data/generated/dry_run/7a3e21ee83b642da95acae96e4fb9663/
```
