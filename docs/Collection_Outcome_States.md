# Collection outcome states: what a plan item ended in, and why

*Verification note, 2026-09-09. Every number below is measured on
`data/dreamjob.db` — the installed 617 MB database — before and after applying
migrations 130, 131 and 132, plus two live collection runs over the real
network. A byte-consistent snapshot of the pre-migration database is kept in
the session scratchpad as `pre130_backup.db`; the before-column of every table
here is read from it, not remembered.*

## 0. Verdict

The three migrations apply cleanly and do what they claim. **The claim that
"538 errors becomes roughly a dozen" is not what happened.** The real corpus
had 1,030 counted errors, not 538; 509 of them were relabelled as expected
outcomes, and **485 rows are still labelled `failed`** — not a dozen.

Of those 485, **only 22 are genuinely actionable**. The other 463 are two
populations that the new rules do not yet reach, and the larger one is a real
finding:

> **231 ATS plan items are recorded as failures — and as suspected adapter
> breakage — for boards that answered `HTTP 200` with a well-formed, empty
> vacancy list.** A company with no open roles is being reported as
> *"the source layout has probably changed (NFR-403)"*. This is the same class
> of bug the outcome-state work exists to fix, one layer down, and it survived
> the fix.

Two integration defects were found and fixed during verification; without the
first, none of the re-classification reached the operator at all.

---

## 1. Before and after, straight from the database

The two queries the brief asked for, run on the pre-migration snapshot and on
the installed database. The verification run's own 14 campaigns are excluded
from the *after* column so the table measures the migrations and nothing else.

```sql
SELECT status, COUNT(*) FROM source_plan_item GROUP BY status;
SELECT SUM(error_count) FROM source_plan_item;
```

| `status`             | before | after  | what moved |
|----------------------|-------:|-------:|------------|
| `planned`            | 45,959 | 45,878 | −81 fixture rows deleted (132) |
| `done`               |  2,020 |  2,020 | unchanged |
| `skipped`            |  1,600 |  1,577 | −23 fixture rows deleted (132) |
| `failed`             |    990 |    485 | −505 relabelled `blocked`/`gone` |
| `running`            |    126 |    122 | −4 relabelled (the rule keys on evidence, not on status) |
| `blocked` *(new)*    |      — |    298 | robots.txt refusals and 403 bot walls |
| `gone` *(new)*       |      — |    211 | 404/410 on a board the registry offered |
| **total rows**       | 50,695 | 50,591 | −104 test-fixture plan items |
| **`SUM(error_count)`** | **1,030** | **521** | −509 |

The four counters still add up: `error_count` 521 + `blocked_count` 298 +
`gone_count` 211 + `failed_count` 485. Nothing was deleted; the counts moved
into columns of their own.

### The campaign the product owner actually looks at

"538 errors" does not appear anywhere in this corpus. The dashboard the brief
describes is campaign `5b2e8ed8…` *(Apply Browser verification)*, owned by
`thibault.casteleyn@example.com`, and it reported **976 failed items / 991
counted errors**. After the migrations:

| answer | count | share of the 5,262 plan items |
|--------|------:|------:|
| `succeeded` | 1,752 | 33% |
| `capped` (FR-186 page budget) | 2,531 | 48% |
| `failed` | **472** | 9% |
| `blocked` (robots.txt, 403, ToS) | 293 | 6% |
| `gone` (dead board slugs) | 211 | 4% |
| `skipped` | 3 | <1% |

The brief's component figures were close but not exact: **211** dead board
slugs, not 308 (the other 91 of the 302 items carrying a 404 are `news.rss`
guessed feed URLs, which are not boards); **293** refusals, not 192 + 51; and
**104** fixture items, which is exact.

---

## 2. What each state means

| state | meaning | counts as an error? | retried? |
|-------|---------|---------------------|----------|
| `succeeded` | records were written to the knowledge base | no | no |
| `blocked` | we declined, correctly: robots.txt (FR-182), a 403 bot wall, a 451, or terms of service (IR-101) | **no** | no — asking again this week cannot change the answer |
| `gone` | 404 or 410 on an ATS board the registry offered: the tenant has left | **no** | no — and the registry is told, so it stops offering the slug |
| `failed` | something actually went wrong: 5xx, 429, transport error, parse crash, adapter exception, missing credential | **yes** | yes |
| `skipped` | there was nothing to do: excluded by the job seeker (FR-163), or the source answered and stated it holds nothing | no | no |
| `capped` | the FR-186 page budget ended the run first — a budget signal, not an outcome | no | yes, by raising the cap |

Three deliberate boundaries, each verified against a live source in §4:

* **401 and 429 are failures, not decisions.** A credential this installation
  was supposed to hold and does not is our own defect. "Come back later" is a
  rate we are exceeding, which the operator has to see.
* **A 404 is only `gone` for a *board*.** A search endpoint that answers 404
  has moved, and the adapter is now wrong about it — that stays a failure.
  This is why 91 `news.rss` 404s on guessed feed URLs remain failures.
* **`blocked` says nothing about existence.** robots.txt tells us what we may
  read, never what is there, so a refusal never touches the registry.

---

## 3. Two integration defects found during verification

### 3.1 The re-classification was invisible on the dashboard

**`backend/dreamjob/api/routers/campaigns.py`, `_bucket_of`.** The endpoint
resolved a plan item's outcome from `caps.outcome.state` *before* the
`outcome_state` column. Migration 130 relabels a row by rewriting `status` and
`outcome_state`; it cannot rewrite the `caps` JSON blob, which still holds the
word the pre-fix code wrote there — `failed`.

Measured on the live payload before the fix:

```
database   :  succeeded 1752  blocked 293  gone 211  failed 472
dashboard  :  succeeded 1746  blocked   0  gone   0  failed 976
```

Every one of the 504 relabelled rows went straight back into the failure count.
The product owner would have seen no change at all. Fixed by preferring the
migrated column; the `caps` blob remains the fallback for rows that predate the
column. Three regression tests in `tests/unit/test_campaign_outcomes.py`.

### 3.2 The breakage banner buried the failure list

**`backend/dreamjob/pipeline/collection.py`, `status()`.** NFR-403's
`extraction_success_rate` is a property of the *adapter* — one
`source_catalogue` row — but a breakage entry was appended per *plan item*. The
payload carried **561 entries naming 6 adapters**, `board.eures (8%)` repeated
228 times, and the amber banner rendering them ran for a full screen directly
underneath the failure count. Now keyed by adapter: 6 entries, one line. Test
in `tests/unit/test_collection_outcome_states.py`.

### 3.3 One un-wired integration completed

`without_gone()` existed but nothing called it, so the registry could retire a
slug and planning would keep offering it. Wired at
`backend/dreamjob/pipeline/discovery.py:612`. Verified in §5.

---

## 4. End to end on the live network

Two runs against the real database. All four states were produced by real
sources, not stubs.

**Pass A — real ATS boards, real network** (campaign `be2fee7d…`):

| target | answer | state | `error_count` | registry told |
|--------|--------|-------|------:|---------------|
| `ats.ashby` / `airops` | 200, 14 records | `succeeded` | 0 | `live`, `last_status` 200 |
| `ats.personio` / `aevoloop` | robots.txt disallows | `blocked` | **0** | **nothing** — correct |
| `ats.personio` / `10xfounders` | HTTP 404 | `gone` | **0** | strike 1→2, **retired** |
| `news.rss` (guessed feed) | HTTP 404 ×6 | `failed` | **1** | n/a — not a board |

**Pass B — the 5xx regression**, the one that would re-hide a real outage. A
real HTTP server on `127.0.0.1:8741` in its own process stands in for the
vendor, so the whole egress path runs — robots.txt, rate limiter, cache,
refusal classifier, registry writer — and only the hostname is redirected. Run
twice, because retirement takes two strikes:

| board | answer | attempt 1 | attempt 2 | registry after two strikes |
|-------|--------|-----------|-----------|---------------------------|
| `ok-board` | 200 + 1 job | `succeeded`, err 0 | `succeeded`, err 0 | `live` |
| `sick-board` | **503** | **`failed`, err 1** | **`failed`, err 1** | **`unverified`, failures 0 — untouched** |
| `dead-board` | 404 | `gone`, err 0, strike 1 | `gone`, err 0, strike 2 | **`gone` — retired** |

**A 5xx never retires a board and never stops counting as an error, even
repeated.** Given Personio alone holds 2,167 slugs in this registry, that is
the difference between a bad afternoon and an emptied inventory.

*One small gap:* the 503 reaches the registry through `mark_unreachable` with
`http_status=None`, because the egress client converts a repeated 503 into a
`RateLimited` exception that carries no status. State and counter are correctly
untouched — the part that matters — but `last_status` is not recorded, so the
registry cannot show *why* the board was unreachable. Cosmetic, worth a line.

---

## 5. The registry learned

| | |
|---|---:|
| rows in `board_registry` | 4,518 |
| marked `gone` (retired) | **1** |
| marked `live` | 33 |
| `unverified` — never asked | 4,484 |
| carrying one 404 strike, one more from retirement | **210** |

Only one board is retired *today*, and that is correct rather than
disappointing. Migration 131 credited every 404 this installation had already
paid for — 211 boards — but **each of those 211 slugs 404'd exactly once**,
verified three ways (211 plan items over 211 distinct slugs; 208 matching
`http_cache` negatives, i.e. the same response, not a second one; one request
per slug in `logs/app.log`). Retiring on one observation would break the
two-strike rule, which exists because a single 404 is as likely to be a tenant
renaming its board as one that has left. The next campaign's 404 retires all
211 — a one-off cost of 211 requests, about 3.5 minutes at the compliant rate.

The 212th board, `personio/10xfounders`, was retired during pass A above: it
came in at one strike and answered 404 again.

**Planning no longer offers it**, measured through the real planner:

```
registry file rows        : 4518
retired (state='gone')    : 1
offered to planning       : 4517
personio/10xfounders  in shipped file = True   still offered to planning = False
board_targets() planned 2166 personio boards;  10xfounders in the planned set: False
```

The filter is "not known to be gone", **not** "verified live" — 4,484 of 4,518
rows are unverified, and filtering to verified-live would leave 33 targets and
kill the campaign.

**Fixtures: 0 remain.** All 104 `spine.*` / `broken_board` / `stub_board` plan
items are gone, and `insert_plan_item` now refuses to create another.

---

## 6. What is still misclassified

The 485 rows still labelled `failed`, categorised by their stored `last_error`:

| # | category | is it really a failure? |
|--:|----------|--------------------------|
| **231** | ATS board fetched, `extracted no record` | **No — see 6.1** |
| **123** | `registry.kbo` company lookup found nothing | **No** — the company is not in the Belgian register; that is an answer, not a breakage |
| **92** | 404/410 on a **non-board** (`news.rss` guessed feeds) | Arguably not — but correct under the stated rule, and cheap to stop guessing |
| 20 | `board.eures` / `news.rss` `extracted no record` | Mixed; EURES already has `no_matches`, these predate it |
| **22** | rate limits, transport errors, wrong content type, missing credentials, normalisation failures | **Yes — this is the real list** |

### 6.1 The finding: an empty board is being reported as a broken adapter

231 ATS plan items are recorded `failed` with
*"fetched 1 page(s) and extracted no record — the source layout has probably
changed (NFR-403)"*. I checked every one of them against the cached HTTP
response, reconstructing each vendor's exact URL. **215 of the 216 that could
be matched to a cached body answered `HTTP 200` with a well-formed, empty
vacancy list.** Zero exceptions.

```
72  ats.recruitee   200, {"offers":[]}
77  ats.teamtailor  200, RSS channel with no <item>
41  ats.personio    200, <?xml …?><workzag-jobs></workzag-jobs>   (72 bytes)
11  ats.ashby       200, empty postings array
 7  ats.greenhouse  200, {"jobs":[]}
 7  ats.lever       200, []
16  ats.workable    (two-endpoint adapter; not URL-matched from cache)
```

These are not broken adapters. Each of those adapters works overwhelmingly
often in the same corpus — `ats.recruitee` succeeded on 722 boards and
collected 11,423 records while 72 came back empty (9%); `ats.teamtailor` 434
successes against 77 empties (15%). What they have in common is a company with
no open roles.

`ItemOutcome` already has the right state for this — `no_matches`, "the source
answered and stated it holds nothing" — and it is already used: `eures.py` sets
`fetch_outcome.stated_empty`, which is why 9 empty EURES partitions are not
reported as breakages. **No ATS adapter sets it.** So an empty board falls
through to `extracted_nothing`, which `_STATE_STATUS` maps to `failed`.

Two consequences, both visible today:

1. **The failure list opens on false positives.** In the screenshot of the
   operator's own dashboard, the first eleven rows of "the list to work
   through" are `ats.ashby` boards with no vacancies.
2. **NFR-403's breakage detector is being fed false breakage.** Empty boards
   drag the per-adapter extraction rate below the 0.5 threshold, so the amber
   banner names adapters that are working: `ats.workable` 18%, `ats.greenhouse`
   55%, `ats.lever` 58%, `ats.personio` 61%.

The fix is the same shape as the one already made for EURES: have the ATS
adapters increment `fetch_outcome.stated_empty` when the board parses cleanly
and lists nothing. That is an adapter change, not a classification change — the
classifier already handles the state correctly once it is told.

### 6.2 One number on the screen still disagrees

The collection stage row shows **"615 errors"** immediately below the honest
"472 failed". That is `job_run.error_count`, a run-level aggregate written by
the pre-fix code (its checkpoint records `errors: 991`). It is not re-bucketed
here, and deliberately so: unlike a plan item, the job row stores no per-event
evidence, so there is no rule that could re-classify it from what is on disk —
only a guess. The plan items *are* the evidence and they are correct; the job
counter self-corrects on the next run. Flagged rather than fixed, because
quietly rewriting a counter to match a nicer number is the move this whole
change exists to prevent.

### 6.3 Ruled out

* `OperationalError: table http_cache has no column named content_hash` — one
  row, timestamped 02:45:26, twenty-four seconds before migration 093 was
  applied. A migration race, not a live defect; the column exists.
* Passes A and B nudged the rolling `extraction_success_rate` of
  `ats.greenhouse` and `ats.lever` by a fraction of a percent. Self-correcting.

---

## 7. Verification performed

| check | result |
|-------|--------|
| Migrations 130 → 131 → 132, in order, on the installed database | applied at boot, 458 ms / 159 ms / 19 ms |
| Rehearsed first on a snapshot copy | identical row counts |
| `pytest tests/unit -q` | **1,371 passed, 0 failed** |
| `ruff check backend/dreamjob tests` | clean |
| `npm run build` | clean |
| Live collection, real network, four states | all four correct (§4) |
| 5xx regression, twice | **failed both times; registry untouched** |
| Registry retirement + planning exclusion | verified through the real planner |
| Dashboard, headless, as `thibault.casteleyn@example.com` | `472 failed` in red at the top of the block; everything else quiet |

Screenshots: `tests/e2e/out/screenshots/outcome-states/`.

## 8. Recommended next

1. **Set `stated_empty` in the ATS adapters** (§6.1) — removes 231 false
   failures and stops NFR-403 flagging four working adapters.
2. **Give `registry.kbo` the same treatment** (§6, 123 rows): "not in the
   register" is an answer.
3. **Run one more campaign** to spend the second strike on the 210 boards
   sitting at one, after which the registry stops paying for them for 90 days.
4. Consider whether `news.rss` should guess feed URLs at all (§6, 92 rows); a
   404 on a guessed URL is a planning cost, not a collection failure.
