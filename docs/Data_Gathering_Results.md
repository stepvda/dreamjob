# Data Gathering: Integration and Measured Results

Date: 2026-09-09
Scope: integrating the nine slices that implemented `docs/Data_Gathering_Plan.md`, and
proving with live measurements that the pipeline now actually retrieves data.

---

## 1. Verdict

**Retrieval works.** Before this integration the product held **0 real vacancy rows**;
it now holds **1,362**, each with a company row and provenance attached, collected
from two live sources through the product's own pipeline.

The nine slices were individually sound: the full unit suite was green (903 passed)
and `ruff` was clean before I changed anything. **Every defect found here lived
*between* two slices**, in a seam that no single slice owned, and none of them was
visible to any slice's own tests.

The single largest cause of "the pipeline retrieves nothing" was not a code defect at
all: **migrations 090–093 had never been applied to the installed database**, so every
fetch died on a missing column.

---

## 2. What was broken between the slices

### 2.1 The installed database was four migrations behind (blocking, total)

`data/dreamjob.db` was at migration `080`. The four migrations the new code depends on
had never run against it.

```
before:      ['001','002','012','026','030','040','052','054','070','080']
applied now: ['090','091','092','093']
```

The first live collection attempt failed with:

```
SourceUnavailable: [ats.greenhouse] 1 request(s), 0 answered;
OperationalError: table http_cache has no column named content_hash
```

`egress/client.py` writes `content_hash` and `raw_document_id` on every cache store.
Those columns are added by `090_fetch_ledger.sql`. Until it ran, **every request from
every adapter raised**, was swallowed as "no records", and was written down as a source
with nothing to offer. `fetch_ledger` and `board_registry` did not exist either.

All four applied cleanly to the live 16 MB database (backup taken first). This is the
one finding a product owner must act on before anything else: *a fresh checkout with an
old database retrieves nothing, silently.*

### 2.2 EURES was never actually partitioned (blocking the acceptance test)

Two halves of item N2 were each written against an assumption about the other, and both
assumptions were wrong.

**(a) The adapter never declared it could be partitioned.**
`discovery.reads_partitions()` refuses to emit a partitioned plan unless the adapter
declares `PARTITION_QUERY_KEYS`. `EuresAdapter` never declared it, so the planner
silently degraded to one country-wide item:

```
adapter declares PARTITION_QUERY_KEYS: None
discovery.reads_partitions(EuresAdapter): False
plan items produced: 1          <- should be 54 for BE
```

**(b) The two halves spelled the sector axis differently.**
`discovery` writes `nace_section` (a string); `EuresAdapter.sector_codes()` read only
`sector_codes` / `sectorCodes`. Forcing partitioning on exposed the consequence — the
sector filter never reached the wire:

```
SEARCH BODY SENT: {'keywords': [], 'locationCodes': ['BE1'], 'sortSearch': 'BEST_MATCH',
                   'resultsPerPage': 50, 'page': 1, 'publicationPeriod': 'LAST_MONTH'}
                   ^ no sectorCodes

distinct request bodies among first 40 plan items: 3
```

**40 plan items collapsed to 3 distinct HTTP requests.** All 18 NACE sections of a region
asked the identical question, bought the identical answer 18 times, and would have
reported it as 18 successful sources.

After the fix:

```
reads_partitions: True
BE plan items: 54          distinct request bodies for BE: 54 of 54 items
BE+NL plan items: 126      <- exactly the plan's own arithmetic
sample body: {..., 'locationCodes': ['BE1'], 'publicationPeriod': 'LAST_WEEK',
              'sectorCodes': ['A']}
```

### 2.3 An empty partition was reported as a broken source (N7 over-correcting)

Nine of the 54 Belgian partitions came back `failed`, with:

> `fetched 1 page(s) and extracted no record - the source layout has probably changed (NFR-403)`

They are not broken. The register is simply empty for them. Probed live:

```
BE1 NACE A: HTTP 200 numberRecords=0 jvs=0     (agriculture, Brussels, last week)
BE1 NACE B: HTTP 200 numberRecords=0 jvs=0     (mining)
BE3 NACE S: HTTP 200 numberRecords=0 jvs=0
BE1 NACE J: HTTP 200 numberRecords=10 jvs=10   (control: a partition that has rows)
```

N7 correctly stopped counting silent zeros as success, but it went one step too far: a
partitioned sweep asks narrow questions *on purpose*, and 17 % of them having no answer
is the design working. Reporting those as probable breakage buries the real breakages
among false ones — precisely the failure N7 exists to prevent, inverted.

### 2.4 europa.eu was read five times faster than it asks (compliance)

The measured pace of the first sweep was **2.2 s/request** against a host whose
robots.txt states `Crawl-delay: 10`. `honour_crawl_delay` was `True` and the limiter
still returned 2.0 s.

Root cause: europa.eu's robots.txt puts a **blank line** between `User-agent: *` and its
`Crawl-delay: 10`. A blank line ends a record, so `urllib.robotparser` attributes the
directive to no group at all:

```
  34: ''
  35: '#IM0017899419 - 20/06/2019'
  36: 'Crawl-delay: 10'

crawl_delay(product UA) = None
crawl_delay("*")        = None
default_entry delay: None
```

The parser is right; the pace was still wrong. Two slices also disagreed about it:
`admin.PUBLISHED_CRAWL_DELAY_SECONDS` hard-coded `board.eures: 10.0` and priced the
catalogue at 0.1 rps, while `DomainLimiter` — the thing that actually paces requests —
never learned it.

### 2.5 The fetch ledger was written by nobody (N4 half-delivered)

`090_fetch_ledger.sql` created the table and `egress/client.py` shipped `record_fetch()`,
`last_fetch()`, `target_is_fresh()` and `fresh_targets()`. Nothing called any of them:

```
$ grep -rn "record_fetch" backend/dreamjob --include="*.py" | grep -v egress/client.py
(no matches)
```

Per-target reuse therefore had no evidence to work from, and re-running a campaign would
re-fetch every target.

### 2.6 Seams that turned out to be fine

Two risks flagged in the slice reports did **not** materialise, verified directly:

- **All 4,511 registry boards reach discovery**, including the 1,066 Teamtailor and 29
  Workable boards the registry slice feared would be dropped. `discovery.board_targets`
  gates on catalogue keys, not on `detect.SUPPORTED_VENDORS`:
  `{'registry': 4511, 'unsupported_vendor': 0}`.
- **`DEFAULT_STALENESS_DAYS["board_registry"] = 30`** (C8) had already landed.

---

## 3. What I changed

| File | Change |
|---|---|
| `data/dreamjob.db` | Applied migrations 090, 091, 092, 093 (backup kept in scratchpad) |
| `adapters/jobboards/eures.py` | Declared `PARTITION_QUERY_KEYS`; `sector_codes()` also reads the planner's `nace_section`; added `_stated_total()` and set `fetch_outcome.stated_empty` when the service states a count of zero |
| `adapters/vacancy_source.py` | `FetchOutcome.stated_empty`; `settle()` no longer charges the extraction rate when every answer stated emptiness |
| `pipeline/collection.py` | New `no_matches` outcome state (→ `done`, no error) with its own message; `_target_key()` / `_record_ledger()` write the N4 fetch ledger once per plan item; `_UnitEgress` remembers the last URL, status and validators |
| `egress/client.py` | `PUBLISHED_CRAWL_DELAY_SECONDS` floor so a delay a strict parser drops is still honoured |
| `tests/unit/test_gathering_integration.py` | **New.** 18 tests pinning every defect above |

No new dependency, no SQL outside `db/`, no rate raised anywhere, no new source, no
robots bypass. The one rate that changed went **down**, from 2 s to 10 s on europa.eu.

**Full suite: 921 passed. `ruff check backend/dreamjob tests scripts`: clean.**
(903 before; +18 new integration tests in one new file.)

---

## 4. Measured retrieval — Campaign B (EURES Belgium, last week, partitioned)

Bounded run through `collection.collection_worker`: all 54 Belgian partitions
(3 NUTS-1 regions × 18 NACE sections, N/T/U excluded), `LAST_WEEK`, **1 page each**.

```
plan items:            54          (was 1 before the fix)
requests:              54          (1:1 — every partition a distinct question)
wall clock:            117 s at the old 2.2 s pace / 181 s for 18 partitions at the
                       compliant 10.06 s pace
records normalised:    1,541
distinct vacancy rows: 1,136       (dedup merged 405 = 26 %)
distinct employers:      456
partitions with rows:     45
partitions truly empty:    9       (numberRecords = 0, verified live)
LLM tokens:                0       (board.eures is a deterministic adapter)
```

**Measured per-request yield (depth 1, across partitions):**

| | per request |
|---|---|
| records normalised | 28.5 |
| distinct vacancies | 21.0 |
| distinct employers | **8.4** |

### 4.1 How big the target actually is

The service states its own totals, so the full-sweep size is measured, not guessed:

```
BE country, LAST_WEEK  numberRecords = 69,042
  BE1 (Brussels)                      1,593     <- matches the adapters-fix slice exactly
  BE2 (Flanders)                     66,002
  BE3 (Wallonia)                      1,042
sum of the 54 partitions           = 80,040     <- 16 % over-count: a vacancy carries
                                                   more than one NACE section
requests to page every partition   =  1,625
```

Note the plan's acceptance figure of "~10,000 vacancy rows" is **conservative**: Belgium
actually published 69,042 vacancies to EURES in the last week. The ~10,000-row sweep is
a bounded slice of that, not its exhaustion.

### 4.2 Employer diversity decays with depth, exactly as the plan predicted

Measured on the two largest partitions, 12 pages each:

| after page | BE2/O employers | per request | BE2/L employers | per request |
|---|---|---|---|---|
| 1 | 15 | 15.0 | 20 | 20.0 |
| 4 | 40 | 10.0 | 44 | 11.0 |
| 8 | 72 | 9.0 | 48 | 6.0 |
| 12 | 86 | **7.2** | 58 | **4.8** |

Rows per request stay flat at 50. **Width buys employers; depth buys more vacancies from
employers already seen.** This is the measurement that justifies the wide-and-shallow
partitioning.

### 4.3 Honest extrapolation to the plan's acceptance test

Target: ~10,000 rows, 2,000–2,600 distinct employers, 0 LLM tokens.

- **Rows.** 10,000 ÷ 50 rows per request = **200 requests**.
  At the compliant `Crawl-delay: 10`, 200 × 10 s = **33 minutes** — which is the plan's
  own stated 33-minute figure, now independently confirmed by measurement.
- **Employers.** 10,000 rows spread over 54 partitions is ~185 rows each, i.e. pages 1–4.
  The measured employer ratio at page 4 was 40/200 (BE2/O) and 44/200 (BE2/L) — **20–22 %**.
  10,000 × 0.21 ≈ **2,100 distinct employers**, inside the plan's 2,000–2,600 band.
- **Tokens.** 0, measured — `board.eures` is in `DETERMINISTIC_ADAPTER_PREFIXES` and no
  LLM call occurs on this path.

**The acceptance test is reachable as specified.** I did not run the full 33 minutes; the
figures above are extrapolated from 54 + 24 + 4 live requests, and every rate they use was
measured, not assumed.

Exhausting all of Belgium last week (69,042 rows) would need 1,625 requests × 10 s =
**4.5 hours**, over NFR-103's four-hour window. The bounded ~10,000-row sweep is the one
that fits, which is what the plan specifies.

---

## 5. Measured retrieval — one real ATS board end to end

Greenhouse `gitlab`, through `collection.collection_worker`:

```
plan items:  1     requests: 1     wall clock: 11 s     records collected: 227
```

Rows landed with the full chain attached:

```
source_adapter  rows  companies  distinct_names  no_company  with_raw_document
ats.greenhouse   226          1               1           0                226

company row:  GitLab | greenhouse | gitlab | source = 'ats.greenhouse'
provenance:   227 vacancy rows + 1 company row, all citing 1 plan item
```

- **227 records → 226 vacancy rows.** Not a loss: GitLab genuinely posts
  *"Intermediate Support Engineer (SHIFT)"* twice at *Remote, United Kingdom*. The two
  records merged into one vacancy carrying **two** provenance rows. The dedup slice's
  C4/N6 fix is what keeps the other 226 distinct — on the pre-fix normaliser the same
  board collapsed to 6 rows.
- **0 rows without a company** — the DR-101 `(ats_vendor, ats_slug)` keying works.
- **Every row carries a `raw_document_id`** — FR-183 satisfied.
- The EURES campaigns each *also* re-read this board unprompted: the FR-181
  discovery → harvest chain saw a known board in the knowledge base and planned it.

---

## 6. Caching works

**Repeat fetch served from cache** — three fetches of GitLab's 3.6 MB board:

```
fetch #1: status=200 from_cache=True bytes=3,593,482 secs=0.01
fetch #2: status=200 from_cache=True bytes=3,593,482 secs=0.00
fetch #3: status=200 from_cache=True bytes=3,593,482 secs=0.00
stats: {'fetched': 0, 'cached': 3, 'errors': 0}
```

**Conditional GET / 304** — with `expires_at` forced into the past, the stored
`ETag: W/"c70c1ad3..."` goes back out and the origin answers 304:

```
revalidating fetch: status=200 from_cache=True revalidated=True bytes=3,593,482
savings: {'revalidated': 1, 'not_modified': 1, 'bytes_not_transferred': 3,593,482}
stats:   {'fetched': 1, 'cached': 0}
```

One request, no body across the wire, 3.59 MB saved.

**Caveat, measured:** the cache is **GET-only** (`client.py:783`). EURES searches are
POSTs, so **no EURES response is ever cached or revalidated** — `SELECT COUNT(*) FROM
http_cache WHERE url LIKE '%jv-search%'` is `0`, and the second identical sweep cost the
full 54 requests again. This is defensible HTTP semantics, but it means the thing that
stops a needless EURES re-run is the **fetch ledger**, not the HTTP cache. The ledger now
records those targets (§2.5); making the reuse *decision* read it is still open (§8).

---

## 7. Real counts, before and after

Straight from `data/dreamjob.db`.

| Query | Before | After |
|---|---|---|
| `SELECT COUNT(*) FROM vacancy WHERE source_adapter != 'e2e-fixture'` | **0** | **1,362** |
| `SELECT COUNT(*) FROM company WHERE source IS NOT NULL` | 45 | **502** |
| `SELECT COUNT(*) FROM provenance` | **0** | **6,619** |
| `SELECT COUNT(*) FROM fetch_ledger` | *(table did not exist)* | **55** |
| `SELECT COUNT(*) FROM raw_document` | 65 | 194 |
| `SELECT COUNT(*) FROM http_cache` | 23 | 25 |

**Before** — every one of 633 plan items, including the `done` ones, had
`records_collected = 0`, and the only vacancy rows in the product were 45 test fixtures:

```
board.eures          done   0   (11 items)
board.jobat          done   0   ( 8 items)
registry.kbo         done   0   ( 7 items)
website.crawl        done   0   ( 7 items)
...
source_adapter  count
e2e-fixture        45
```

**After** — `SELECT adapter_key, status, SUM(records_collected) FROM source_plan_item
GROUP BY adapter_key, status` for the two sources exercised:

```
adapter_key     status   records  items
ats.greenhouse  done       1,135      5
ats.greenhouse  failed         0      1     <- the pre-migration OperationalError
ats.greenhouse  planned        0     18     ┐ pre-existing rows from historic
ats.greenhouse  skipped        0     14     │ runs, never re-run
board.eures     done       5,027    182
board.eures     failed         0      9     <- the pre-fix run of §2.3; not re-run
board.eures     planned        0     18     ┘
board.eures     running        0      3
```

Vacancy rows by source:

```
source_adapter  rows  companies  no_company  with_raw
board.eures     1,136       456          38     1,136
ats.greenhouse    226         1           0       226
e2e-fixture        45        45           0         0
```

The final verification sweep reports cleanly: **54 of 54 partitions `done`, 0 errors.**

---

## 8. What still does not work, plainly

1. **No source other than EURES and Greenhouse has been proven end to end here.**
   The other 26 adapters are unit-tested and four of them (Workable, Teamtailor, Actiris,
   Arbeitnow) were verified live by their own slice, but I did not run them through the
   pipeline. Do not read "retrieval works" as "all 28 sources work".

2. **38 of 1,136 EURES rows (3.3 %) have no company.** All 38 have
   `company_name_raw IS NULL`: the EURES record itself carries no `employer.name`. The
   pipeline correctly declines to invent a company. This is a source data gap, not a bug,
   but those vacancies cannot be scored on employer attractiveness.

3. **EURES responses are never cached (POST).** Re-running a sweep costs every request
   again. See §6.

4. **The fetch ledger is written but not yet read for the reuse decision.**
   `assess_reuse` / `_pages_for` still count fresh rows per adapter. Until they call
   `target_is_fresh()` / `fresh_targets()`, a second campaign can still skip a
   never-read board because a hundred other rows of that vendor are fresh. The evidence
   now exists; the decision does not use it.

5. **`board_registry` (migration 092) is empty.** `scripts/import_board_registry.py
   --load-db` has never been run. Discovery reads the 4,511-board JSON file directly, so
   nothing is broken — but the liveness store the nightly refresh needs holds 0 rows.

6. **`.gitignore` line 24 is `data/`, which hides `backend/dreamjob/pipeline/data/`.**
   `board_registry.json` and five other runtime files the product reads cannot be
   committed; a fresh clone has none of them. One-line fix (`/data/`, or a negation),
   in a file no slice owned. **This is the second thing a product owner should fix.**

7. **104 leaked test plan-item rows remain in the installed database**
   (`spine.*`, `stub_board`, `broken_board`). The catalogue slice fixed the *catalogue*
   and stopped the leak at its source (`tests/unit/conftest.py`), but did not delete the
   historic `source_plan_item` rows. They are inert — no campaign references them — but
   they distort any global `GROUP BY adapter_key` until deleted.

8. **`detect.SUPPORTED_VENDORS` still omits `teamtailor` and `workable`.** This does not
   affect discovery (§2.6), but a website crawl or watchlist check that finds one of
   those boards cannot record it.

9. **The 9 pre-fix `failed` EURES rows and 1 pre-migration `failed` Greenhouse row were
   left as they are.** They are historical evidence of the defects in §2.1 and §2.3, not
   current behaviour; the final run of the same partitions is `done` with 0 errors.

10. **Nothing here proves NFR-103 at full scale.** The largest run was 54 plan items.
    A Campaign A plan is ~7,600 items; that has been estimated but never executed.

---

## 9. Reproducing this

```bash
# 1. bring the database up to date first — this is the one that matters
PYTHONPATH=backend python3 -c "from dreamjob.db.migrator import migrate; print(migrate())"

# 2. tests
PYTHONPATH=backend python3 -m pytest tests/unit -q      # 921 passed
python3 -m ruff check backend/dreamjob tests scripts    # clean

# 3. what the partitioner now produces (no network)
PYTHONPATH=backend python3 -c "
from dreamjob.adapters import load_all; load_all()
from dreamjob.pipeline import discovery
from dreamjob.adapters.jobboards.eures import EuresAdapter
print(discovery.reads_partitions(EuresAdapter))
print(len(discovery.eures_partitions(countries=['BE','NL'], caps={}, partitioned=True)))"
# -> True / 126
```

The live runs used a harness that builds `planning.PlannedSource` items, calls
`planning.persist_plan`, then `collection.collection_worker` — the product's own code
path throughout. It is in the session scratchpad at
`.../scratchpad/harness.py` and `.../scratchpad/measure.py`; neither is part of the
repository, because a proof harness is not a product feature. A pre-migration backup of
the database is at `.../scratchpad/dreamjob.pre090.db`.

All live traffic used the product's own `EgressClient`: its User-Agent
(`DreamJobBot/0.1 (+https://stepvda.net/dreamjob; contact stephane@stepvda.com)`),
robots.txt checked first, and — now — europa.eu's stated `Crawl-delay: 10` honoured.
No prohibited source was contacted.
