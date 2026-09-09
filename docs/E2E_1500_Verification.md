# Verification: 1,500 apply contacts, packages, and nothing sent

Date: 9 September 2026 · commit `2ed854c`
Scope: what the product owner asked for — at least 1,500 vacancies with a real
validated apply contact, application packages generated through to an e-mail
with the tailored CV attached, and **nothing sent** — measured rather than
asserted.

> **The target was not reached. 1,270 of 2,652 vacancies have a validated
> apply contact, against the 1,500 asked for — a shortfall of 230.**
> Section 3 says exactly what is in the way and why no more can be had honestly.

---

## 1. Sign in

| | |
|---|---|
| Application | **http://127.0.0.1:8000** (the built SPA is served by the API; `./scripts/dev.sh` also puts it on :5173) |
| E-mail | **thibault.casteleyn@example.com** |
| Password | **Dreamjob-Verify-2026!** |
| Job seeker id | `7a3e21ee83b642da95acae96e4fb9663` |
| Campaign | *Apply Browser verification* — `5b2e8ed8e6db4ace8b2a00edb79c9bc7` |

The account already existed and was reused, not rebuilt. Sign-in was confirmed
through the API and again through a headless browser (section 6). Thibault
Casteleyn is the fictional persona in `tests/e2e/persona/out/persona.json`; the
address is in the RFC 2606 reserved domain, so nothing addressed to him can
reach a real inbox.

**Nothing was sent.** `DREAMJOB_MAIL_DRY_RUN` defaults to on, is not overridden
in `.env`, and is enforced in the mail backend registry rather than in the
interface. Section 5 is the proof.

---

## 2. The numbers

| | |
|---|---:|
| Vacancies in the corpus | **2,652** at 1,379 companies |
| **Vacancies with a validated apply contact** | **1,270** at 461 companies |
| Target | 1,500 — **short by 230** |
| Application packages, complete | **70** of 70 in this seeker's Apply Browser |
| Opportunities selected | 70, at 41 companies, every one with a contact |
| Dispatches with `delivery_status='sent'` for this seeker | **0** |
| Assembled-and-withheld messages on disk | 12 `.eml` files |
| Token spend on this campaign | 8,704,094 tokens, €4.95 |

### 2.1 A correction to the previous count

The figure reported earlier in the night was **1,324**. That number counted
55 companies created by repeated end-to-end test runs — rows literally named
*"Havenstad Analytics (e2e 235919)"* — whose addresses sit on the reserved
`.example` TLD and can never receive mail. Excluding them gives **1,270**, and
that is the number in this report. The fixture rows are still in the database;
they are excluded by name and by reserved TLD, not deleted.

One consequence is worth stating plainly, because it is good news. Before the
fixtures were excluded, 27 vacancies appeared to be covered by
`pattern_inference` — a *named person's* address guessed from the domain's
convention, the riskiest class there is. Every single one of them was a fixture
(`tom.peeters@havenstad-233431.example` and its 52 siblings). **No real vacancy
in this corpus is reached through a guessed personal address.**

---

## 3. Where the 1,270 come from, and why there are not more

### 3.1 By FR-303 discovery method

| method | vacancies | companies | what it is |
|---|---:|---:|---|
| `conventional_mailbox` | 685 | 209 | a role mailbox (`jobs@`, `careers@`) spelled on a **confirmed** domain — published by nobody |
| `website` | 548 | 226 | printed on the company's own pages |
| `press` | 28 | 20 | a press office mailbox — real, but not a hiring channel |
| `vacancy` | 9 | 6 | the channel the employer stated in the posting itself |
| **total** | **1,270** | **461** |  |

**53% of the coverage is a conventional careers mailbox** — an address
nobody published, spelled onto a domain the identity gate confirmed. The other
585 rest on an address a person or a company actually published. That
distinction is in the database (`contact.email_source_method`) and on the
Contacts screen; it is not blurred into one "validated" number.

### 3.2 The bottleneck is domains, and it is exhausted

701 companies were walked and could not be reached:

| why the company is unreachable | companies | vacancies |
|---|---:|---:|
| no domain could be confirmed | 618 | 1,180 |
| the company name is one short word | 40 | 93 |
| the company name carries no identifying word | 35 | 80 |
| the domain was revoked as a namesake | 7 | 22 |
| addresses found, none survived FR-304 | 1 | 1 |

The domain pass resolved 165 companies:

| rung | companies | vacancies |
|---|---:|---:|
| `derived_confirmed` | 152 | 274 |
| `held_contact` | 9 | 46 |
| `vacancy_text` | 2 | 2 |
| `vacancy_link` | 2 | 2 |

and refused far more than it accepted:

| probe outcome | candidates |
|---|---:|
| `no_mx` | 3,187 |
| `unreachable` | 642 |
| `confirmed` | 286 |
| `rejected` | 95 |

11 domains were written and then taken back as namesakes. That is the safety
margin working: a company whose domain cannot be confirmed stays unreachable,
which is a correct outcome rather than a failure. **The gap to 1,500 cannot be
closed without either inventing domains or relaxing the identity gate, and
neither is acceptable.**

### 3.3 What "validated" does and does not mean

| FR-304 verdict | vacancies |
|---|---:|
| `risky` | 1,269 |
| `unknown` | 1 |

Almost every address reads `risky`, and that is by construction rather than by
accident: the bulk pass runs FR-304 offline — syntax, MX, disposable-domain and
role-address checks — and a role address such as `jobs@` is `risky` **by
definition**. An address that cannot receive mail at all is already `invalid`
and was discarded before it got here. Turning `risky` into `valid` needs one
SMTP probe per domain; that is a decision for the product owner, not something
this run took unilaterally.

---

## 4. The packages

| | |
|---|---:|
| Complete packages | **70** (asked for: at least 40) |
| Artefacts each | motivation e-mail · CV DOCX · CV PDF · briefing PDF · motivation-and-fit PDF · FR-322 check |

Every complete package has all six. The artefact files were opened and measured
on disk, not merely counted in the database.

### 4.1 By employer kind

| employer kind | packages | FR-322 passed |
|---|---:|---:|
| `employer` | 50 | 17 |
| `agency` | 13 | 6 |
| `cannot_tell` | 7 | 1 |

The mix is deliberate: mostly `employer`, with `agency` and `cannot_tell` present
so the tag's effect on the documents can be compared side by side.

**The tag does change what the documents say.** For an `employer` row the
motivation document argues *why I fit this company*. For an `agency` row that
section is not softened — it is **replaced**, because writing about the agency's
culture would be writing about the wrong organisation. From the motivation
document generated for *NXT Hero GmbH*:

> **What the posting says about the employer.** The employer is not named in this
> posting. It was placed by NXT Hero GmbH, an intermediary, for a client it does
> not identify. Nothing below is inferred about that employer: the sentences are
> the posting's own.

and in place of the company-fit argument it offers questions to put to the
recruiter — from the Dutch-language document for *CONNECT CONSULTING BV*:

> **Vragen voor de recruiter.** Wie is de werkgever, en mag ik de naam kennen?

Neither document contains a "why I fit the company" section at all.

One caveat, and it is why some packages were rebuilt for this run: a package
generated *before* its company had an employer-kind verdict carries none of
this. The verdicts were established at 09:31–09:59; every `agency` and
`cannot_tell` package older than its own verdict was regenerated so the
comparison is fair.

### 4.2 The two gates

These are **full-gate** verdicts: the deterministic checks *and* the LLM judge,
re-run in-process on all 70 packages after the three fixes in section 8 went in.
The judge genuinely ran on **69 of 70** (one package's judge call failed on the
provider and kept a deterministic-only pass, which is noted rather than hidden).

They are deliberately **not** the verdicts the interface's **Re-check** button
produces — that button skips the judge entirely, which is finding 6. Section 8
explains what the 46 failures actually contain.

| FR-322 consistency | packages |
|---|---:|
| `fail` | 46 |
| `pass` | 24 |

| NFR-206 leak scan | packages |
|---|---:|
| `review` | 59 |
| `pass` | 11 |

### 4.3 Token cost

| | |
|---|---:|
| Campaign total | **8,704,094 tokens · €4.95** (budget 12,000,000) |
| Per package, mean | **69,900 tokens · €0.0399** |
| Per package, median | 69,871 tokens · €0.0396 |
| Per package, range | 0 – 144,946 tokens |

The per-package figures are **generation only** — CV, briefing, motivation and
e-mail, billed against the opportunity. The campaign total is larger than
70 × the mean because it also carries this verification's own work: the FR-322
gate was re-run over all 70 packages three times while the fixes in section 8
were being made, which is why `cv.consistency` shows 350 calls where generating
70 packages once would have made about 70. Do not divide the campaign total by
70 and call it the cost of a package.

The bottom of that range is not an error. 5 packages were produced while the
campaign's token budget was exhausted, so the NFR-104 ladder dropped the model
and built them from the profile deterministically (`tailored_by_llm: false` in
their generation notes). They have every artefact; their CV is untailored, and
the package says so.

By task:

| task | calls | input | output | cost |
|---|---:|---:|---:|---:|
| `cv.consistency` | 350 | 2,202,376 | 1,608,707 | €2.1593 |
| `generate.motivation` | 128 | 1,290,362 | 614,536 | €0.9371 |
| `generate.briefing` | 104 | 560,557 | 505,663 | €0.6458 |
| `generate.cv` | 131 | 383,735 | 607,538 | €0.7035 |
| `generate.email` | 120 | 566,034 | 364,586 | €0.5061 |
| `profile.composite` | 1 | 4,950 | 25,725 | €0.0270 |
| `profile.dreamjob` | 1 | 2,212 | 12,657 | €0.0132 |
| `normalise.skill` | 2 | 1,602 | 112 | €0.0005 |

**Just over half of that spend bought nothing.** Document generation prefers the
reasoning model, and it usually spends its entire completion budget thinking and
returns nothing usable; the work is then redone on the chat model:

| model | outcome | calls | tokens | cost |
|---|---|---:|---:|---:|
| `deepseek-reasoner` | `truncated` | 191 | 2,577,837 | €1.7155 |
| `deepseek-chat` | `ok` | 205 | 1,420,550 | €0.5125 |
| `deepseek-reasoner` | `ok` | 78 | 808,660 | €0.5312 |
| `deepseek-reasoner` | `empty` | 9 | 85,964 | €0.0333 |

2,663,801 tokens and €1.75 — **54% of the generation spend** —
were burned on calls that returned `truncated` or `empty`, and each one also cost
60–85 seconds of wall clock. The fallback itself is deliberate and documented
(`documents/_llm.py`); what is not deliberate is how often it fires. See finding
9 in section 8.

---

## 5. Nothing was sent — the proof

**a. The switch.** `DREAMJOB_MAIL_DRY_RUN` is absent from `.env` and defaults to
`True` (`config.py:81`). `GET /api/apply/send-status` answers `"dry_run": true`,
`"sending_armed": false`.

**b. No dispatch left.** For this seeker:

| delivery_status | rows | rows with `sent_at` |
|---|---:|---:|
| `dry_run` | 12 | 0 |

`delivery_status='sent'` for this seeker: **0**. (The corpus does hold
244 `sent` rows,
all belonging to end-to-end harness accounts and addressed to fixture domains
such as `jobs@small-example.com`. None belong to this account, and
`RESEND_API_KEY` is empty, so no provider was ever reachable.)

**c. The guard is below the interface.** Reaching straight for a concrete
transport, the way a rogue caller would, still refuses:

```
dry run armed: True
guarded backends: ['gmail_oauth', 'resend']
  gmail_oauth: blocked at the transport -> DREAMJOB_MAIL_DRY_RUN is on: the message
               was assembled in full via the gmail_oauth backend and stopped ...
  resend:      blocked at the transport -> DREAMJOB_MAIL_DRY_RUN is on: the message
               was assembled in full via the resend backend and stopped ...
```

**d. FR-321 — the CV travels, the seeker's own documents do not.** All 12
assembled messages were parsed. Distinct attachment filenames across every one
of them: `CV-Thibault-Casteleyn.pdf`. Messages whose attachments were
anything other than exactly one CV: **0**. The briefing (FR-329) and the
motivation-and-fit document (FR-330) appear in none of them.

Read one for yourself:

```
open /Users/nstephane/Dev/AI_Data_Science_training/dreamjob/data/generated/dry_run/7a3e21ee83b642da95acae96e4fb9663
```

---

## 6. The headless walk

`tests/e2e/verify_1500_walk.py` signs in as the persona and photographs the
product. Screenshots: `tests/e2e/out/screenshots/verify-1500/` (15 images).

Run the walk *after* the pytest suite, not before: `tests/e2e/conftest.py` clears
`tests/e2e/out/screenshots/` at session start, and it takes this directory with
it.

| step |  | what was seen |
|---|:-:|---|
| sign in | ok | thibault.casteleyn@example.com reached /overview |
| journey map | ok | 16 stages painted in the workflow map |
| apply browser list | ok | 50 rows on page 1; facet chips: ['Has a contact 70', 'None yet 0', 'Unreachable 0', 'Not looked 0', 'Generated 70', 'Not generated 0', 'Approved 4', ' |
| dry-run banner on the list | ok | Nothing will be sent. This machine is in dry run. Both send controls below run the whole path for real — the approval rule, the consistency gate, the  |
| tab Email | ok | rendered for Senior Data Platform Engineer, Intelligent Platforms (all ge |
| tab CV | ok | rendered for Senior Data Platform Engineer, Intelligent Platforms (all ge |
| tab Briefing | ok | rendered for Senior Data Platform Engineer, Intelligent Platforms (all ge |
| tab Motivation | ok | rendered for Senior Data Platform Engineer, Intelligent Platforms (all ge |
| tab Checks | ok | rendered for Senior Data Platform Engineer, Intelligent Platforms (all ge |
| single Send | ok | Assembled in full. Nothing was sent. / DREAMJOB_MAIL_DRY_RUN is on: the message was assembled in full via the resend backend and stopped at the transp |
| bulk selection | ok | 4 rows are eligible for a bulk send; ticked 3 |
| Send all | ok | Exactly what would go out, and to whom ✕ Nothing below will be sent. Each message will be built in full — recipient, subject, body, your tailored CV a |
| employer-kind badge (agency) | ok | badge 'Via agency · employer not named'; popover opened; Via agency · employer not named confidence 95% This vacancy was posted by a staffing or recru |
| employer-kind badge (cannot_tell) | ok | badge 'Employer type not verified'; popover opened; Employer type not verified confidence 0% We have not established whether the organisation on this  |

14 of 14 steps passed.
One console error: the `401` from the session probe on the sign-in screen, before anyone has signed in. That is expected.

---

## 7. What to click

| To see | Where |
|---|---|
| The journey map | sidebar **Overview → Where I am**, or `/overview` |
| The Apply Browser and its counts | sidebar **Apply → Apply browser**, or `/apply` |
| One package's five tabs | click any row on `/apply` → **Email · CV · Briefing · Motivation · Checks** |
| The tailored CV, briefing, motivation | the **CV**, **Briefing** and **Motivation** tabs each preview the PDF and offer a download |
| The FR-322 consistency report | the **Checks** tab |
| The dry-run banner | the top of `/apply`, above the list — and again in the report after pressing either Send control |
| What a message would look like | press **Send email with CV attached** on an approved package; the report names the recipient and the `.eml` file on disk |
| The bulk path | tick rows, then **Send all with attachment** — it shows exactly who would receive what before it will run |
| The employer-kind badge | `/opportunities/{id}` for an agency or unverified row — click the badge for its evidence (a direct employer deliberately carries no badge) |
| Contact coverage across the corpus | sidebar **Apply → Contacts**, and **Discover → Companies** |
| Token spend | sidebar **Plan → Campaigns → Apply Browser verification** |

---

## 8. What does not work

Written as found, with the evidence.

### 1. The target was missed by 230 — and the shortfall is structural

1,270 of 2,652 vacancies have a validated contact. 618
companies are blocked on a domain that could not be confirmed. The ladder was
walked three times, including a retry of every unresolved company, and the
retry recovered nothing. Reaching 1,500 from here needs a source of company
domains this system does not have — a purchased register, or the job seeker
naming employers by hand — not more of the same crawling.

### 2. The previous count was inflated by end-to-end fixtures

Fixed in this report (section 2.1), **not** in the database. 55 fixture
companies and their `.example` contacts are still there, and any future query
that does not exclude them will over-report again. Worth a cleanup task, and
worth teaching the e2e harness to tear its companies down.

### 3. FR-322 blocked packages on a name the sibling check allowed — **fixed**

`check_free_text` is given the company and role the application is *for*, and
permits them. `judge()` was not given that set, so `unsupported_tokens` read the
target employer as an invented one and escalated the finding to `high`, failing
the package. The report literally read *"Not in the profile: Senior Fullstack
Engineer, Solactive"* — the company being applied to. This also broke the
module's own stated rule that the judge "can never fail a document on its own".

Fixed in `backend/dreamjob/documents/consistency.py`: `allow` is threaded into
`judge()` and `unsupported_tokens()` and folded exactly as `check_free_text`
folds it. Pinned by `test_judge_allows_the_company_and_role_the_application_is_for`.
An invented employer and an invented figure are still caught.

### 4. The leak scan read a date as a phone number and hard-blocked the package — **fixed**

`_PHONE_RE` matched `2015-2017`, reduced it to the eight digits `20152017`, and
reported *"This telephone number is not in this job seeker's material"* at `high`
severity. That sets `leak_scan_status = 'fail'`, which is a **non-overridable**
approval blocker — so a CV or motivation document that dates its experience with
a hyphen could never be approved or sent. 3 packages in this run carried the
finding — on the spans `(2015-2017`, `2017-2019` — and every CV that dates its
experience is exposed to it.

Fixed in the same file with a narrow `_YEAR_SPAN_RE` guard (two four-digit years,
a dash, nothing else). Real numbers — `+32 471 12 34 56`, `+44 20 7946 0958` —
are still reported. Pinned by
`test_leak_scan_does_not_read_a_span_of_years_as_a_phone_number`.

### 5. The judge escalation ignored the document's language, so German CVs failed by construction — **fixed**

The same shape as finding 3, and the one that was actually costing the most.

`capitalised_phrases` takes a language and says why:

> Sentence-initial words are skipped... **German capitalises every noun, so there
> a run has to be more than one word, an acronym or a legal form before it counts
> as a name at all — otherwise the scan would report most of the document.**

`check_free_text` passes `language=document.language`, so it gets that. But
`unsupported_tokens` called `capitalised_phrases(text)` with no language, so it
always applied the English rule — and in German every noun is capitalised. The
judge escalation therefore read *Erfahrung*, *Systeme*, *Entwickler*,
*Monolithen* and even *Sie* as invented proper nouns and failed the package.
Across the failing set those five accounted for more escalations than any real
name did.

Fixed by threading `language` through `judge()` into `unsupported_tokens()`, so
both halves of the check read the same document the same way. Pinned by
`test_judge_escalation_honours_german_noun_capitalisation`; a German-language
employer (`Globex International GmbH`) is still caught.

### 6. The "Re-check" control runs half the gate, silently — **open**

This is the most consequential thing found tonight, and it was found by using it.

`api/routers/applications.py:327` re-runs the check like this:

```python
updated = package_module.run_consistency(seeker.id, package)
```

with no `llm` argument. `run_consistency` defaults `llm` to `None` and then sets
`use_judge=llm is not None`, so **the LLM judge never runs on a manual re-check**.
The verdict that comes back is the deterministic half alone, it is written to
`consistency_status` as if it were the whole gate, and the only trace is
`judge_ran: false` buried in the stored report.

The consequence is that a package the full gate failed can be turned green by
pressing **Re-check** in the Checks tab, and neither the badge nor the tab says
that half the check was skipped. Measured: after 44 failing packages were put
through the endpoint, every one of the 70 read `pass` — with `judge_ran: false`
on 53 of them.

The numbers in section 4.2 of this report are therefore **not** the endpoint's:
the gate was re-run in-process with a client attached
(`run_consistency(..., llm=LLMClient(...))`, the same call the generator makes)
so that what is reported is the full-gate verdict. The endpoint itself is left
as found.

The fix is to give the router a client the way the generator does, and to let
the NFR-104 ladder decide whether to spend on it — with the screen saying so
when it does not. Until then, treat a `pass` produced by **Re-check** as
"deterministic checks only".

### 7. A failed FR-322 check is overridable at approval but not at send — **open**

`documents/package.py:460` marks the consistency blocker `"overridable": True`,
and the interface asks for a written reason. Both send paths then refuse it
outright with no override at all — `mail/dry_run.py:393` and
`mail/dispatcher.py:407`. Recording an override therefore buys an approved
package that can never be sent. One of the two should give way; the send-side
refusal is the safer one to keep, in which case approval should stop offering an
override it cannot honour.

### 8. What still fails FR-322 is descriptive prose, not invented employers — **open**

After the three fixes above, **46 of 70 packages still fail** the full gate.
That number is real and the product owner will see it, so here is exactly what
is in those failures: **78 high findings, every one of them from the LLM judge —
zero from the deterministic checks.** Not one is a fabricated employer, date,
figure or qualification. 43 of the 49 distinct flagged phrases are multi-word,
and they read like this:

```
TypeScript React and Cloud-Native Backend Expertise
Enterprise Architecture Expertise
Data-Driven Product Experience
Software Engineer & Technical Leader
German and English
Kafka-pijplijn
Jahren Erfahrung
```

These are skill phrases and CV headline fragments. `capitalised_phrases` returns
*maximal runs* of capitalised words, so a tailored headline collapses into one
"name", and the fallback test — is every word in the profile? — fails on
ordinary vocabulary: **Expertise**, **Experience**, **and**. `Enterprise` and
`Architecture` are both in the profile; `Expertise` is not, and that is what
fails the package.

The same detector legitimately catches `Globex International`, and it is
calibrated for `medium`-severity advice — `check_free_text` never fails a
document with it. Only the judge escalation promotes it to `high`. Fixing it
means deciding what makes a capitalised run a *name* rather than a phrase — a
prose stop-list, a required corporate marker, or splitting on conjunctions
before testing. That is a calibration judgement on a gate whose whole purpose is
to stop CV fabrication, so it was left for the product owner rather than taken
here.

The practical consequence today: a job seeker meets a failed check on a package
whose CV is factually fine, and FR-324 asks them to write an override reason
(see finding 7 for why that override then does not help).

### 9. Over half the generation spend is thrown away — **open**

Section 4.3. 2,663,801 tokens (€1.75) on `truncated` and `empty`
strong-model calls, plus 60–85 seconds each. The comment in `documents/_llm.py`
is right that raising the ceiling would only buy a longer think — which is an
argument for not preferring the reasoning model for these tasks at all. Routing
`generate.cv`, `generate.briefing`, `generate.motivation` and `generate.email` to
the chat model directly would roughly halve both the bill and the wall clock.

### 10. `slow statement` warnings in the log are false — **open**

`observability/db_logging.py` times a statement from its own start to the start
of the next one on that thread. The last statement before an LLM call is the
budget read, so the log carries lines like
`slow statement ms=60722.8 sql=SELECT token_budget, tokens_used, cost_eur FROM campaign WHERE id = ?`
for a primary-key lookup. The technique is documented in the module, but the
output is alarming and wrong, and it will send someone hunting a database
problem that does not exist.

### 11. 28 vacancies would be applied to at a press office — **open**

20 companies are reachable only through a `presse@` / `press@` mailbox. The
address is real and published, and the contact ladder is honest about where it
came from (`email_source_method = 'press'`), but a job application arriving at a
press office is unlikely to reach a hiring manager. Worth deciding whether
`press` should count as an apply channel at all.

### 12. The verification account does not itself demonstrate the widening

All 41 of this seeker's companies were resolved at 04:00–05:00, before the
contact pass that widened the corpus ran at 11:00. Its Apply Browser reads 70 of
70 with a contact, and that was already true this morning. Tonight's widening is
visible in the corpus totals and on the Contacts and Companies screens, not in
these 70 rows.

### 13. `cannot_tell` is still the majority verdict

| employer kind | vacancies | companies |
|---|---:|---:|
| `cannot_tell` | 1,677 | 949 |
| `employer` | 624 | 211 |
| `agency` | 402 | 56 |
| `(not judged)` | 42 | 2 |

The employer-kind resolver reaches a verdict it will stand behind for a minority
of companies. That is the honest behaviour — guessing is the failure this
feature exists to prevent — but it means the badge is absent or unverified on
most rows, and the largest single cause is the same missing domain as section 3.

---

## 9. Tests

End-to-end (`tests/e2e`, 42 tests, driving the real browser against the running
app): **42 passed in 434.48s (0:07:14)** — 0 failed, the same 42 as the last full run.

Unit and integration: **1,295 passed, 0 failed**, including the three new tests
that pin the fixes in findings 3, 4 and 5:

- `test_judge_allows_the_company_and_role_the_application_is_for`
- `test_leak_scan_does_not_read_a_span_of_years_as_a_phone_number`
- `test_judge_escalation_honours_german_noun_capitalisation`

Everything in this document can be re-measured, in this order:

```
cd /Users/nstephane/Dev/AI_Data_Science_training/dreamjob
PYTHONPATH=backend python3 -m pytest tests/unit tests/integration -q
PYTHONPATH=backend python3 -m pytest tests/e2e -q                    # 42 tests
DREAMJOB_VERIFY_AGENCY_ID=42ee603adb0749a8afc2c9f7ced9af67 DREAMJOB_VERIFY_CANNOT_TELL_ID=31fed98f487449ba921f37e0aa6f6cd2 PYTHONPATH=backend python3 tests/e2e/verify_1500_walk.py            # the screenshots
```

The walk runs last because the pytest suite clears the screenshot directory.

## 10. What was changed

Three fixes, all in `backend/dreamjob/documents/consistency.py`, each with a
test. Nothing else in the product was modified; the contact ladder, the domain
gate, the employer resolver and the mail layer are exactly as the earlier phases
left them.

| | |
|---|---|
| Fixed | the judge escalation now receives the `allow` set and the document language that `check_free_text` already had (findings 3 and 5), and a span of years is no longer read as a telephone number (finding 4) |
| Tests | +3 in `tests/unit/test_documents.py` |
| Data | 40 packages generated, 5 regenerated, the FR-322 gate re-run over all 70; the campaign token budget was raised from 2M to 12M through `PATCH /api/campaigns/{id}` to allow it |
| Not changed | the `Re-check` endpoint (finding 6), the approval/send override mismatch (finding 7), the escalation's phrase granularity (finding 8), model routing (finding 9) |
