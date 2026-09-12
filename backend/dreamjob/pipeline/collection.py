"""Plan execution: the collection spine (FR-181..186, NFR-401, NFR-403, NFR-603).

Collection turns the persisted plan into knowledge-base rows.  It is the part
of the pipeline that runs longest and fails most often, so the design is built
around interruption rather than around the happy path:

* every page is one unit of work, and the job checkpoints after each one, so a
  crash or a cancel loses at most the page in flight (NFR-401);
* every page is *measured* - requests issued, raw documents returned, records
  parsed, normalised, written and refused - so that "this source found nothing"
  can be told apart from "this source was never asked", "every request was
  refused" and "the page parsed to nothing".  Only the first of those is
  ``done``; the rest are failures, and they used to be recorded as success
  (:class:`ItemOutcome`);
* the job is pausable, resumable and cancellable while it runs, and reports
  per-adapter progress and error counts (FR-185);
* four explicit caps - pages, companies, people and wall-clock duration - bound
  the whole run and are checked before every unit of work (FR-186).  The page
  budget is charged for requests that were actually issued and reserves a floor
  for every runnable source, so an exhausted budget shortens the run instead of
  deleting the sources at the end of the plan;
* the plan runs in stages - discover companies, deepen them, then harvest their
  ATS boards - and the boards discovered by a run are read by that same run
  (FR-181);
* every record is written through the knowledge-base writer, which links it to
  the plan item that produced it (FR-166) and de-duplicates it (FR-184);
* the extraction rate of each adapter is tracked and a collapse is flagged for
  the administrator within the campaign that saw it (NFR-403);
* an adapter that is missing, disabled or blocked by robots.txt settles its
  own plan item and nothing else (IR-101, FR-182);
* **an outcome is labelled for what it is.**  Counting every non-2xx as an
  error was the correction of a worse bug - a plan item that fetched nothing
  was recorded as ``done`` with 0 records and 0 errors, which hid a total
  retrieval failure - but it over-corrected: one campaign reported 538
  "errors" of which 308 were dead registry slugs answering 404, 192 were
  robots.txt refusals the product made on purpose, and 51 were bot walls.  A
  real failure buried among 500 expected outcomes is a failure nobody sees, so
  a plan item now ends in one of six measured states (:class:`ItemOutcome`):

  ``succeeded``  records were written;
  ``blocked``    we declined, correctly - robots.txt, a 403 bot wall, terms of
                 service.  A decision, and one the product can defend
                 (FR-182, IR-101, CR-402);
  ``gone``       the target is not there any more - 404 or 410 on a board the
                 registry offered.  The registry is told, so it learns and
                 stops offering it (FR-343, DR-101);
  ``failed``     something actually went wrong - 5xx, a transport error, a
                 rate limit, a parse crash, an adapter exception.  This is the
                 one the operator must see, and it is the only one that still
                 increments ``error_count``;
  ``skipped``    there was nothing to do;
  ``capped``     the FR-186 budget ended the run before this item finished.

  ``blocked`` and ``gone`` are counted in columns of their own (migration 130)
  rather than made quiet: "we declined 192 sources on principle" is
  information the operator wants, it is simply not an error.

Two properties of that design were missing and are added here (see
``docs/Data_Gathering_Plan.md`` sections 2.4 and 5.2, items N5 and N7):

* **plan items that share a rate limit run in sequence; the rest run at the
  same time.**  The loop used to be strictly serial and grouped by adapter, so
  the wall-clock of a campaign was the *sum* of its per-domain waits - 5.5 h for
  the campaign NFR-103 gives four hours to.  Work is now grouped into rate-limit
  buckets and one task drives each bucket, so the wall-clock is the longest
  bucket instead: 100 minutes for the same campaign.  Per-tenant ATS vendors
  (``*.recruitee.com``, ``*.jobs.personio.de``, ``*.myworkdayjobs.com``,
  ``*.teamtailor.com``) share one bucket each, because the vendor sees one
  client IP however many subdomains it has - Recruitee answered 429 to 11 of 56
  requests at concurrency 10.  Nothing here raises a rate: the per-domain limit
  stays where the egress layer sets it, and volume comes from more domains.
* **a refused request is a failure, and it is charged to the plan item that
  made it.**  Requests used to be attributed by diffing the egress client's
  global counters around ``adapter.run()``, which is only true while exactly one
  item runs at a time, and a board that answered 404 or 403 produced an empty
  record list indistinguishable from a board with no openings.  Each item now
  fetches through its own counting view of the shared client, so its requests
  and its refusals are its own (FR-185, NFR-403).

Stages register themselves here so any of them can be re-run from the persisted
artefacts of the previous one (NFR-603).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib
import inspect
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from dreamjob.adapters.base import PlanItem, adapter_unavailable_reason, get_adapter
from dreamjob.adapters.vacancy_source import SourceUnavailable
from dreamjob.config import get_settings
from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import campaigns as repo
from dreamjob.db.repositories import hygiene as hygiene_repo
from dreamjob.db.repositories import knowledge as kb_repo
from dreamjob.egress import client as egress_client
from dreamjob.egress.client import EgressClient, FetchResult, RobotsDisallowed
from dreamjob.jobs.runner import JobCancelled, JobContext, runner
from dreamjob.pipeline import board_registry, knowledge_base, planning
from dreamjob.pipeline import outcomes as outcome_rules


def _is_ats(adapter: Any) -> bool:
    """Is this an ATS adapter?  Answered by duck-typing so collection does not
    need to import the ATS module (which would be a circular import)."""
    return hasattr(adapter, "has_slug") or getattr(adapter, "source_type", None) == "ats"


def _has_ats_slug(adapter: Any, item: PlanItem) -> bool:
    """Does this plan item name a board to read?  Uses the adapter's own
    ``has_slug`` when present; otherwise assumes there is work to do."""
    checker = getattr(adapter, "has_slug", None)
    if callable(checker):
        return bool(checker(item))
    return True

log = logging.getLogger(__name__)

JOB_KIND = "collection"

# NFR-403: an adapter that extracts nothing from pages it used to parse has
# almost certainly been broken by a site change.
BREAKAGE_MIN_ATTEMPTS = 5
BREAKAGE_RATE = 0.5

# FR-183 / DR-102: how long a stored body outlives the last record that cites
# it.  ``DREAMJOB_RAW_DOCUMENT_RETENTION_DAYS`` overrides it; this is the
# default, and it is also the answer when the setting is not configured.
RAW_DOCUMENT_RETENTION_DAYS = 30
RAW_DOCUMENT_SWEEP_LIMIT = 2_000


# ---------------------------------------------------------------------------
# How collection competes for the process (NFR-102, CR-408, FR-185)
# ---------------------------------------------------------------------------
#
# Collection is the longest-running job in the product and the one that broke
# it: a real 2,686-item campaign left the backend unresponsive for eighteen
# hours.  ``jobs/runner.py`` answers *where* a job runs - every job now has a
# thread and an event loop of its own, so no job body can starve the loop
# uvicorn serves HTTP from.  That fixes the mechanism.  It says nothing about
# the two things this module is answerable for:
#
# * **how much of the single writer collection takes.**  SQLite has one writer
#   and ``db/connection.py`` queues for it in process (CR-408, NFR-102); every
#   transaction collection opens is one an interactive request may have to get
#   past.  A campaign of 2,686 single-page items used to open 8,058
#   transactions on ``source_plan_item`` and 5,379 on ``job_run`` - three and
#   two per item - to record numbers that are read once, by a screen.
# * **how long it goes without offering to stop.**  ``checkpoint_barrier()``
#   is where pause and cancel are honoured (FR-185).  A barrier that is
#   reached once per *page* is bounded by whatever the page happened to
#   contain: sixty postings through the knowledge-base writer is a hundred-odd
#   transactions and, on a knowledge base of any size, seconds of them.  The
#   intervals below are counts of work, because a bound that depends on how
#   big a page turned out to be is not a bound.
#
# What is *not* coalesced is the checkpoint.  Its cadence is a requirement and
# not a preference - NFR-401 says a crash loses at most the page in flight,
# and that is only true if it is written after every page - so it stays
# exactly where it was.

#: Knowledge-base records written between two checkpoint barriers.  Small
#: pages are written in one call, exactly as before; only a page big enough to
#: hold the loop for a noticeable time is broken up.
WRITE_BATCH = 25

#: Plan items prepared, and plan items settled, between two barriers.  Both
#: loops run over the whole plan - 2,686 items each - with no I/O to yield on
#: of their own, which is how a pause could go unanswered for minutes at the
#: start and at the end of a run.
PREPARE_BATCH = 64
SETTLE_BATCH = 32

#: How often the derived counters are written.  ``progress_done`` is a bar on
#: a screen and ``records_collected`` is a number beside a source name: nobody
#: needs 2,686 rows of either, they need the number to be right when they look
#: at it, and at the end.  Both are flushed at every point where the run can
#: end, so what is written is never stale by more than this.
PROGRESS_INTERVAL_SECONDS = 1.0
COUNTER_INTERVAL_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Rate-limit buckets (FR-182, NFR-103; plan sections 2.2 and 5.2 item N5)
# ---------------------------------------------------------------------------

#: Vendors that give every customer its own host.  The egress limiter is keyed
#: by netloc, so it never waits between two tenants of the same vendor - but the
#: vendor sees one client IP whatever the subdomain is, and Recruitee answered
#: 429 to 11 of 56 requests spread over 10 tenants.  One bucket per vendor keeps
#: its boards strictly sequential at the product's ordinary per-domain rate.
VENDOR_BUCKETS: dict[str, str] = {
    "recruitee": "*.recruitee.com",
    "personio": "*.jobs.personio.de",
    "workday": "*.myworkdayjobs.com",
    "teamtailor": "*.teamtailor.com",
}

#: The same grouping, reached from a host rather than from an adapter's vendor.
_TENANT_HOST_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("recruitee.com", "*.recruitee.com"),
    ("jobs.personio.de", "*.jobs.personio.de"),
    ("jobs.personio.com", "*.jobs.personio.de"),
    ("myworkdayjobs.com", "*.myworkdayjobs.com"),
    ("teamtailor.com", "*.teamtailor.com"),
)

# Where a plan item's host can be read from without issuing a request: the
# query the planner wrote, then the URL template the adapter is built around.
_QUERY_URL_KEYS = ("url", "base_url", "api_base", "endpoint", "seed_url", "sitemap_url")
_QUERY_URL_LISTS = ("urls", "crawl_seeds", "seeds", "sitemaps")
_MODULE_URL_NAMES = (
    "API", "API_BASE", "BASE", "BASE_URL", "DEFAULT_API_BASE", "FEED", "LISTING",
    "SEARCH", "SITEMAP", "PUBLIC", "PUBLIC_DETAILS", "DETAIL_API", "URL",
)


def _netloc(value: Any) -> str:
    """The host of a URL or of a URL template, lower-cased.

    A template names a tenant (``{slug}.recruitee.com``); the part that is
    stable across tenants is the suffix, and it is the suffix the vendor rate
    limits, so that is what is returned.
    """
    if not isinstance(value, str) or not value.startswith("http"):
        return ""
    host = urlparse(value).netloc.lower()
    if "{" not in host:
        return host
    tail = host.split(".", 1)[1] if "." in host else ""
    return tail if tail and "{" not in tail else ""


def _host_of(item: dict, adapter: Any) -> str:
    """The host this plan item will talk to, as far as it can be known."""
    query = item.get("native_query")
    if isinstance(query, dict):
        for key in _QUERY_URL_KEYS:
            host = _netloc(query.get(key))
            if host:
                return host
        for key in _QUERY_URL_LISTS:
            values = query.get(key)
            if isinstance(values, (list, tuple)) and values:
                host = _netloc(values[0])
                if host:
                    return host
    module = sys.modules.get(type(adapter).__module__)
    for name in _MODULE_URL_NAMES:
        host = _netloc(getattr(adapter, name, None)) or _netloc(getattr(module, name, None))
        if host:
            return host
    return ""


def _bucket_for(item: dict, adapter: Any) -> str:
    """Which rate-limit bucket this plan item belongs to (FR-182).

    Items in one bucket run in sequence because they share a limit; items in
    different buckets run at the same time because they do not.  The answer is
    the vendor group for a per-tenant ATS, the host otherwise, and the adapter
    key when the host cannot be known without fetching - which is the
    conservative answer, because it keeps that adapter as serial as it is today.
    """
    hint = getattr(adapter, "rate_limit_bucket", None)
    if isinstance(hint, str) and hint:
        return hint
    if callable(hint):
        try:
            named = str(hint(item) or "")
        except Exception:  # noqa: BLE001 - a bad hint must not stop collection
            log.warning("[%s] rate_limit_bucket() failed; bucketing by adapter",
                        item.get("adapter_key"), exc_info=True)
            named = ""
        if named:
            return named
    vendor = str(getattr(adapter, "vendor", "") or "").strip().lower()
    if vendor in VENDOR_BUCKETS:
        return VENDOR_BUCKETS[vendor]
    host = _host_of(item, adapter)
    if host:
        for suffix, bucket in _TENANT_HOST_SUFFIXES:
            if host == suffix or host.endswith("." + suffix):
                return bucket
        return host
    return f"adapter:{item.get('adapter_key') or getattr(adapter, 'key', '')}"


# ---------------------------------------------------------------------------
# What an answer means (FR-182, FR-185, IR-101, NFR-403)
# ---------------------------------------------------------------------------

#: Answers in which the other side declined us, or we declined it.  A bot wall
#: (403) and a legal refusal (451) are the HTTP spelling of the decision
#: robots.txt states in words: the source does not want to be read this way.
#: Neither is a defect in this product, and neither is retried by paging on -
#: but both are recorded, because "we declined 192 sources on principle" is
#: exactly the kind of thing the operator has to be able to defend (FR-182,
#: IR-101, CR-402).
#:
#: 401 is deliberately absent: a credential this installation was supposed to
#: hold and does not is our own defect, and it must stay loud.  So is 429 and
#: so is 503 - "come back later" is a rate the product is exceeding, which is
#: the operator's problem to see, not a decision to record and forget.
BLOCKED_STATUSES: frozenset[int] = frozenset({403, 451})

#: Answers in which the target is not there any more.  On a board the registry
#: offered, this is the measured cost of a harvested registry, not a failure:
#: liveness of a Wayback-only slug was measured at 27.5% (board_registry, N3).
#: The registry learns it once and stops offering it (FR-343, DR-101).
GONE_STATUSES: frozenset[int] = frozenset({404, 410})


@dataclass
class Refusal:
    """One request that was turned down, with the evidence to classify it."""

    url: str
    #: The HTTP status, or ``None`` when no request was made at all because
    #: robots.txt disallowed it (:class:`RobotsDisallowed`, FR-182).
    status: int | None
    robots: bool = False

    @property
    def detail(self) -> str:
        if self.robots:
            return f"{self.url}: robots.txt disallows this source (FR-182)"
        return f"{self.url}: HTTP {self.status}"

    def kind(self, *, board: bool) -> str:
        """``blocked``, ``gone`` or ``failed`` - the three ways to be refused.

        ``board`` says whether this plan item reads one named ATS board, which
        is the only target a 404 can mean *gone* for.  A search endpoint that
        answers 404 has moved and the adapter is now wrong about it; that is a
        breakage and it stays a failure.
        """
        if self.robots or (self.status in BLOCKED_STATUSES):
            return "blocked"
        if board and self.status in GONE_STATUSES:
            return "gone"
        return "failed"


class _UnitEgress:
    """One plan item's view of the shared egress client (FR-183, FR-185).

    It is a view and not a client: the rate limiter, the robots.txt cache, the
    connection pool and the concurrency semaphore all stay shared, because they
    are what keeps the product inside its own limits (FR-182).  What is *not*
    shared is the accounting.  Requests used to be attributed to a plan item by
    diffing the client's global counters around ``adapter.run()``; that is only
    true while exactly one item runs at a time, and it silently charged one
    board's requests to another as soon as two ran together.

    It also records the HTTP status of every refusal.  ``EgressClient.fetch``
    returns a 404 rather than raising, so an adapter that quietly returns no
    records from a dead endpoint used to be indistinguishable from a source with
    nothing to offer - the "done, 0 records, 0 errors" row (NFR-403).
    """

    def __init__(self, client: EgressClient):
        self._client = client
        self.requests = 0
        #: Every request that was turned down, as evidence rather than as
        #: prose: the status is what tells a bot wall from a dead board from a
        #: server that fell over, and a string cannot be classified twice.
        self.refusals: list[Refusal] = []
        # The last request this item made, kept for the fetch ledger (N4): the
        # ledger answers "was this target read recently", so it needs the URL
        # and validators of the read, not just how many there were.
        self.last_url: str | None = None
        self.last_status: int | None = None
        self.last_etag: str | None = None
        self.last_modified: str | None = None

    def __getattr__(self, name: str) -> Any:
        # Everything not about accounting belongs to the shared client.
        return getattr(self._client, name)

    async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        self.requests += 1
        try:
            result = await self._client.fetch(url, **kwargs)
        except RobotsDisallowed:
            # RobotsUnavailable subclasses this: a robots.txt that could not be
            # read is a refusal too, and by the same decision (FR-182).
            self.refusals.append(Refusal(url=url, status=None, robots=True))
            raise
        if not result.ok:
            self.refusals.append(Refusal(url=url, status=result.status_code))
        self.last_url = result.url or url
        self.last_status = result.status_code
        headers = result.headers or {}
        self.last_etag = headers.get("etag") or headers.get("ETag")
        self.last_modified = headers.get("last-modified") or headers.get("Last-Modified")
        return result

    async def fetch_json(self, url: str, **kwargs: Any) -> object:
        result = await self.fetch(url, **kwargs)
        return json.loads(result.text)

    async def fetch_many(self, urls: list[str], **kwargs: Any) -> list[FetchResult | Exception]:
        tasks = [self.fetch(u, **kwargs) for u in urls]
        return await asyncio.gather(*tasks, return_exceptions=True)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Caps (FR-186)
# ---------------------------------------------------------------------------


@dataclass
class Caps:
    """Hard bounds on one collection run, derived from the plan and editable."""

    max_pages: int = planning.DEFAULT_CAPS["max_pages"]
    max_pages_per_source: int = planning.DEFAULT_CAPS["max_pages_per_source"]
    max_companies: int = planning.DEFAULT_CAPS["max_companies"]
    max_people: int = planning.DEFAULT_CAPS["max_people"]
    max_duration_seconds: int = planning.DEFAULT_CAPS["max_duration_seconds"]

    @classmethod
    def from_campaign(cls, campaign: dict) -> Caps:
        caps = dict(planning.DEFAULT_CAPS)
        stored = campaign.get("caps") or {}
        if isinstance(stored, dict):
            for key, value in stored.items():
                if key in caps and isinstance(value, (int, float)):
                    caps[key] = int(value)
        return cls(**{k: caps[k] for k in caps if k in cls.__annotations__})

    def to_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass
class CollectionStats:
    pages: int = 0
    #: Planned pages completed, which is what the progress bar counts.  Kept
    #: apart from ``pages`` because that is the *budget*, charged in requests
    #: (FR-186), and one planned page may cost several of them - a partitioned
    #: search, a website crawl.  Using the budget as the numerator is what let
    #: a real job report ``progress_done=244 / progress_total=85``: the
    #: denominator counted planned pages while the numerator counted requests.
    pages_done: int = 0
    records: int = 0
    #: Things that actually went wrong (5xx, transport, crashes).  Refusals we
    #: made on principle and targets that are gone are counted next door, so
    #: this number is the one an operator can act on (FR-185).
    errors: int = 0
    #: Requests declined on principle: robots.txt, a bot wall, terms of
    #: service (FR-182, IR-101).
    blocked: int = 0
    #: Requests to a target that no longer exists (404/410 on a board).
    gone: int = 0
    companies: set[str] = field(default_factory=set)
    people: set[str] = field(default_factory=set)
    by_adapter: dict[str, dict[str, int]] = field(default_factory=dict)
    stopped_by: str | None = None
    #: FR-186: pages charged per collection stage, which is what a stage's
    #: reservation is spent against.
    pages_by_stage: dict[int, int] = field(default_factory=dict)
    #: ``(stage, ceiling)`` of the wave running now, or ``None`` between waves.
    #: Reaching the ceiling ends that stage, not the run.
    stage_limit: tuple[int, int] | None = None

    def adapter(self, key: str) -> dict[str, int]:
        bucket = self.by_adapter.setdefault(
            key, {"pages": 0, "records": 0, "errors": 0, "blocked": 0, "gone": 0}
        )
        # A bucket restored from a checkpoint written before migration 130 has
        # neither key, and a missing key here is a KeyError in the page loop.
        bucket.setdefault("blocked", 0)
        bucket.setdefault("gone", 0)
        return bucket

    def charge_stage(self, stage: int, pages: int) -> None:
        self.pages_by_stage[stage] = self.pages_by_stage.get(stage, 0) + pages

    def to_dict(self) -> dict:
        return {
            "pages": self.pages,
            "pages_done": self.pages_done,
            "records": self.records,
            "errors": self.errors,
            # NFR-401: the checkpoint carries these, so a resumed job reports
            # the same totals as an uninterrupted one.
            "blocked": self.blocked,
            "gone": self.gone,
            "companies": len(self.companies),
            "people": len(self.people),
            "by_adapter": self.by_adapter,
            "stopped_by": self.stopped_by,
            "pages_by_stage": {str(k): v for k, v in self.pages_by_stage.items()},
        }

    def restore(self, saved: dict | None) -> None:
        """Continue an interrupted run's counters instead of restarting them.

        A resumed job re-runs at most the page that was in flight, so every
        record, error, refusal and dead target the previous attempt counted is
        work this attempt will not do again.  Leaving them at zero made a
        resumed campaign report fewer records and fewer failures than the same
        campaign run straight through, which is the same class of lie as
        ``done, 0 records, 0 errors`` (NFR-401, FR-185).
        """
        if not isinstance(saved, dict):
            return
        for name in ("records", "errors", "blocked", "gone"):
            value = saved.get(name)
            if isinstance(value, (int, float)):
                setattr(self, name, int(value))
        by_adapter = saved.get("by_adapter")
        if isinstance(by_adapter, dict):
            for key, counts in by_adapter.items():
                if not isinstance(counts, dict):
                    continue
                bucket = self.adapter(str(key))
                for name, value in counts.items():
                    if isinstance(value, (int, float)):
                        bucket[name] = bucket.get(name, 0) + int(value)


# FR-186: how the page budget is reserved across the collection stages.
#
# Weights, not fractions: what a stage does not spend is divided among the
# stages that still have work, in proportion, so a reservation never wastes
# budget.  Before this existed the budget was first-come-first-served in stage
# order and the floor pass gave every *unit* one page, so a plan with more
# discovery items than pages spent the entire budget before the harvest stage
# was reached: one real run settled 4,429 planned ATS boards as
# "not started: max_pages" without issuing a single request to any of them.
# The harvest stage is the only route to a company's own board, so it is
# reserved as much of the budget as discovery.
STAGE_RESERVE: dict[int, int] = {
    repo.STAGE_DISCOVER: 4,
    repo.STAGE_DEEPEN: 2,
    repo.STAGE_HARVEST: 4,
}
DEFAULT_STAGE_RESERVE = 1


def _stage_allowance(
    stage: int, pending_stages: set[int], stats: CollectionStats, caps: Caps
) -> int:
    """How many more pages this stage may charge in the wave about to run.

    Computed when the wave starts, from what is actually left and which stages
    still have work, so an under-spending stage hands its remainder on rather
    than reserving it against nothing.
    """
    remaining = max(0, caps.max_pages - stats.pages)
    if remaining <= 0:
        return 0
    weights = sum(STAGE_RESERVE.get(s, DEFAULT_STAGE_RESERVE) for s in pending_stages)
    if weights <= 0:
        return remaining
    share = STAGE_RESERVE.get(stage, DEFAULT_STAGE_RESERVE) / weights
    # A stage always gets at least one page: a reservation that rounded to zero
    # would settle the whole stage "never started" for arithmetic reasons.
    return max(1, min(remaining, int(remaining * share)))


def _stage_exhausted(stats: CollectionStats) -> bool:
    """Has the wave now running spent its stage's reservation (FR-186)?

    Distinct from :func:`_cap_hit`: this ends a stage and lets the run continue
    to the next one, where a cap ends the run.
    """
    if stats.stage_limit is None:
        return False
    stage, ceiling = stats.stage_limit
    return stats.pages_by_stage.get(stage, 0) >= ceiling


def _cap_hit(stats: CollectionStats, caps: Caps, started: float) -> str | None:
    if stats.pages >= caps.max_pages:
        return "max_pages"
    if len(stats.companies) >= caps.max_companies:
        return "max_companies"
    if len(stats.people) >= caps.max_people:
        return "max_people"
    if time.monotonic() - started >= caps.max_duration_seconds:
        return "max_duration_seconds"
    return None


# ---------------------------------------------------------------------------
# Worker (FR-181, FR-185, NFR-401)
# ---------------------------------------------------------------------------


def administrator_caps(adapter_key: str) -> dict[str, Any]:
    """Per-source caps the administrator has set (FR-363).

    Read through the repository rather than the admin router, which would
    invert the layering.  These bound the campaign rather than the other way
    round: an operator who caps a source has capped it, and a campaign asking
    for more does not get more.
    """
    try:
        from dreamjob.db.repositories import admin as admin_repo  # noqa: PLC0415

        return admin_repo.get_setting(f"source.{adapter_key}.caps", {}) or {}
    except Exception:  # noqa: BLE001 - configuration must never stop collection
        log.exception("Could not read administrator caps for %s", adapter_key)
        return {}


def _pages_for(
    item: dict, caps: Caps, capabilities: dict, admin_cache: dict[str, dict] | None = None
) -> int:
    """This plan item's page budget (FR-186).

    ``admin_cache`` memoises the per-source administrator caps for one run:
    the lookup reads ``app_setting`` and used to be repeated for every item on
    every call, twice per run.
    """
    pages = int(item.get("estimated_pages") or 1)
    if not capabilities.get("pagination", True):
        pages = 1
    item_caps = item.get("caps") or {}
    if isinstance(item_caps, dict) and item_caps.get("max_pages"):
        pages = min(pages, int(item_caps["max_pages"]))
    key = item.get("adapter_key") or ""
    if admin_cache is None:
        admin = administrator_caps(key)
    elif key in admin_cache:
        admin = admin_cache[key]
    else:
        # ``dict.setdefault(key, administrator_caps(key))`` evaluates the read
        # every call, so the promised one lookup per run became one per item.
        admin = administrator_caps(key)
        admin_cache[key] = admin
    admin_max = admin.get("max_pages")
    if admin_max:
        pages = min(pages, int(admin_max))
    return max(1, min(pages, caps.max_pages_per_source))


def _load_adapter(adapter_key: str, egress: EgressClient) -> Any:
    """Adapters are optional at runtime: a missing one skips its plan item only."""
    try:
        return get_adapter(adapter_key, egress)
    except KeyError:
        log.warning("No adapter registered for %r; its plan item is skipped", adapter_key)
        return None
    except Exception:  # noqa: BLE001 - a broken adapter must not stop the campaign
        log.exception("Adapter %r could not be constructed", adapter_key)
        return None


# ---------------------------------------------------------------------------
# What a plan item actually did (FR-185, NFR-403)
# ---------------------------------------------------------------------------


@dataclass
class ItemOutcome:
    """The measured result of running one plan item.

    Collection used to observe one thing - the list ``adapter.run()`` returned -
    so "issued no request", "every request was refused", "parsed a good page and
    found nothing" and "this source genuinely has no matching vacancies" were
    the same empty list, and all four were written down as ``done``.  Counting
    the steps separately is what makes those four different answers again.
    """

    requests: int = 0            # HTTP calls the egress layer made for this item
    attempted: int = 0           # raw documents handed to the extractor
    parsed: int = 0              # records the extractor found in them
    normalised: int = 0          # records mapped onto knowledge-base columns
    created: int = 0
    updated: int = 0
    dropped: int = 0             # records the knowledge-base writer refused
    pages: int = 0               # pages that issued a request or returned material
    productive_pages: int = 0    # ... of which yielded at least one parsed record
    charged: int = 0             # what this item cost the run's page budget (FR-186)
    #: Pages that actually went wrong.  One per failed page, which is what
    #: ``source_plan_item.error_count`` counts and what an operator is asked to
    #: look at.  A refusal we made on principle and a target that is gone are
    #: *not* errors and never land here.
    errors: int = 0
    refused: int = 0             # every request the server or robots.txt turned down
    #: ... of which: declined on principle (robots.txt, 403, 451) and dead
    #: (404/410 on a board).  Both are counted per request, and both are
    #: outcomes rather than failures (FR-182, IR-101, FR-343).
    blocked: int = 0
    gone: int = 0
    #: ... and the rest: requests that failed, plus one for a page that failed
    #: without issuing one (a crash).  This is ``failed_count`` in the plan row
    #: - the same events as ``errors``, counted per request instead of per page.
    failed: int = 0
    #: Answers in which the source itself stated it holds nothing for this query.
    stated_empty: int = 0
    robots_blocked: int = 0      # ... of ``blocked``, refused by robots.txt (FR-182)
    #: Why we were declined, and what was gone: the evidence a ``blocked`` or a
    #: ``gone`` verdict has to be able to show (NFR-402).
    block_reason: str | None = None
    gone_reason: str | None = None
    #: The statuses the gone and failed verdicts rest on, which is what the
    #: board registry is told: 404/410 retires a board, a 5xx never can.
    gone_status: int | None = None
    failed_status: int | None = None
    last_error: str | None = None

    @property
    def written(self) -> int:
        return self.created + self.updated

    @property
    def failed_requests(self) -> int:
        """Refusals that were neither a decision nor a dead target.

        Derived rather than stored: every refusal is classified exactly once,
        so the three buckets always add up to ``refused`` and no answer can be
        counted twice or dropped between them.
        """
        return max(0, self.refused - self.blocked - self.gone)

    @property
    def page_failures(self) -> int:
        """What this page cost in real failures, at request granularity.

        A page that issued three requests and had all three answered 500 failed
        three times; a page that crashed without issuing one still failed once.
        ``max`` rather than a sum, so a 500 that then raised is one failure and
        not two.
        """
        return max(self.errors, self.failed_requests)

    @property
    def extraction_rate(self) -> float | None:
        """NFR-403: pages that yielded a record over pages that were fetched.

        Measured at the fetch boundary, not inside the parser, so an adapter
        that returns nothing at all is visible.  The old rate was computed per
        raw record, so an adapter with zero raw records had no rate - the
        detector could only see sources that were already working.

        A page whose answer *stated* that the source holds nothing is not an
        extraction attempt and is left out of the denominator: the parser was
        never given anything to extract.  Without this the plan item reads
        "done, nothing to collect" while the same page pins the catalogue's
        rolling rate at zero and fires the breakage audit - which is how
        ``registry.kbo`` came to sit at 0.0001 with 303 breakage events, and
        ``board.eures`` at 0.05 with 168, both of them working.
        """
        attempted = self.pages - min(self.stated_empty, self.pages)
        if attempted <= 0:
            return None
        return self.productive_pages / attempted

    def state(self) -> str:
        """The one word this item ended in, never folded into "done".

        Order is the whole point.  ``failed`` is tested before ``blocked`` and
        ``gone`` so a real failure on a source that was also refused somewhere
        stays loud: the expected outcomes must never be able to swallow one.
        ``blocked`` precedes ``gone`` because robots.txt refuses the *source*
        while a 404 refuses one target, and the wider statement is the truer
        label for the item.
        """
        if self.written:
            return "succeeded"
        if self.errors:
            return "failed"
        if self.blocked:
            return "blocked"
        if self.gone:
            return "gone"
        if self.dropped:
            return "rejected"
        if self.parsed or self.normalised:
            return "normalised_nothing"
        if self.stated_empty:
            # The source answered, and said it holds nothing for this query.
            # A partitioned sweep asks narrow questions on purpose and some have
            # no answer; reporting those as breakages made 9 of 54 EURES
            # partitions read as "the source layout has probably changed" while
            # the register was simply empty for them.
            return "no_matches"
        if self.pages or self.requests:
            return "extracted_nothing"
        return "no_work"

    def to_dict(self) -> dict:
        return {
            "state": self.state(),
            "requests": self.requests,
            "refused": self.refused,
            "raw_documents": self.attempted,
            "parsed": self.parsed,
            "normalised": self.normalised,
            "created": self.created,
            "updated": self.updated,
            "dropped": self.dropped,
            "pages": self.pages,
            "charged_pages": self.charged,
            # FR-185: the three ways a request can be turned down, kept apart.
            # ``refused`` stays their total, so nothing that read it before
            # migration 130 reads a smaller number now.
            "blocked": self.blocked,
            "gone": self.gone,
            "failed": self.failed,
            "blocked_by_robots": bool(self.robots_blocked),
            "block_reason": self.block_reason,
            "gone_reason": self.gone_reason,
            "stated_empty": self.stated_empty,
            "extraction_rate": self.extraction_rate,
        }


# How each measured state is written into ``source_plan_item.status``.  Only
# "succeeded" is ``done``: a source that fetched nothing, was refused, or
# produced records the knowledge base would not take has not done its job, and
# recording that as ``done`` is what hid this failure for months.
#
# ``blocked`` and ``gone`` are statuses of their own rather than either ``done``
# or ``failed``.  Both are terminal - neither is retried, because neither answer
# changes by asking again this week - and both must be countable apart from the
# failures, which is the whole point: 500 expected outcomes filed as errors is
# how the next real failure gets missed.
_STATE_STATUS: dict[str, str] = {
    "succeeded": "done",
    "failed": "failed",
    "blocked": "blocked",
    "gone": "gone",
    "rejected": "failed",
    "normalised_nothing": "failed",
    "extracted_nothing": "failed",
    # The source was read successfully and holds nothing for this query.  That
    # is a completed unit of work, not a failure - but it still says so in
    # words, so an empty partition is never mistaken for a collected one.
    "no_matches": "done",
    "no_work": "skipped",
}

#: The states that are not failures.  Everything else charges the plan item an
#: error, so a new state cannot be added by accident and stay silent.
NON_FAILURE_STATES: frozenset[str] = frozenset(
    {"succeeded", "no_matches", "blocked", "gone", "skipped", "capped"}
)

#: Plan-item statuses that will not be run again by a later campaign, and so
#: carry no remaining time.  ``blocked`` and ``gone`` are terminal for the same
#: reason ``done`` is: asking again this week cannot change the answer.
TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "skipped", "blocked", "gone"})


#: What a source *holds*, keyed on ``SourceAdapter.source_type``.  A register
#: holds records and filings, not vacancies, and telling a job seeker that the
#: Belgian enterprise register "holds no vacancy" for a company is not what
#: happened.  ``api.routers.campaigns._RECORD_NOUN`` names the same things for
#: the feed; this one is the pipeline's, so the pipeline does not import a
#: router.
_HELD_NOUN: dict[str, str] = {
    "ats": "vacancy",
    "job_board": "vacancy",
    "registry": "record",
    "directory": "company",
    "website": "page",
    "news": "article",
    "compensation": "pay benchmark",
    "events": "event",
    "linkedin": "profile",
}


def _state_message(outcome: ItemOutcome, *, holds: str = "record") -> str | None:
    state = outcome.state()
    if state == "succeeded":
        # A source that collected records and was refused some of what it asked
        # for has not fully succeeded, and the refusal outlives the run that saw
        # it rather than being cleared with the previous run's errors (FR-185).
        return outcome.last_error if outcome.refused else None
    if state == "failed":
        return outcome.last_error
    if state == "blocked":
        # Not an error, and phrased as what it is: a decision this product has
        # to be able to defend, with the evidence it rests on (FR-182, IR-101).
        return (
            f"declined on principle: {outcome.block_reason or 'no reason recorded'} - "
            f"{outcome.blocked} request(s) refused, none collected"
        )
    if state == "gone":
        return (
            f"the target is gone: {outcome.gone_reason or 'no reason recorded'} - "
            "the board registry has been told, so it stops offering it (FR-343)"
        )
    if state == "rejected":
        return (
            f"the knowledge base refused all {outcome.dropped} record(s) this source produced: "
            f"{outcome.last_error or 'no reason recorded'}"
        )
    if state == "normalised_nothing":
        return (
            f"parsed {outcome.parsed} record(s) but none could be normalised onto the "
            "knowledge-base schema"
        )
    if state == "extracted_nothing":
        return (
            f"fetched {outcome.pages} page(s) and extracted no record - the source layout "
            "has probably changed (NFR-403)"
        )
    if state == "no_matches":
        return (
            f"the source answered {outcome.stated_empty} request(s) and stated it holds no "
            f"{holds} for this query: read successfully, nothing to collect"
        )
    return "the source issued no request for this query: nothing to collect"


@dataclass
class _Unit:
    """One plan item, ready to run, with its own budget and outcome."""

    item: dict
    adapter: Any
    writer: Any
    pages: int
    egress: Any = None           # this item's counting view of the shared client
    bucket: str = ""             # the rate limit it shares with other items
    board: tuple[str, str] | None = None   # (ats_vendor, ats_slug) when it reads one
    done: int = 0
    stage: int = 1
    outcome: ItemOutcome = field(default_factory=ItemOutcome)
    started: bool = False
    running: bool = False        # inside its page loop right now (NFR-401)
    #: Its verdict has been written (:func:`_settle`).  A run can now be
    #: cancelled *between* the last page and the settle pass - the settle pass
    #: yields, because it is thousands of writes long - so "it ran and nothing
    #: has been written down about it yet" is a state the cancel handler has to
    #: be able to see.
    settled: bool = False
    #: Companies this item collected for, whether it created them or found them.
    company_ids: set[str] = field(default_factory=set)
    #: The board's company, resolved once per item rather than once per posting.
    board_company_id: str | None = None
    board_company_resolved: bool = False
    #: The activity kind last written onto this row (FR-361).  ``_settle`` reads
    #: it to tell a verdict that merely confirms the live stamp from one that
    #: corrects it, so the feed carries the correction and not the repetition.
    activity_kind: str | None = None

    @property
    def id(self) -> str:
        return str(self.item["id"])

    @property
    def adapter_key(self) -> str:
        return str(self.item["adapter_key"])

    @property
    def holds(self) -> str:
        """The noun this source's answers are counted in (see ``_HELD_NOUN``)."""
        source_type = getattr(self.adapter, "source_type", None)
        return _HELD_NOUN.get(str(getattr(source_type, "value", source_type) or ""), "record")

    @property
    def remaining(self) -> int:
        return max(0, self.pages - self.done)


def _stated_empty(adapter: Any) -> int:
    """How many answers this page got in which the source stated it holds nothing.

    Read off the adapter rather than passed back, because the four-step contract
    only returns records: ``SourceAdapter.record_stated_empty`` is the one way
    an adapter can say "answered, and the answer was no" without inventing a
    record to say it with (FR-181).

    ``max`` of the two places it can be found, never a sum: the base counter is
    the general one and ``FetchOutcome.stated_empty`` is the vacancy adapters'
    own copy of it, kept equal by ``VacancySourceAdapter.record_stated_empty``.
    A source that only knows the older of the two - a stub in a test, an adapter
    written before this counter moved down to the base class - still counts.
    """
    return max(
        int(getattr(adapter, "stated_empty", 0) or 0),
        int(getattr(getattr(adapter, "fetch_outcome", None), "stated_empty", 0) or 0),
    )


def _record_extraction(unit: _Unit, campaign_id: str) -> float | None:
    """NFR-403: keep the rolling extraction rate and flag a collapse.

    The rate is measured over *pages that were fetched*, not over raw records
    that were parsed.  An adapter whose ``fetch()`` returns nothing produced no
    raw record, so the old measure gave it no rate at all and the detector could
    only ever see sources that were already working - which is why every row of
    ``source_catalogue`` still had a NULL extraction rate after months of runs.
    """
    outcome = unit.outcome
    rate = outcome.extraction_rate
    if rate is None and not outcome.written:
        # This item fetched no page and wrote no record, so it is not evidence
        # about the adapter's extraction rate either way - and a campaign that
        # ends on its page budget leaves thousands of such items (one real run
        # left 4,664).  Recording "nothing to report" for each of them was a
        # read and a write on one ``source_catalogue`` row per plan item that
        # changed only its ``updated_at`` (CR-408, NFR-102).
        return None
    repo.record_extraction_rate(unit.adapter_key, rate, had_success=bool(outcome.written))
    if rate is None or rate >= BREAKAGE_RATE:
        return rate
    # A source that fetched a page and extracted nothing is broken now; there is
    # nothing to be gained from waiting for five such pages before saying so.
    if outcome.pages < BREAKAGE_MIN_ATTEMPTS and rate > 0:
        return rate
    message = (
        f"extraction rate {rate:.0%} over {outcome.pages} fetched page(s) - "
        "the source layout has probably changed (NFR-403)"
    )
    repo.bump_plan_item(unit.id, last_error=message)
    repo.record_audit(
        "adapter.breakage_suspected",
        entity_type="source_catalogue",
        entity_id=unit.adapter_key,
        detail={"campaign_id": campaign_id, "rate": rate, "pages": outcome.pages},
    )
    log.warning("Adapter %s: %s", unit.adapter_key, message)
    return rate


# ---------------------------------------------------------------------------
# The company a board's postings belong to (FR-166, FR-184, DR-101)
# ---------------------------------------------------------------------------


def _record_entity(record: Any) -> str:
    if isinstance(record, dict):
        return str(record.get("entity_type") or "")
    return str(getattr(record, "entity_type", "") or "")


def _record_data(record: Any) -> dict:
    data = record.get("data") if isinstance(record, dict) else getattr(record, "data", None)
    return data if isinstance(data, dict) else {}


def _board_of(item: dict, adapter: Any) -> tuple[str, str] | None:
    """The ``(ats_vendor, ats_slug)`` this plan item reads, if it reads one board.

    An item that names several boards is left alone: its postings would all be
    attached to whichever company was resolved first, and a wrong identity is
    worse than a missing one.  Per-target planning gives one board per item.
    """
    vendor = str(getattr(adapter, "vendor", "") or "").strip().lower()
    if not vendor or not _is_ats(adapter):
        return None
    slugs: list[str] = []
    reader = getattr(adapter, "slugs_of", None)
    if callable(reader):
        try:
            slugs = [str(s).strip() for s in (reader(item) or []) if str(s).strip()]
        except Exception:  # noqa: BLE001 - a malformed query is "no board", not a crash
            log.warning("[%s] could not read a board slug for company keying",
                        item.get("adapter_key"), exc_info=True)
            slugs = []
    if not slugs:
        slug = str((item.get("native_query") or {}).get("slug") or "").strip()
        slugs = [slug] if slug else []
    if len(slugs) != 1:
        return None
    return vendor, slugs[0]


def _target_key(unit: _Unit) -> str:
    """A stable name for the thing this plan item reads (N4, FR-342).

    The fetch ledger answers "was *this target* read recently".  Counting fresh
    rows per adapter instead made one Greenhouse board's freshness stand for all
    6,900 of them, so a second campaign skipped every board once any hundred
    rows were fresh.  An ATS board is named by its slug; anything else is named
    by the same identity the planner uses to match a re-plan to its rows, so a
    EURES partition and its re-plan are one target and two partitions are two.

    The key itself is :func:`planning.target_key`, because the reuse assessment
    reads these rows back and a second implementation that drifted by one
    character would silently restore the per-adapter behaviour this replaced.
    """
    return planning.target_key(unit.adapter_key, unit.item.get("native_query"), unit.board)


def _record_ledger(unit: _Unit) -> None:
    """Remember that this target was read, whatever it answered (N4).

    A 404 board is as worth remembering as a 200 one: both answer "do not read
    this again this week".  The ledger row is written for every item that
    actually issued a request, so a re-run can skip a target on evidence rather
    than on a per-adapter row count.
    """
    view = unit.egress
    if view is None or not getattr(view, "requests", 0):
        return
    url = getattr(view, "last_url", None)
    if not url:
        return
    try:
        egress_client.record_fetch(
            unit.adapter_key,
            _target_key(unit),
            str(url),
            http_status=getattr(view, "last_status", None),
            record_count=unit.outcome.written,
            etag=getattr(view, "last_etag", None),
            last_modified=getattr(view, "last_modified", None),
        )
    except Exception:  # noqa: BLE001 - the ledger is an optimisation, never a run failure
        log.warning("[%s] could not record the fetch ledger entry", unit.adapter_key,
                    exc_info=True)


def _board_company(unit: _Unit, records: list[Any]) -> str | None:
    """The knowledge-base company that runs this board, created if it is new.

    ``max_companies`` (FR-186) counts company rows, and until this ran the
    adapters that actually retrieve wrote vacancies whose employer existed only
    as a name: 7,500 companies could never materialise because no board ever
    produced one.  The identity is the board itself - ``(ats_vendor, ats_slug)``
    is stable across every spelling of the employer's name on it, and it is what
    lets the *next* campaign read the same board (DR-101, FR-162).

    The row is written through the knowledge-base writer, so it carries the
    adapter as its ``source`` and gets a provenance row pointing at the plan
    item that produced it (FR-166, NFR-402).
    """
    if unit.board_company_resolved:
        return unit.board_company_id
    unit.board_company_resolved = True
    vendor, slug = unit.board or ("", "")
    query = unit.item.get("native_query") or {}
    planned = str(query.get("company_id") or "").strip()
    if planned and kb_repo.get_company(planned):
        unit.board_company_id = planned
    else:
        found = kb_repo.find_shared("company", {"ats_vendor": vendor, "ats_slug": slug})
        if found:
            unit.board_company_id = str(found["id"])
    if unit.board_company_id is None:
        named = [str(query.get("company_name") or "")]
        named += [str(_record_data(r).get("company_name_raw") or "") for r in records]
        # The board names its owner; the slug is the fall-back, and it is still
        # an identity - "a company with an empty board counts" (plan, appendix B).
        name = next((n.strip() for n in named if n.strip()), "")
        method = getattr(unit.adapter, "access_method", None)
        outcome = unit.writer.write(
            {
                "entity_type": "company",
                # A board is the employer's own system of record, so the claim
                # that this company exists is a strong one - but the name may be
                # only the slug, so it is not a strong claim about the name.
                "confidence": 0.6,
                "data": {
                    "name": name or slug,
                    "ats_vendor": vendor,
                    "ats_slug": slug,
                    "access_method": str(getattr(method, "value", method) or "api"),
                },
            }
        )
        unit.board_company_id = outcome.entity_id if outcome else None
    if unit.board_company_id:
        unit.company_ids.add(unit.board_company_id)
    return unit.board_company_id


def _link_board_company(unit: _Unit, records: list[Any]) -> None:
    """Attach this board's postings to the company that runs it (FR-166)."""
    if unit.board is None:
        return
    unlinked = [
        r for r in records
        if _record_entity(r) == "vacancy" and not _record_data(r).get("company_id")
    ]
    if not unlinked:
        return
    company_id = _board_company(unit, unlinked)
    if not company_id:
        return
    for record in unlinked:
        _record_data(record)["company_id"] = company_id


def _extraction_counters(adapter: Any) -> tuple[int, int]:
    return (
        int(getattr(adapter, "_extraction_attempts", 0) or 0),
        int(getattr(adapter, "_extraction_successes", 0) or 0),
    )


def _classify_refusals(step: ItemOutcome, refusals: list[Any], *, board: bool) -> None:
    """Sort this page's refusals into decision, dead target and failure.

    Every refusal is classified exactly once and the three counts add up to
    ``refused``, so an answer can neither be counted twice nor fall between the
    buckets - which is what a second, independent reading of the same log line
    would eventually do.
    """
    for refusal in refusals:
        if not isinstance(refusal, Refusal):  # pragma: no cover - defensive
            continue
        kind = refusal.kind(board=board)
        if kind == "blocked":
            step.blocked += 1
            step.robots_blocked += 1 if refusal.robots else 0
            step.block_reason = step.block_reason or refusal.detail
        elif kind == "gone":
            step.gone += 1
            step.gone_reason = step.gone_reason or refusal.detail
            step.gone_status = step.gone_status or refusal.status
        else:
            step.failed_status = step.failed_status or refusal.status


def _last_detail(refusals: list[Any]) -> str:
    if not refusals:
        return "no detail recorded"
    last = refusals[-1]
    return last.detail if isinstance(last, Refusal) else str(last)


async def _run_page(unit: _Unit, page: int, books: _Bookkeeping) -> ItemOutcome:
    """One page of one source, measured at every step (FR-181, NFR-403)."""
    step = ItemOutcome()
    plan_item = PlanItem(
        adapter_key=unit.adapter_key,
        native_query={**(unit.item.get("native_query") or {}), "page": page},
        rationale=unit.item.get("rationale") or "",
        estimated_pages=1,
        caps=unit.item.get("caps") or {},
    )
    # This item's own counters, not the client's global ones: with buckets
    # running in parallel the global figures belong to no single item.
    egress = unit.egress
    requests_before = int(getattr(egress, "requests", 0))
    refusals_before = len(getattr(egress, "refusals", ()) or ())

    def measure() -> None:
        """Charge this page with the requests and refusals it made itself.

        The refusals are classified here rather than counted here and read
        somewhere else, so every exit from this function - including the two
        that raise - leaves the same, complete verdict behind.
        """
        step.requests = int(getattr(egress, "requests", 0)) - requests_before
        new = list(getattr(egress, "refusals", ()) or ())[refusals_before:]
        step.refused = len(new)
        _classify_refusals(step, new, board=unit.board is not None)

    attempts_before, successes_before = _extraction_counters(unit.adapter)
    failures_before = len(getattr(unit.writer, "failures", ()) or ())
    try:
        records = await unit.adapter.run(plan_item)
    except RobotsDisallowed as exc:
        # FR-182 / IR-101: we declined this source; the source did not fail us.
        # No page is counted, so NFR-403's breakage detector is not fed a
        # refusal it would read as a layout change - which is how 96 dead
        # Personio boards made a working adapter look broken.
        measure()
        if not step.blocked:
            # The adapter raised without fetching through this item's view.
            step.blocked = 1
            step.robots_blocked = 1
        step.block_reason = step.block_reason or f"robots.txt disallows this source: {exc}"
        step.last_error = f"robots.txt disallows this source (FR-182): {exc}"
        return step
    except SourceUnavailable as exc:
        # The adapter's own "every request I issued was turned down" (FR-185).
        # What that *means* is in the answers, which this item's view recorded:
        # a bot wall is a decision, a 404 on a board the registry offered is a
        # dead target, and a 5xx, a rate limit or a transport error is a
        # failure - and stays one.
        measure()
        step.charged = step.requests
        if step.failed_requests or not step.refused:
            step.errors = 1
            step.last_error = f"{type(exc).__name__}: {exc}"
            step.pages = 1 if step.requests else 0
        else:
            step.last_error = step.block_reason or step.gone_reason
        return step
    except Exception as exc:  # noqa: BLE001 - one page must not stop the run
        log.exception("[%s] page %d failed", unit.adapter_key, page)
        measure()
        step.errors = 1
        step.last_error = f"{type(exc).__name__}: {exc}"
        # NFR-403: a request that was made and yielded nothing is an extraction
        # attempt that failed.  Counting it is what lets the rolling rate
        # collapse to zero for a board that has stopped answering at all,
        # instead of leaving it with no rate and no alarm.
        step.pages = 1 if step.requests else 0
        step.charged = step.requests
        return step

    attempts_after, successes_after = _extraction_counters(unit.adapter)
    measure()
    step.attempted = attempts_after - attempts_before
    step.parsed = successes_after - successes_before
    # Reset by the adapter at the start of every ``run()``, so this counts only
    # the answers this page received (NFR-403).
    step.stated_empty = _stated_empty(unit.adapter)
    step.normalised = len(records)
    _link_board_company(unit, records)
    written = await _write_records(unit, records, books)
    step.created = sum(1 for outcome in written if outcome.created)
    step.updated = len(written) - step.created
    step.dropped = len(getattr(unit.writer, "failures", ()) or ()) - failures_before
    if step.dropped:
        step.last_error = getattr(unit.writer, "last_failure", None)
    # A page counts as fetched when the adapter did something: it issued a
    # request, or it returned material to extract from.  A page on which no
    # request was made is not a page, and charging the run's budget for it is
    # what starved the sources at the end of the plan (FR-186).
    if step.requests or step.attempted:
        step.pages = 1
        step.productive_pages = 1 if step.parsed else 0
        # FR-186 counts requests, not adapter invocations.  A website crawl is
        # one "page" to the pipeline and up to thirty requests to the site, and
        # charging it as one is what let a handful of sources spend a whole
        # run's budget while the cap believed it had barely been touched.
        step.charged = max(1, step.requests)
    if step.refused:
        # N7: a board that answered 404, 403 or 500, or that robots.txt refused,
        # has not been collected.  ``EgressClient.fetch`` returns the non-2xx
        # rather than raising, so an adapter that swallows it returned an empty
        # list - and an empty list used to be written down as "done, 0 records,
        # 0 errors", which is the shape every bug in this pipeline hid behind.
        detail = _last_detail(list(getattr(egress, "refusals", ()) or ()))
        if not step.dropped:
            step.last_error = (
                f"{step.refused} request(s) refused - {detail}"
                if step.normalised
                else f"{step.refused} request(s) refused and nothing collected - {detail}"
            )
        if not step.normalised and step.failed_requests:
            # Nothing survived, and what turned us down was neither a decision
            # we made nor a target that is gone: this page failed.
            step.errors = max(step.errors, 1)
    return step


def _absorb(unit: _Unit, step: ItemOutcome) -> None:
    total = unit.outcome
    total.requests += step.requests
    total.attempted += step.attempted
    total.parsed += step.parsed
    total.normalised += step.normalised
    total.created += step.created
    total.updated += step.updated
    total.dropped += step.dropped
    total.pages += step.pages
    total.productive_pages += step.productive_pages
    total.charged += step.charged
    total.errors += step.errors
    total.refused += step.refused
    total.stated_empty += step.stated_empty
    total.blocked += step.blocked
    total.gone += step.gone
    total.robots_blocked += step.robots_blocked
    # Per request, and never twice for one page: see ``page_failures``.
    total.failed += step.page_failures
    total.block_reason = total.block_reason or step.block_reason
    total.gone_reason = total.gone_reason or step.gone_reason
    total.gone_status = total.gone_status or step.gone_status
    total.failed_status = total.failed_status or step.failed_status
    if step.last_error:
        total.last_error = step.last_error


class _Bookkeeping:
    """The run's own writes, coalesced, and the barrier it yields at.

    Three of the writes collection makes are frequent, small and *derived*:
    the progress bar on ``job_run``, and ``records_collected`` and
    ``error_count`` on the plan item.  One page used to write each of them
    separately and immediately, which on a 2,686-item campaign is thirteen
    thousand transactions through the single writer (CR-408) to keep two
    screens up to date.

    Each is accumulated here instead and written on a bounded cadence, when
    the source that produced it finishes, and at every point where the run can
    end - a cap, a cancel, a failure, or the last page.  So the numbers are
    never stale by more than a second or two, and they are exact by the time
    anything reads them at rest.

    The checkpoint is deliberately not in here.  It is the one write whose
    cadence NFR-401 fixes - a crash may lose the page in flight and no more -
    and it is written after every page exactly as it was.  ``ctx`` is
    published for it, and for the barrier: everything downstream carries the
    bookkeeper rather than the context, so the two cannot drift apart.
    """

    def __init__(self, ctx: JobContext) -> None:
        self.ctx = ctx
        self._done = 0
        self._total: int | None = None
        self._progress_written: tuple[int, int | None] | None = None
        self._progress_at = 0.0
        self._counters: dict[str, dict[str, Any]] = {}
        self._counters_at = 0.0

    # -- FR-185: where pause and cancel are honoured ------------------------
    async def barrier(self) -> None:
        """Yield to the loop and honour pause/cancel (FR-185, NFR-401)."""
        await self.ctx.checkpoint_barrier()

    # -- the progress bar (FR-185) -----------------------------------------
    def progress(self, done: int, total: int | None = None) -> None:
        """Publish the bar, clamped to the denominator and never backwards.

        Both sides count planned pages now, but the bar sits at the end of a
        resumed run's restored counters, of a plan that grows while a harvest
        stage runs, and of a unit whose pages shrink when it fails early: any
        of those can hand it a pair that reads as more than full.  A screen
        that says 287% is a screen that has stopped being evidence, so the
        numerator is clamped to the denominator, and it never decreases - the
        one property a progress bar can still promise when the plan changes.
        """
        done = max(0, int(done))
        if total is not None:
            total = max(0, int(total))
            self._total = total
        if self._total is not None:
            done = min(done, self._total)
        self._done = max(self._done, done)
        self._flush_progress()

    def _flush_progress(self, *, force: bool = False) -> None:
        pending = (self._done, self._total)
        if pending == self._progress_written:
            return
        now = time.monotonic()
        if not force and now - self._progress_at < PROGRESS_INTERVAL_SECONDS:
            return
        self.ctx.progress(self._done, self._total)
        self._progress_written = pending
        self._progress_at = now

    # -- a plan item's own counters (FR-185) -------------------------------
    def count(
        self,
        plan_item_id: str,
        *,
        records: int = 0,
        errors: int = 0,
        last_error: str | None = None,
    ) -> None:
        """Charge one page's records and failures to its plan item."""
        if not (records or errors or last_error):
            return
        pending = self._counters.setdefault(
            plan_item_id, {"records": 0, "errors": 0, "last_error": None}
        )
        pending["records"] += records
        pending["errors"] += errors
        if last_error:
            pending["last_error"] = last_error
        self._flush_counters()

    def flush_counters(self, plan_item_id: str) -> None:
        """Write one plan item's counters now.

        Called when its source finishes, so that nothing is still queued for a
        row that :func:`_settle` is about to write its verdict onto - a
        pending increment landing afterwards would overwrite the settled
        ``last_error`` with the message of a page that has already been
        accounted for.
        """
        pending = self._counters.pop(plan_item_id, None)
        if pending:
            self._write_counter(plan_item_id, pending)

    def _flush_counters(self, *, force: bool = False) -> None:
        if not self._counters:
            return
        now = time.monotonic()
        if not force and now - self._counters_at < COUNTER_INTERVAL_SECONDS:
            return
        for plan_item_id, pending in list(self._counters.items()):
            del self._counters[plan_item_id]
            self._write_counter(plan_item_id, pending)
        self._counters_at = now

    @staticmethod
    def _write_counter(plan_item_id: str, pending: dict[str, Any]) -> None:
        repo.bump_plan_item(
            plan_item_id,
            records=int(pending["records"]),
            errors=int(pending["errors"]),
            last_error=pending["last_error"],
        )

    # -- every way the run can end -----------------------------------------
    def flush(self) -> None:
        """Make everything accumulated true on disk, in the order it is read.

        Counters first: they are increments on the plan item, and the settle
        write that follows states the item's verdict on the same row.
        """
        self._flush_counters(force=True)
        self._flush_progress(force=True)


async def _write_records(unit: _Unit, records: list[Any], books: _Bookkeeping) -> list[Any]:
    """Write one page's records in bounded groups (FR-185, CR-408).

    ``write_many`` is a loop over ``write``, and each ``write`` is one or two
    transactions through the single writer.  A page of sixty postings is
    therefore a hundred-odd transactions in one unbroken synchronous stretch,
    with no opportunity to notice that the user pressed Pause - and against a
    knowledge base with a full-text index over tens of thousands of rows that
    stretch is measured in seconds, not milliseconds.

    The records written, their order and their outcomes are exactly what
    ``write_many(records)`` produces.  What changes is that the interval
    between two pause/cancel checks is bounded by ``WRITE_BATCH`` records
    rather than by however big this page turned out to be.  A page small
    enough not to matter is still written in one call, so the ordinary ATS
    board behaves exactly as it did.
    """
    if len(records) <= WRITE_BATCH:
        return unit.writer.write_many(records)
    written: list[Any] = []
    for start in range(0, len(records), WRITE_BATCH):
        await books.barrier()
        written.extend(unit.writer.write_many(records[start : start + WRITE_BATCH]))
    return written


async def collection_worker(ctx: JobContext) -> None:
    """Execute every active plan item of a campaign, page by page (FR-181)."""
    campaign_id = ctx.campaign_id or ""
    campaign = repo.get_campaign_any(campaign_id)
    if campaign is None:
        raise LookupError(f"No campaign {campaign_id}")

    caps = Caps.from_campaign(campaign)
    stats = CollectionStats()
    # Every write this run makes about itself goes through here, and so does
    # every point at which it offers to stop (NFR-102, FR-185).
    books = _Bookkeeping(ctx)
    completed: dict[str, int] = dict(ctx.checkpoint.get("completed") or {})
    started = time.monotonic()
    # Pages already fetched by an interrupted run count towards both the
    # progress bar and the page cap: this is the same job continuing (NFR-401).
    # The saved counter is authoritative because the budget is charged in
    # requests; the page numbers in ``completed`` are the fall-back for a
    # checkpoint written before that was true.
    saved = ctx.checkpoint.get("stats") if isinstance(ctx.checkpoint.get("stats"), dict) else {}
    stats.pages = int((saved or {}).get("pages") or 0) or sum(
        int(v) for v in completed.values()
    )
    # The progress numerator counts *planned pages done*, restored on the same
    # terms as the budget above: a checkpoint written before this counter
    # existed falls back to the per-item ``completed`` page counts, which are
    # exactly the planned pages the interrupted attempt finished (NFR-401).
    stats.pages_done = int((saved or {}).get("pages_done") or 0) or sum(
        int(v) for v in completed.values()
    )
    # FR-185 / NFR-401: the records, failures, refusals and dead targets the
    # interrupted attempt counted are work this attempt will not repeat, so a
    # resumed job reports the same totals as an uninterrupted one.
    stats.restore(saved)
    # FR-186 / NFR-401: a resumed run continues against the same per-stage
    # reservations, so an interrupted campaign cannot spend discovery's share
    # twice and starve its own harvest stage on the second attempt.
    for key, value in ((saved or {}).get("pages_by_stage") or {}).items():
        try:
            stats.pages_by_stage[int(key)] = int(value)
        except (TypeError, ValueError):
            continue

    items = [
        i
        for i in repo.list_plan_items(campaign_id, include_excluded=False)
        if i["status"] in ("planned", "running", "failed")
    ]
    catalogue = {c["adapter_key"]: c for c in repo.list_catalogue(enabled_only=False)}
    repo.set_stage(campaign_id, "collection", "running")
    admin_caps: dict[str, dict] = {}
    # Several plan items are in flight at once (N5), so the units are held where
    # the cancel and failure handlers can reach every one of them.
    units: list[_Unit] = []

    try:
        async with EgressClient() as egress:
            units.extend(
                await _build_units(
                    items, catalogue, egress, campaign_id, caps, completed, admin_caps, books
                )
            )
            # FR-186: the denominator is settled once, after the sources with no
            # work have been taken out, so the bar cannot end at "1 of 8" on a
            # run in which seven of the eight sources were never runnable.
            # Both terms count planned pages, never charged requests, so the
            # numerator can never overtake the denominator (FR-185).
            total_pages = stats.pages_done + sum(u.remaining for u in units)
            books.progress(stats.pages_done, total_pages)

            # FR-186: every runnable source is guaranteed a floor of the budget
            # before any source is allowed to take more than its share, so an
            # exhausted budget shortens the run instead of deleting its tail.
            floor = max(1, caps.max_pages // max(1, len(units))) if units else 1
            harvested = False
            # The same 20 slots the egress layer already bounds itself by
            # (DREAMJOB_HTTP_MAX_CONCURRENCY), spent on buckets rather than
            # left unused by a serial loop.
            concurrency = int(getattr(egress.settings, "http_max_concurrency", 20) or 20)

            async def harvest_now() -> int:
                """Plan the boards this run's own discovery found (FR-181)."""
                nonlocal harvested
                harvested = True
                units.extend(
                    await _expand_harvest_stage(campaign_id, catalogue, egress, items, units,
                                                caps, admin_caps, books,
                                                budget=max(0, caps.max_pages - stats.pages))
                )
                return stats.pages_done + sum(u.remaining for u in units)

            for pass_limit in (floor, caps.max_pages_per_source):
                # One wave per collection stage, in stage order: the sources
                # that find companies still finish before the ones that need a
                # company to read (FR-181).  Inside a wave the buckets run
                # together, which is where the four-hour window comes from.
                ran_stages: set[int] = set()
                while not stats.stopped_by:
                    pending = [u for u in units if u.stage not in ran_stages and u.remaining > 0]
                    if not pending:
                        break
                    stage = min(u.stage for u in pending)
                    if stage >= repo.STAGE_HARVEST and not harvested:
                        total_pages = await harvest_now()
                        books.progress(stats.pages_done, total_pages)
                        continue
                    ran_stages.add(stage)
                    # FR-186: the stage takes its reservation of what is left,
                    # never the whole of it.  A wave that reaches its ceiling
                    # ends that stage and the next one runs; only a cap in
                    # ``_cap_hit`` ends the run.
                    stats.stage_limit = (
                        stage,
                        stats.pages_by_stage.get(stage, 0)
                        + _stage_allowance(stage, {u.stage for u in pending}, stats, caps),
                    )
                    try:
                        await _run_wave(
                            [u for u in pending if u.stage == stage],
                            pass_limit, books, stats, caps, started, completed,
                            total_pages, concurrency,
                        )
                    finally:
                        stats.stage_limit = None
                        # A wave is over: whatever it accumulated is true now,
                        # not in two seconds' time.
                        books.flush()
                if stats.stopped_by:
                    break
                if not harvested:
                    # No ATS source was planned - which is the normal shape of a
                    # first campaign, when no company had a known board yet.  The
                    # discovery stages have now run, so ask again.
                    total_pages = await harvest_now()
                    books.progress(stats.pages_done, total_pages)

            # The verdicts.  Two writes per plan item over the whole plan is
            # thousands of transactions in one stretch, so the pass yields:
            # a person who pressed Cancel while a 2,686-item campaign was
            # writing its outcomes should not wait for all of them (FR-185).
            # An item left unsettled by that cancel is left runnable, which is
            # what a cancel means, and the handler below says so on its row.
            books.flush()
            for index, unit in enumerate(units):
                if index and not index % SETTLE_BATCH:
                    await books.barrier()
                _settle(unit, stats, campaign_id)
    except JobCancelled:
        books.flush()
        for unit in units:
            if not (unit.running or (unit.started and not unit.settled)):
                continue
            # NFR-401: an interrupted item is rewound so it is runnable again.
            # Left at 'running' it reads as a source that has been fetching for
            # hours on a job that is not running at all.  Every bucket that was
            # in flight is rewound, not just the one that noticed the cancel -
            # and so is every item the cancel caught between its last page and
            # its verdict, which is a gap the settle pass now has because it
            # yields.
            repo.update_plan_item(
                unit.id,
                {
                    "status": "planned",
                    "last_error": (
                        "cancelled while running; resumable"
                        if unit.running
                        else "cancelled before its outcome was written; resumable"
                    ),
                    # FR-361: a cancel is the last thing that happened to this
                    # source, and it supersedes whatever it was doing before.
                    "activity_at": utcnow(),
                    "activity_kind": "cancelled",
                },
            )
        repo.set_stage(campaign_id, "collection", "cancelled")
        repo.record_audit(
            "campaign.collection_cancelled",
            job_seeker_id=ctx.job_seeker_id,
            entity_type="campaign",
            entity_id=campaign_id,
            detail=stats.to_dict(),
        )
        raise
    except Exception as exc:
        books.flush()
        for unit in units:
            if not unit.running:
                continue
            reason = f"{type(exc).__name__}: {exc}"
            unit.outcome.failed += 1
            repo.update_plan_item(
                unit.id,
                {
                    "status": "failed",
                    "last_error": reason,
                    # FR-361: the run fell over underneath this source; that is
                    # the last thing that happened to it.
                    "activity_at": utcnow(),
                    "activity_kind": "failed",
                    **_outcome_columns(unit, "failed", reason),
                },
            )
            repo.bump_plan_item(unit.id, errors=1)
        repo.set_stage(campaign_id, "collection", "failed")
        raise

    outcome = _run_outcome(stats)
    repo.set_stage(campaign_id, "collection", _campaign_status(outcome))
    repo.record_audit(
        "campaign.collection_finished",
        job_seeker_id=ctx.job_seeker_id,
        entity_type="campaign",
        entity_id=campaign_id,
        detail={**stats.to_dict(), "outcome": outcome},
    )
    prune_raw_documents(campaign_id)
    # Collection used to end here, leaving the vacancies it had just written
    # one un-pressed button away from being of any use (FR-261, FR-281).
    ranking = await _rank_collected(ctx, campaign, outcome)
    # Last: the counters and the bar are exact at rest, whatever cadence they
    # were written on while the run was in flight.
    books.flush()
    # A campaign writes tens of thousands of rows; folding the log back now
    # keeps the WAL from growing for the length of the next run (NFR-102).
    # PASSIVE never blocks, so it is safe while readers are attached.
    try:
        from dreamjob.db.connection import checkpoint  # noqa: PLC0415

        checkpoint("PASSIVE")
    except Exception:  # noqa: BLE001 - storage hygiene never fails a campaign
        log.debug("WAL checkpoint after collection failed", exc_info=True)
    ctx.save_checkpoint(
        completed=completed,
        stats=stats.to_dict(),
        ranking=ranking,
        finished_at=utcnow(),
    )


async def _rank_collected(ctx: JobContext, campaign: dict, outcome: str) -> dict | None:
    """Turn what this run collected into a ranked list (FR-261, FR-264, FR-281).

    These are the same four passes the Opportunities screen runs from its own
    buttons, in the order that screen runs them, because a collection that
    stops short of them produces a screen reading "0 opportunities" with
    nothing to say why.  A seeker who has just watched a campaign collect
    42,883 vacancies has already asked for this; the buttons stay for re-runs.

    Failure is per-pass and never fails the collection: the records are
    written and the run was good, so a pass that falls over is recorded
    against the campaign and the ones after it still get their turn.  Only
    synthesis is load-bearing - the other three all read the opportunity rows
    it writes - so it is the one whose failure stops the chain.
    """
    if outcome not in ("completed", "partial"):
        return None

    # NFR-401: each pass records its own completion in the checkpoint.  A run
    # that dies between synthesis and scoring then resumes at the pass it had
    # not finished, instead of rebuilding tens of thousands of opportunity rows
    # or - the failure this replaces - the campaign reading "completed" with
    # every row unscored because nothing ever ran the passes.
    completed_passes: set[str] = {
        str(name) for name in (ctx.checkpoint.get("rank_passes") or [])
    }
    report: dict[str, Any] = dict(ctx.checkpoint.get("rank_report") or {})

    async def once(name: str, fn: Any, *, async_pass: bool = False) -> dict:
        """Run one pass unless its predecessor already recorded it done."""
        if name in completed_passes:
            previous = report.get(name)
            return previous if isinstance(previous, dict) else {}
        result = await (
            _pass_async(ctx, campaign, name, fn)
            if async_pass
            else _pass(ctx, campaign, name, fn)
        )
        report[name] = result
        completed_passes.add(name)
        ctx.save_checkpoint(rank_passes=sorted(completed_passes), rank_report=report)
        return result

    synthesis = await once("synthesis", _synthesise)
    if "error" in synthesis:
        return report
    # FR-149: a spontaneous-application campaign plans no job board and no ATS,
    # so synthesis alone can only ever hand it an empty list.  The track that
    # gives such a campaign its opportunities is the speculative one.
    if _is_spontaneous(campaign):
        await once("speculative", _speculate)
    # FR-264 before FR-281: the compensation sub-score reads the stored
    # estimate rather than recomputing it, so scoring first would score every
    # unpriced opportunity against a range nothing had filled in.
    await once("compensation", _price)
    # Company enrichment, before scoring: the employer kind decides what the
    # product may do with an employer, and company attractiveness reads the
    # website profile, the signals and the filings.  Every one of those passes
    # already existed and none ran automatically, so a campaign left every
    # employer a bare name with "employer type not verified" on every row.
    await once("enrichment", _enrich, async_pass=True)
    await once("scoring", _score)

    repo.record_audit(
        "campaign.ranked",
        job_seeker_id=ctx.job_seeker_id,
        entity_type="campaign",
        entity_id=campaign["id"],
        detail=report,
    )
    # The terminal marker: a resumed worker that sees this knows the chain is
    # whole and does not replay a pass over an already-ranked corpus.
    ctx.save_checkpoint(ranked=True)
    return report


async def _pass(ctx: JobContext, campaign: dict, name: str, fn: Any) -> dict:
    """Run one post-collection pass off this job's loop, reporting its outcome.

    The passes are synchronous and, on a campaign that collected 40k
    vacancies, slow; they belong off the loop so the run stays cancellable
    while they work (NFR-102).
    """
    try:
        return await asyncio.to_thread(fn, campaign)
    except JobCancelled:
        raise
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        log.warning("Post-collection %s failed for campaign %s: %s", name, campaign["id"], reason)
        repo.record_audit(
            f"campaign.{name}_failed",
            job_seeker_id=ctx.job_seeker_id,
            entity_type="campaign",
            entity_id=campaign["id"],
            detail={"error": reason},
        )
        return {"error": reason}


def _synthesise(campaign: dict) -> dict:
    """FR-261.  The opportunity ids are dropped: on a large campaign they run
    to tens of thousands and say nothing the counts do not."""
    fn = _load("dreamjob.pipeline.opportunities", "synthesise_campaign")
    if fn is None:
        return {"skipped": "no synthesis stage"}
    return {k: v for k, v in fn(campaign).as_dict().items() if k != "opportunity_ids"}


def _is_spontaneous(campaign: dict) -> bool:
    fn = _load("dreamjob.pipeline.speculative", "is_spontaneous_campaign")
    return bool(fn(campaign)) if fn is not None else False


def _speculate(campaign: dict) -> dict:
    """FR-262.  Consent is the seeker's to give, so its absence is an outcome
    to report rather than an error to log."""
    module = "dreamjob.pipeline.speculative"
    fn = _load(module, "generate_campaign")
    if fn is None:
        return {"skipped": "no speculative stage"}
    consent_required = _load(module, "ConsentRequired") or ()
    try:
        return fn(campaign).as_dict()
    except consent_required as exc:  # type: ignore[misc]
        return {"error": "consent_required", "detail": str(exc)}


def _price(campaign: dict) -> dict:
    """FR-264: pure corpus work, no tokens and no network."""
    fn = _load("dreamjob.pipeline.compensation", "enrich_campaign")
    if fn is None:
        return {"skipped": "no compensation stage"}
    return fn(campaign["job_seeker_id"], campaign["id"])


async def _pass_async(ctx: JobContext, campaign: dict, name: str, fn: Any) -> dict:
    """Run one asynchronous post-collection pass, reporting its outcome.

    The employer-kind ladder and the website crawl are coroutines (they own
    their concurrency and their own network budget), so they cannot go through
    :func:`_pass`, which runs a synchronous callable in a thread.
    """
    try:
        result = await fn(campaign)
    except JobCancelled:
        raise
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        log.warning("Post-collection %s failed for campaign %s: %s", name, campaign["id"], reason)
        repo.record_audit(
            f"campaign.{name}_failed",
            job_seeker_id=ctx.job_seeker_id,
            entity_type="campaign",
            entity_id=campaign["id"],
            detail={"error": reason},
        )
        return {"error": reason}
    if hasattr(result, "as_dict"):
        return result.as_dict()
    if isinstance(result, dict):
        return result
    return {"result": str(result)[:200]}


async def _enrich(campaign: dict) -> dict:
    """Company enrichment for the campaign's shortlist (FR-221..246, FR-341).

    Bounded: a campaign that touched 1,600 companies still shows its list
    promptly, and the rest is picked up by the next run or the nightly sweep.
    """
    from dreamjob.pipeline import company_enrichment  # noqa: PLC0415

    report = await company_enrichment.enrich_campaign(
        campaign["id"],
        campaign["job_seeker_id"],
        limit=company_enrichment.DEFAULT_COMPANY_LIMIT,
    )
    return report.as_dict()


def _score(campaign: dict) -> dict:
    """FR-281.  The deterministic pass costs no tokens and scores everything;
    the model is then spent on the top rows only, and only as far as the
    campaign's own token budget allows (NFR-104)."""
    fn = _load("dreamjob.pipeline.scoring", "score_campaign")
    if fn is None:
        return {"skipped": "no scoring stage"}
    return fn(campaign).as_dict()


def prune_raw_documents(campaign_id: str | None = None, limit: int = RAW_DOCUMENT_SWEEP_LIMIT) -> int:
    """Delete stored bodies nothing refers to any more (FR-183, DR-102).

    Every fetched page is stored raw so extractors can be re-run (FR-183), and
    nothing ever removed one.  A weekly revalidation of ~6,900 ATS boards
    replaces the bodies of the few hundred that changed and leaves the previous
    ones behind - ~160 MB a week, ~8 GB a year, cited by no provenance row, no
    vacancy and no live cache entry (plan section 4, L2).

    Only documents older than the retention window are candidates, so a body
    this campaign has just fetched is never one, and the sweep is bounded so it
    cannot hold the single writer (CR-408) for long.  It runs at the end of a
    collection run and can never fail one: storage hygiene is not worth a
    campaign.
    """
    try:
        settings = get_settings()
        days = int(
            getattr(settings, "raw_document_retention_days", RAW_DOCUMENT_RETENTION_DAYS)
            or RAW_DOCUMENT_RETENTION_DAYS
        )
        if days <= 0:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        orphans = hygiene_repo.orphan_raw_documents(cutoff, utcnow(), limit=limit)
        if not orphans:
            return 0
        root = settings.abs_data_dir
        removable: list[str] = []
        freed = 0
        for row in orphans:
            try:
                (root / str(row["storage_path"])).unlink(missing_ok=True)
            except OSError:
                # The row stays: a row whose file could not be removed still
                # describes a file that is there.
                log.warning("Could not delete orphaned raw document %s", row["storage_path"])
                continue
            removable.append(str(row["id"]))
            freed += int(row.get("byte_size") or 0)
        deleted = hygiene_repo.delete_raw_documents(removable)
        if deleted:
            log.info("Pruned %d orphaned raw document(s), %.1f MB", deleted, freed / 1e6)
            repo.record_audit(
                "raw_document.pruned",
                entity_type="campaign",
                entity_id=campaign_id,
                detail={"documents": deleted, "bytes": freed, "retention_days": days},
            )
        return deleted
    except Exception:  # noqa: BLE001 - housekeeping never fails a campaign
        log.exception("Pruning orphaned raw documents failed")
        return 0


def _run_outcome(stats: CollectionStats) -> str:
    """What the run achieved, in one word, for the audit trail (FR-185)."""
    if stats.records and not stats.errors and not stats.stopped_by:
        return "completed"
    if stats.records:
        return "partial"
    return "failed" if (stats.errors or stats.by_adapter) else "nothing_to_collect"


def _campaign_status(outcome: str) -> str:
    """The campaign's terminal status, in the vocabulary the schema allows.

    ``completed`` used to be written unconditionally, so a run in which every
    source failed and not one record was retrieved still reported success.  A
    run that produced nothing and had something to try is a failed run; the
    finer "partial" answer is kept on the audit event, because the campaign
    status column is draft|planned|running|paused|completed|cancelled|failed.
    """
    return "failed" if outcome == "failed" else "completed"


async def _build_units(
    items: list[dict],
    catalogue: dict[str, dict],
    egress: EgressClient,
    campaign_id: str,
    caps: Caps,
    completed: dict[str, int],
    admin_caps: dict[str, dict],
    books: _Bookkeeping,
    skip_logged: set[str] | None = None,
) -> list[_Unit]:
    """Turn the plan into runnable work, settling what cannot run (IR-101).

    One pass over the whole plan, constructing an adapter and settling the
    items that cannot run - and, before this yielded, the one stretch of a
    collection job with no I/O in it at all.  On a 2,686-item campaign it ran
    for long enough that a pause pressed just after Launch was not answered
    until the first page had been fetched (FR-185).
    """
    units: list[_Unit] = []
    # One line per unavailable or browser-only source, however many of its plan
    # items this run holds (a 379-item registry plan used to warn once per item,
    # and to log an ERROR with a traceback once per page it was charged).
    logged: set[str] = skip_logged if skip_logged is not None else set()
    for index, item in enumerate(items):
        if index and not index % PREPARE_BATCH:
            await books.barrier()
        try:
            unit = _build_unit(
                item, catalogue, egress, campaign_id, caps, completed, admin_caps, logged
            )
        except Exception as exc:  # noqa: BLE001 - the item fails, the campaign does not
            # Every decision about one plan item belongs to that plan item.  The
            # skip decision used to sit outside all exception handling, so a
            # malformed native_query took the whole job down with it and left
            # every source looking untouched - status planned, 0 errors.
            log.exception("[%s] could not be prepared for collection", item.get("adapter_key"))
            reason = f"{type(exc).__name__}: {exc}"
            repo.update_plan_item(
                item["id"],
                {
                    "status": "failed",
                    "last_error": reason,
                    # A plan item this product could not even prepare is a
                    # failure of ours, and it stays one (FR-185).
                    "outcome_state": "failed",
                    "outcome_reason": reason[:2000],
                    "failed_count": int(item.get("failed_count") or 0) + 1,
                    # FR-361: it never ran, so nothing else will ever say what
                    # became of it.
                    "activity_at": utcnow(),
                    "activity_kind": "failed",
                },
            )
            repo.bump_plan_item(item["id"], errors=1)
            continue
        if unit is not None:
            units.append(unit)
    units.sort(key=lambda u: (u.stage, u.adapter_key))
    return units


def _log_skip_once(logged: set[str], adapter_key: str, reason: str) -> None:
    """One skip line per source per plan, not one per item and not one per page."""
    if adapter_key in logged:
        return
    logged.add(adapter_key)
    log.info("[%s] skipped in collection: %s", adapter_key, reason)


def _build_unit(
    item: dict,
    catalogue: dict[str, dict],
    egress: EgressClient,
    campaign_id: str,
    caps: Caps,
    completed: dict[str, int],
    admin_caps: dict[str, dict],
    skip_logged: set[str] | None = None,
) -> _Unit | None:
    """Prepare one plan item, or settle it and return ``None``."""
    entry = catalogue.get(item["adapter_key"], {})
    key = item["adapter_key"]
    logged = skip_logged if skip_logged is not None else set()
    if key in repo.BROWSER_STRATEGY_KEYS:
        # FR-165: the network crawl is a browser strategy, not an adapter, and
        # its plan item is the browser run's target allowlist (FR-205).
        # Collection has nothing to run for it, and saying "no adapter
        # registered" once per item was noise about a source that is working
        # exactly as designed.
        reason = (
            f"{key} is a browser strategy, not a collection adapter: its plan item is "
            "the browser run's allowlist, and collection leaves it to that run (FR-165, FR-205)"
        )
        repo.update_plan_item(
            item["id"],
            {
                "status": "skipped",
                "last_error": reason,
                "outcome_state": "skipped",
                "outcome_reason": reason,
            },
        )
        _log_skip_once(logged, key, reason)
        return None
    # The adapter fetches through this item's own view of the shared client, so
    # its requests and refusals are its own even when other buckets are running.
    view = _UnitEgress(egress)
    adapter = _load_adapter(key, view)
    if adapter is None:
        repo.update_plan_item(
            item["id"],
            {
                "status": "skipped",
                "last_error": "no adapter registered for this source",
                # Nothing to do, and nothing went wrong: 104 plan items of one
                # campaign named adapters that exist only as test fixtures.
                "outcome_state": "skipped",
                "outcome_reason": "no adapter registered for this source",
            },
        )
        return None
    unavailable = adapter_unavailable_reason(adapter)
    if unavailable:
        # FR-245 / RK-06: a required API key this deployment does not hold
        # makes the source unusable, not broken.  It is left *runnable* (like
        # an ATS item with no board yet) so that configuring the key lets the
        # next run collect it - but this run does not charge it a page, count
        # an error, or raise the ``UnusableQuery`` traceback it used to per
        # page.  The planner already leaves it out of new plans.
        reason = f"adapter unavailable: {unavailable}"
        repo.update_plan_item(
            item["id"],
            {
                "status": "planned",
                "last_error": reason,
                "outcome_state": "skipped",
                "outcome_reason": reason,
            },
        )
        _log_skip_once(logged, key, reason)
        return None
    # An ATS plan item with no board slug has nothing to read.  It is left
    # runnable rather than skipped: the company whose board it needs may be
    # discovered by this run, and a terminal 'skipped' would never be
    # revisited even once that company exists (FR-162).
    if _is_ats(adapter) and not _has_ats_slug(adapter, item):
        reason = (
            "no company with a known board for this source yet - run the "
            "discovery sources first (FR-162)"
        )
        repo.update_plan_item(
            item["id"],
            {
                "status": "planned",
                "last_error": reason,
                "outcome_state": "skipped",
                "outcome_reason": reason,
            },
        )
        return None
    pages = _pages_for(item, caps, entry.get("query_capabilities") or {}, admin_caps)
    done = int(completed.get(item["id"], 0))
    return _make_unit(
        item,
        adapter,
        view,
        campaign_id,
        pages=max(pages, done),
        stage=repo.plan_item_stage(item),
        done=done,
    )


def _make_unit(
    item: dict,
    adapter: Any,
    view: Any,
    campaign_id: str,
    *,
    pages: int,
    stage: int,
    done: int = 0,
) -> _Unit:
    """Assemble one runnable plan item: its writer, its bucket, its board."""
    return _Unit(
        item=item,
        adapter=adapter,
        writer=knowledge_base.KnowledgeBaseWriter(
            adapter_key=item["adapter_key"],
            plan_item_id=item["id"],
            campaign_id=campaign_id,
        ),
        pages=pages,
        egress=view,
        bucket=_bucket_for(item, adapter),
        board=_board_of(item, adapter),
        done=done,
        stage=stage,
    )


async def _expand_harvest_stage(
    campaign_id: str,
    catalogue: dict[str, dict],
    egress: EgressClient,
    items: list[dict],
    units: list[_Unit],
    caps: Caps,
    admin_caps: dict[str, dict],
    books: _Bookkeeping,
    budget: int,
) -> list[_Unit]:
    """Plan the ATS boards the discovery stages just found (FR-162, FR-181).

    This is the second half of the two-phase order: discovery writes
    ``company.ats_vendor``/``ats_slug``, and the harvest stage reads them back.
    Without it a board found by this run's own website crawl could only be read
    by the *next* campaign, which is why every ATS source was permanently dark.

    Each board planned is an insert and a read of its own, and the budget can
    be thousands of them, so this yields on the same interval the first pass
    over the plan does: a run that is planning its harvest is still a run that
    can be paused (FR-185).
    """
    covered = {
        (str(row.get("adapter_key")), str((row.get("native_query") or {}).get("slug") or "").lower())
        for row in [*items, *(u.item for u in units)]
    }
    added: list[_Unit] = []
    considered = 0
    if budget <= 0:
        return added
    for key, entry in sorted(catalogue.items()):
        if entry.get("source_type") != "ats" or not entry.get("enabled", 1):
            continue
        if len(added) >= budget:
            break
        adapter = _load_adapter(key, egress)
        vendor = str(getattr(adapter, "vendor", "") or "").lower()
        if adapter is None or not vendor:
            continue
        for company in repo.companies_with_ats_board(vendor, limit=caps.max_companies):
            if len(added) >= budget:
                # One board is one page, so the run's remaining page budget is
                # also the number of boards it can still read (FR-186).
                break
            slug = str(company.get("ats_slug") or "").strip()
            if not slug or (key, slug.lower()) in covered:
                continue
            considered += 1
            if not considered % PREPARE_BATCH:
                await books.barrier()
            covered.add((key, slug.lower()))
            row = {
                "adapter_key": key,
                "native_query": {
                    "slug": slug,
                    "company_id": company.get("id"),
                    "company_name": company.get("name"),
                },
                "rationale": (
                    f"{entry.get('display_name') or key} board of "
                    f"{company.get('name') or slug}, found by this campaign's own discovery"
                ),
                "caps": {"stage": repo.STAGE_HARVEST, "planned_pages": 1},
                "estimated_pages": 1,
                "estimated_seconds": 8,
                "estimated_cost_eur": 0.0,
            }
            item = repo.get_plan_item(repo.insert_plan_item(campaign_id, row))
            if item is None:
                continue
            view = _UnitEgress(egress)
            harvest_adapter = _load_adapter(key, view)
            if harvest_adapter is None:
                continue
            added.append(
                _make_unit(
                    item,
                    harvest_adapter,
                    view,
                    campaign_id,
                    pages=_pages_for(item, caps, entry.get("query_capabilities") or {}, admin_caps),
                    stage=repo.STAGE_HARVEST,
                )
            )
    if added:
        log.info("Collection planned %d ATS board(s) discovered during this run", len(added))
    return added


async def _run_wave(
    wave: list[_Unit],
    pass_limit: int,
    books: _Bookkeeping,
    stats: CollectionStats,
    caps: Caps,
    started: float,
    completed: dict[str, int],
    total_pages: int,
    concurrency: int,
) -> None:
    """Run one stage of the plan, one task per rate-limit bucket (NFR-103).

    Plan items that share a bucket run strictly in sequence, because they share
    a rate limit and a vendor sees one client IP however many tenant subdomains
    it answers on.  Items in different buckets run at the same time, because
    nothing is shared between them but the connection pool and the 20-slot
    concurrency semaphore both of them already respect.  The wall-clock of a
    campaign becomes its longest bucket instead of the sum of all of them -
    which is the difference between 100 minutes and 5.5 hours for Campaign A
    (plan section 2.4), and therefore between passing NFR-103 and failing it.

    Concurrency changes no rate: the egress limiter still paces every domain,
    and it is still the only thing that decides when a request may go out.
    """
    buckets: dict[str, list[_Unit]] = {}
    for unit in wave:
        buckets.setdefault(unit.bucket, []).append(unit)

    async def drain(units: list[_Unit], slots: asyncio.Semaphore | None) -> None:
        for unit in units:
            if stats.stopped_by:
                return
            # A cap ends the run and is recorded as the reason; a stage
            # reservation only ends this wave.  The cap is tested first, or a
            # stage that had spent its share would swallow the reason the run
            # actually stopped (FR-186).
            reason = _cap_hit(stats, caps, started)
            if reason:
                stats.stopped_by = reason
                return
            if _stage_exhausted(stats):
                return
            if unit.remaining <= 0:
                continue
            if slots is None:
                await _run_unit(unit, min(pass_limit, unit.pages), books, stats, caps,
                                started, completed, total_pages)
            else:
                async with slots:
                    await _run_unit(unit, min(pass_limit, unit.pages), books, stats, caps,
                                    started, completed, total_pages)

    if len(buckets) <= 1:
        # One bucket is the serial case, and it stays exactly serial: no task,
        # no semaphore, no change to how a single-source campaign behaves.
        for units in buckets.values():
            await drain(units, None)
        return

    slots = asyncio.Semaphore(max(1, concurrency))
    results = await asyncio.gather(
        *(asyncio.create_task(drain(units, slots)) for units in buckets.values()),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    for failure in failures:
        # A cancel is the whole job's answer, so it wins over any other failure
        # a sibling bucket happened to raise on its way out (FR-185, NFR-401).
        if isinstance(failure, JobCancelled):
            raise failure
    if failures:
        raise failures[0]


async def _run_unit(
    unit: _Unit,
    limit: int,
    books: _Bookkeeping,
    stats: CollectionStats,
    caps: Caps,
    started: float,
    completed: dict[str, int],
    total_pages: int,
) -> None:
    """Run one source, flagged as in flight for as long as it is (NFR-401).

    ``running`` is cleared only on the way out through the front door: an item
    interrupted by a cancel keeps it, which is how the worker knows which items
    to rewind to 'planned' when several buckets were in flight at once.

    Whatever this source accumulated is written before the source is left,
    however it is left, so no increment is still queued for a row that
    :func:`_settle` is about to write a verdict onto.
    """
    unit.running = True
    try:
        await _drive_unit(unit, limit, books, stats, caps, started, completed, total_pages)
    finally:
        books.flush_counters(unit.id)
        _stamp_finished(unit)
    unit.running = False


async def _drive_unit(
    unit: _Unit,
    limit: int,
    books: _Bookkeeping,
    stats: CollectionStats,
    caps: Caps,
    started: float,
    completed: dict[str, int],
    total_pages: int,
) -> None:
    """Run one source up to ``limit`` pages, recording what it did (FR-181)."""
    if not unit.started:
        unit.started = True
        # FR-361: the first thing the activity feed can say about a source is
        # that it has started, and it is said in the write that was happening
        # anyway rather than in one of its own.
        repo.update_plan_item(
            unit.id,
            {"status": "running", "activity_at": utcnow(), "activity_kind": "started"},
        )
        unit.activity_kind = "started"
    bucket = stats.adapter(unit.adapter_key)
    while unit.done < min(limit, unit.pages):
        reason = _cap_hit(stats, caps, started)
        if reason:
            stats.stopped_by = reason
            return
        if _stage_exhausted(stats):
            # FR-186: this stage has spent its reservation.  The run continues -
            # the next stage is exactly what the reservation was protecting.
            return
        await books.barrier()  # FR-185 pause / cancel
        page = unit.done + 1
        try:
            step = await _run_page(unit, page, books)
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - the item fails, the run continues
            log.exception("[%s] failed outside the page loop", unit.adapter_key)
            step = ItemOutcome(errors=1, last_error=f"{type(exc).__name__}: {exc}")
        _absorb(unit, step)
        unit.done = page
        # FR-185: the bar's numerator is one *planned page* completed, whatever
        # that page cost the budget in requests.  Counting ``step.charged`` here
        # instead is what let an adapter issuing three requests per page report
        # 287% of a plan whose denominator counted pages.
        stats.pages_done += 1

        if step.errors:
            books.count(unit.id, errors=step.errors, last_error=step.last_error)
            books.ctx.record_error(step.last_error or "collection error")
            bucket["errors"] += step.errors
            stats.errors += step.errors
            unit.pages = unit.done  # a failed source is not paged through further
        elif step.failed_requests:
            # The page produced records and part of what it asked for still
            # failed.  The records are kept and the failure is counted anyway:
            # a source that half answered is not a source that answered (N7).
            # Only the requests that actually failed are charged here - a bot
            # wall or a dead board on the same page is counted below, not as an
            # error the operator is asked to look at.
            books.count(unit.id, errors=step.failed_requests, last_error=step.last_error)
            books.ctx.record_error(step.last_error or "request refused")
            bucket["errors"] += step.failed_requests
            stats.errors += step.failed_requests
        if step.blocked:
            # FR-182 / IR-101: we declined, correctly.  Counted, never buried,
            # and never as an error - the job's own error counter stays the
            # number of things that went wrong.
            bucket["blocked"] += step.blocked
            stats.blocked += step.blocked
        if step.gone:
            bucket["gone"] += step.gone
            stats.gone += step.gone
        if not step.errors and not step.written and (step.refused or not step.pages):
            # Nothing came back and nothing will: robots.txt does not change its
            # mind on page two, a board that is gone is gone, and an adapter
            # that issued no request for page one issues none for page two
            # either - asking would charge the budget for work that never
            # happens (FR-186).
            unit.pages = unit.done
        if step.written:
            books.count(unit.id, records=step.written)
        stats.pages += step.charged
        stats.charge_stage(unit.stage, step.charged)
        stats.records += step.written
        # FR-186: a company this item collected for counts whether the writer
        # created it or the board's identity resolved to one that existed.
        stats.companies |= unit.writer.company_ids | unit.company_ids
        stats.people |= unit.writer.people_ids
        bucket["pages"] += step.charged
        bucket["records"] += step.written

        # NFR-401: the checkpoint is written after the unit of work, so a resume
        # re-runs at most this page - and the writer is idempotent, so
        # re-running it costs time, not correctness.
        completed[unit.id] = unit.done
        # NFR-401 fixes this one's cadence: after the unit of work, every time.
        books.ctx.save_checkpoint(completed=completed, stats=stats.to_dict())
        # ... and this one's is a preference, so it is coalesced (NFR-102).
        books.progress(stats.pages_done, total_pages)
        if step.errors:
            return


#: What each settled state tells the board registry about the board.  The
#: names are the registry slice's own contract (``pipeline/board_registry``),
#: and the distinctions are the point of it:
#:
#: * ``mark_gone`` - 404 or 410, evidence the tenant has left.  Two of them
#:   retire the slug and every later campaign stops paying a request for it,
#:   which is what turns 308 repeated dead boards into one fact learned once.
#: * ``mark_live`` - the board answered, so the failure counter is cleared.
#:   Without this a board that 404'd once during a rename would carry that
#:   strike for ever and be retired by an unrelated 404 months later.
#: * ``mark_unreachable`` - a 5xx, a rate limit, a transport error or a crash.
#:   The status is recorded and the registry's opinion is left exactly as it
#:   was: a vendor having a bad afternoon must never retire the boards it
#:   serves.
#:
#: Every other state says nothing.  ``blocked`` in particular is silence on
#: purpose: robots.txt tells us what we may read, never what exists.
_REGISTRY_VERDICT: dict[str, str] = {
    "gone": "gone",
    "failed": "unreachable",
    "succeeded": "live",
    "no_matches": "live",
}


def _tell_the_registry(unit: _Unit, outcome: ItemOutcome, state: str) -> None:
    """Tell the board registry what this item's board answered (FR-343, DR-101).

    Keyed on the item's settled state rather than on the counters, so a board
    that answered on page one and 404'd on page two - which is how a paging
    board ends - is reported as the live board it is.  What to *do* about each
    answer is the registry's policy, not this module's, and a registry that
    cannot be written never fails a campaign: the board is simply offered again
    next time, which is what happens today.
    """
    if unit.board is None:
        return
    verdict = _REGISTRY_VERDICT.get(state)
    if verdict is None:
        return
    vendor, slug = unit.board
    try:
        if verdict == "gone":
            board_registry.mark_gone(vendor, slug, outcome.gone_status or 404)
        elif verdict == "unreachable":
            board_registry.mark_unreachable(vendor, slug, http_status=outcome.failed_status)
        else:
            # The last answer is only evidence of liveness when it *was* one: a
            # board that answered page one and 404'd page two is a live board,
            # and writing that 404 into the registry row would say otherwise.
            last = int(getattr(unit.egress, "last_status", None) or 200)
            board_registry.mark_live(
                vendor,
                slug,
                http_status=last if 200 <= last < 300 else 200,
                job_count=outcome.normalised,
                company_id=unit.board_company_id,
            )
    except Exception:  # noqa: BLE001 - bookkeeping never fails a run
        log.warning("[%s] could not tell the board registry about %s/%s",
                    unit.adapter_key, vendor, slug, exc_info=True)


def _outcome_columns(unit: _Unit, state: str, reason: str | None) -> dict[str, Any]:
    """The per-item counts migration 130 added, continued rather than replaced.

    A resumed or re-run campaign adds to what the previous attempt recorded,
    the same way ``error_count`` does, so the four columns keep adding up to
    every answer the item has ever had.
    """
    outcome = unit.outcome
    previous = unit.item

    def before(column: str) -> int:
        value = previous.get(column)
        return int(value) if isinstance(value, (int, float)) else 0

    return {
        "outcome_state": state,
        "outcome_reason": reason[:2000] if reason else None,
        "blocked_count": before("blocked_count") + outcome.blocked,
        "gone_count": before("gone_count") + outcome.gone,
        "failed_count": before("failed_count") + outcome.failed,
    }


def _stamp_finished(unit: _Unit) -> None:
    """Record that this source has stopped working, while the run is still on (FR-361).

    :func:`_settle` writes the verdict, but it runs once for the whole plan
    after both waves have finished - so on a four-hour run it is four hours
    late.  What the activity feed needs is the moment the page loop ended, which
    is here, and what it ended on, which :meth:`ItemOutcome.state` already
    computes from the same counters :func:`_settle` will read.  Nothing about
    the verdict is written early: the row keeps ``status='running'`` and a NULL
    ``outcome_state`` until :func:`_settle`, so the FR-185 ledger, which reads
    ``status`` first, does not see this at all.
    """
    if unit.remaining > 0:
        return  # a cap or a stage reservation ended the wave, not the source
    state = unit.outcome.state()
    repo.update_plan_item(unit.id, {"activity_at": utcnow(), "activity_kind": state})
    unit.activity_kind = state


def _settle(unit: _Unit, stats: CollectionStats, campaign_id: str) -> None:
    """Write down what this source did, in words that distinguish the cases."""
    unit.settled = True
    outcome = unit.outcome
    _record_extraction(unit, campaign_id)
    _record_ledger(unit)
    if not unit.started:
        if unit.remaining <= 0:
            # Every page of this item was already fetched by the run this one
            # resumed (NFR-401): it is finished, not waiting.  Its outcome was
            # written by the run that did the work and is left alone.
            repo.update_plan_item(unit.id, {"status": "done", "last_error": None})
        elif stats.stopped_by:
            repo.update_plan_item(
                unit.id,
                {
                    "status": "planned",
                    "last_error": f"not started: {stats.stopped_by} (FR-186)",
                    # FR-186: the budget, not a fault.  4,664 items in one
                    # campaign ended here and every one of them was reported as
                    # a collection error.
                    "outcome_state": "capped",
                    "outcome_reason": f"not started: {stats.stopped_by} (FR-186)",
                },
            )
        else:
            # Its stage spent its reservation before reaching this item.  It is
            # runnable and will be reached by the next run - but saying so is
            # what makes a starved stage legible in the plan table, which is
            # exactly how "not started: max_pages" on 4,429 boards was found.
            reason = "not started: this stage's page reservation was spent (FR-186)"
            repo.update_plan_item(
                unit.id,
                {
                    "status": "planned",
                    "last_error": reason,
                    "outcome_state": "capped",
                    "outcome_reason": reason,
                },
            )
        return
    if stats.stopped_by and unit.remaining > 0 and not outcome.errors:
        # Bounded by a cap, not finished: leave it runnable so that raising the
        # cap continues it from its checkpoint (FR-186).
        reason = f"stopped by cap: {stats.stopped_by} (FR-186)"
        repo.update_plan_item(
            unit.id,
            {"status": "planned", "last_error": reason, **_outcome_columns(unit, "capped", reason)},
        )
        return
    state = outcome.state()
    status = _STATE_STATUS.get(state, "failed")
    message = _state_message(outcome, holds=unit.holds)
    if state not in NON_FAILURE_STATES and state != "failed" and message:
        # A source that fetched and produced nothing has not raised, so nothing
        # has counted an error for it yet - unlike ``failed``, whose error the
        # page loop charged when the page failed.  It counts as one now: "done, 0
        # records, 0 errors" is the shape this whole failure hid behind.
        # ``no_matches`` is excluded: the source answered and said it holds
        # nothing, so charging it an error would contradict its own "done".
        # ``blocked`` and ``gone`` are excluded for the same reason - they are
        # answers, not faults - and they are counted in columns of their own.
        repo.bump_plan_item(unit.id, errors=1)
        outcome.failed += 1
        stats.errors += 1
        stats.adapter(unit.adapter_key)["errors"] += 1
    _tell_the_registry(unit, outcome, state)
    caps_payload = dict(unit.item.get("caps") or {})
    caps_payload["outcome"] = outcome.to_dict()
    reason = outcome.block_reason if state == "blocked" else (
        outcome.gone_reason if state == "gone" else message
    )
    # FR-361: the settle pass runs once, after the whole plan has finished, so
    # what it writes here is hours behind the feed for most of a run.  It is
    # worth a line only when it *changes* the answer - a source that succeeded
    # and was then capped - and never when it repeats one the reader has read.
    changed = (
        {"activity_at": utcnow(), "activity_kind": state}
        if state != unit.activity_kind
        else {}
    )
    repo.update_plan_item(
        unit.id,
        {
            "status": status,
            "last_error": message,
            "caps": caps_payload,
            **_outcome_columns(unit, state, reason),
            **changed,
        },
    )


runner.register_worker(JOB_KIND, collection_worker)


# ---------------------------------------------------------------------------
# Control surface (FR-185)
# ---------------------------------------------------------------------------


async def launch(campaign_id: str, job_seeker_id: str) -> str:
    """Start collection for a campaign as a background job.  Returns the job id."""
    campaign = repo.get_campaign(campaign_id, job_seeker_id)
    if campaign is None:
        raise LookupError(f"No campaign {campaign_id} for this job seeker")
    items = [
        i
        for i in repo.list_plan_items(campaign_id, include_excluded=False)
        if i["status"] in ("planned", "running", "failed")
    ]
    if not items:
        raise ValueError("Nothing to collect: generate a plan first, or all sources are excluded")

    caps = Caps.from_campaign(campaign)
    # A first estimate for the progress bar; the worker settles the real
    # denominator once it knows which sources are runnable.  Reading the
    # catalogue's capabilities here keeps the two from disagreeing by a factor
    # of the ATS sources, which do not paginate at all.
    catalogue = {c["adapter_key"]: c for c in repo.list_catalogue(enabled_only=False)}
    admin_cache: dict[str, dict] = {}
    total = sum(
        _pages_for(
            i,
            caps,
            catalogue.get(i["adapter_key"], {}).get("query_capabilities") or {},
            admin_cache,
        )
        for i in items
    )
    job_id = runner.create(
        JOB_KIND,
        campaign_id=campaign_id,
        job_seeker_id=job_seeker_id,
        total=total,
        estimated_seconds=sum(int(i["estimated_seconds"] or 0) for i in items),
    )
    await runner.start(job_id, collection_worker)
    repo.set_stage(campaign_id, "collection", "running")
    repo.record_audit(
        "campaign.collection_started",
        job_seeker_id=job_seeker_id,
        entity_type="campaign",
        entity_id=campaign_id,
        detail={"job_id": job_id, "plan_items": len(items), "caps": caps.to_dict()},
    )
    return job_id


def _current_job(campaign_id: str) -> dict | None:
    return repo.latest_job(campaign_id, JOB_KIND)


def pause(campaign_id: str) -> dict:
    job = _current_job(campaign_id)
    ok = bool(job) and runner.pause(job["id"])
    if ok:
        repo.set_stage(campaign_id, "collection", "paused")
    return {"paused": ok, "job_id": (job or {}).get("id")}


def resume(campaign_id: str) -> dict:
    job = _current_job(campaign_id)
    ok = bool(job) and runner.resume(job["id"])
    if ok:
        repo.set_stage(campaign_id, "collection", "running")
    return {"resumed": ok, "job_id": (job or {}).get("id")}


def cancel(campaign_id: str) -> dict:
    job = _current_job(campaign_id)
    ok = bool(job) and runner.cancel(job["id"])
    repo.set_stage(campaign_id, "collection", "cancelled")
    return {"cancelled": bool(job), "was_running": ok, "job_id": (job or {}).get("id")}


async def resume_job(campaign_id: str, job_seeker_id: str) -> str:
    """Restart a job that a crash or a restart left unfinished (NFR-401)."""
    job = _current_job(campaign_id)
    resumable = job and job["status"] in ("pending", "paused", "running")
    if resumable and not runner.is_running(job["id"]):
        await runner.start(job["id"], collection_worker)
        repo.set_stage(campaign_id, "collection", "running")
        return job["id"]
    return await launch(campaign_id, job_seeker_id)


# ---------------------------------------------------------------------------
# Dashboard (FR-185, FR-361)
# ---------------------------------------------------------------------------


#: How many per-source rows one status poll carries by default.  The installed
#: database held 84,041 ``source_plan_item`` rows, and embedding one row per item
#: made a ~7 MB response that the dashboard asked for every four seconds: 6,749
#: calls delivered ~46 GB, 2,580 of them slower than a second.  The per-adapter
#: aggregates below stay exact whatever page is sent, and the full list is
#: reachable through ``sources_limit``/``sources_offset``.
DEFAULT_SOURCES_LIMIT = 100
#: Upper bound on a caller-requested page, so ``?sources_limit=999999999`` cannot
#: rebuild the payload this exists to remove.
MAX_SOURCES_LIMIT = 2000

#: Longest error/reason string one per-source row carries.  A row's error is a
#: one-line summary and a tooltip; 2,000-character stack traces are not what a
#: status poll is for.
SOURCE_TEXT_LIMIT = 400


def _compact_source(item: dict, entry: dict) -> dict:
    """One per-source row carrying only what the dashboard renders.

    The plan item's raw query, ``caps`` blob and rationale stay behind: they are
    never read from the status payload, and repeating them per row is what made
    the response scale with the plan.  Long error text is clipped, not dropped -
    the operator still sees the failure, at tooltip length.
    """
    item_caps = item.get("caps") if isinstance(item.get("caps"), dict) else {}
    outcome = (item_caps or {}).get("outcome") or {}
    label, label_detail = planning.plan_item_label(
        item["adapter_key"], item.get("native_query")
    )
    return {
        "plan_item_id": item["id"],
        "adapter_key": item["adapter_key"],
        "display_name": entry.get("display_name") or item["adapter_key"],
        # FR-162: ``display_name`` belongs to the adapter, so a plan with 2,417
        # Personio boards in it draws 2,417 rows all reading "Personio".
        # ``label`` is what this particular item asked for.
        "label": label,
        "label_detail": outcome_rules._shorten(label_detail, SOURCE_TEXT_LIMIT),
        # Two plan items with one target read the identical thing.  The reuse
        # assessment leaves a 'skipped' twin beside 1,426 running items in one
        # campaign, and no label can separate rows that are the same row - so
        # the screen is given the key that says so instead (FR-166, FR-342).
        "target_key": planning.target_key(item["adapter_key"], item.get("native_query")),
        "status": item["status"],
        # FR-185: what the source actually did, not just whether the worker
        # reached the end of its page loop.
        "outcome": outcome.get("state"),
        "requests_issued": outcome.get("requests"),
        # FR-185: how much of what it asked for was turned down, which a source
        # that collected something used to be able to hide.
        "requests_refused": outcome.get("refused"),
        # FR-185: of those, what was a decision, what was a dead target and what
        # actually failed.  The three add up to the refusals, and only the last
        # is an error.
        "requests_blocked": outcome.get("blocked"),
        "requests_gone": outcome.get("gone"),
        "outcome_state": item.get("outcome_state"),
        "outcome_reason": outcome_rules._shorten(item.get("outcome_reason"), SOURCE_TEXT_LIMIT),
        "blocked_count": item.get("blocked_count") or 0,
        "gone_count": item.get("gone_count") or 0,
        "failed_count": item.get("failed_count") or 0,
        "records_dropped": outcome.get("dropped"),
        "excluded_by_user": bool(item["excluded_by_user"]),
        "records_collected": item["records_collected"],
        "error_count": item["error_count"],
        "last_error": outcome_rules._shorten(item.get("last_error"), SOURCE_TEXT_LIMIT),
        "estimated_pages": item["estimated_pages"],
        # FR-185: the two numbers the per-source bar is drawn from.  The screen
        # used to join them from ``/plan``, which returns one page of 100 items,
        # so 6,424 rows of a 6,524-item plan drew their bar against a guess.
        "estimated_seconds": item["estimated_seconds"],
        "records_per_page": (item_caps or {}).get("records_per_page"),
        "extraction_success_rate": entry.get("extraction_success_rate"),
        "activity_at": item.get("activity_at"),
        "activity_kind": item.get("activity_kind"),
    }


def _source_row_key(source: dict) -> str:
    """The key the per-source table collapses duplicate targets on (FR-342).

    Mirrors ``SourceList.rowKey``: the fetch ledger's target key and the label
    the reader sees.  Two rows with the same target *and* the same label are one
    row, so the aggregate's ``targets`` is a count of what the screen draws.
    """
    target = source.get("target_key") or source.get("plan_item_id")
    return f"{target}\u0000{str(source.get('label') or '').lower()}"


def _interleave_by_adapter(sources: list[dict]) -> list[dict]:
    """Round-robin one row per adapter, active rows first inside each.

    A fixed page that simply took the plan's first N rows would name one adapter
    and hide the other seventeen.  Interleaving keeps every adapter represented
    in a small page; within an adapter the running and recently-active rows come
    first, which is what a person watching a run is looking for.
    """
    buckets: dict[str, list[dict]] = {}
    for source in sources:
        buckets.setdefault(source["adapter_key"], []).append(source)
    for rows in buckets.values():
        rows.sort(key=lambda s: s.get("activity_at") or "", reverse=True)
        rows.sort(key=lambda s: 0 if s.get("status") == "running" else 1)
    page: list[dict] = []
    while buckets:
        for adapter in list(buckets):
            rows = buckets[adapter]
            page.append(rows.pop(0))
            if not rows:
                del buckets[adapter]
    return page


def _source_groups(sources: list[dict]) -> list[dict]:
    """Exact per-adapter aggregates, computed over *every* plan item.

    The response carries a bounded page of rows, so the client cannot count the
    groups from what it receives.  These are the numbers the group heads show -
    plan items, distinct targets, records and the status chips - and they are
    correct whatever page ``sources`` happens to be.
    """
    groups: dict[str, dict] = {}
    for source in sources:
        key = source["adapter_key"]
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "adapter_key": key,
                "display_name": source.get("display_name") or key,
                "plan_items": 0,
                "records": 0,
                "counts": {},
                "_targets": set(),
            }
        group["plan_items"] += 1
        group["records"] += source.get("records_collected") or 0
        status = source.get("status") or "planned"
        group["counts"][status] = group["counts"].get(status, 0) + 1
        group["_targets"].add(_source_row_key(source))
    return [
        {
            "adapter_key": group["adapter_key"],
            "display_name": group["display_name"],
            "plan_items": group["plan_items"],
            "targets": len(group["_targets"]),
            "records": group["records"],
            "counts": group["counts"],
        }
        for group in groups.values()
    ]


#: The ``job_run`` columns the dashboard renders.  ``checkpoint`` is deliberately
#: absent: the collection worker keeps every completed plan-item id in it, which
#: measured 159 KB for one campaign and was sent twice (as ``job`` and inside
#: ``jobs``) on every poll.  Only the lightweight ``report`` copy the progress
#: card reads is carried through by :func:`_compact_job`.
_JOB_FIELDS = (
    "id",
    "campaign_id",
    "job_seeker_id",
    "kind",
    "status",
    "adapter_key",
    "progress_done",
    "progress_total",
    "error_count",
    "last_error",
    "estimated_seconds",
    "started_at",
    "finished_at",
    "created_at",
)


def _compact_job(job: dict | None) -> dict | None:
    """A job row without its checkpoint, which is a list of every id it finished."""
    if not job:
        return None
    compact = {key: job.get(key) for key in _JOB_FIELDS}
    checkpoint = job.get("checkpoint")
    if isinstance(checkpoint, str):
        try:
            checkpoint = json.loads(checkpoint)
        except ValueError:
            checkpoint = None
    report = checkpoint.get("report") if isinstance(checkpoint, dict) else None
    if report is not None:
        compact["checkpoint"] = {"report": report}
    return compact


def _status_rev(campaign: dict, items: list[dict], job: dict | None) -> str:
    """A cheap fingerprint of everything the dashboard renders (NFR-401).

    The campaign row has no ``updated_at``, so the revision is built from the
    fields that do move: the campaign's own state, the plan's size and newest
    activity stamp, and the collection job's progress.  Two polls that see the
    same tuple have the same dashboard, so the second can be answered ``304``.
    """
    last_activity = ""
    for item in items:
        activity = item.get("activity_at") or ""
        if activity > last_activity:
            last_activity = activity
    parts = (
        str(campaign.get("status") or ""),
        str(campaign.get("stage") or ""),
        str(campaign.get("started_at") or ""),
        str(campaign.get("finished_at") or ""),
        str(campaign.get("tokens_used") or 0),
        str(campaign.get("cost_eur") or 0),
        str(len(items)),
        last_activity,
        str((job or {}).get("progress_done") or 0),
        str((job or {}).get("progress_total") or ""),
        str((job or {}).get("status") or ""),
        str((job or {}).get("finished_at") or ""),
    )
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def status(
    campaign_id: str,
    job_seeker_id: str,
    *,
    sources_limit: int | None = None,
    sources_offset: int = 0,
) -> dict:
    """Per-adapter progress, errors, cost and remaining time for one campaign.

    The ledger is computed over the whole plan; the ``sources`` list is a page
    of it (:data:`DEFAULT_SOURCES_LIMIT` rows by default) so the poll stays
    cheap on a plan of tens of thousands of items.  ``source_groups`` carries
    the exact per-adapter counts, and ``sources_total`` says how much is not in
    this page.  ``rev`` is the change signal the router turns into an ETag.
    """
    campaign = repo.get_campaign(campaign_id, job_seeker_id)
    if campaign is None:
        raise LookupError(f"No campaign {campaign_id} for this job seeker")
    items = repo.list_plan_items(campaign_id)
    catalogue = {c["adapter_key"]: c for c in repo.list_catalogue(enabled_only=False)}
    jobs = repo.list_jobs(campaign_id)
    job = next((j for j in jobs if j["kind"] == JOB_KIND), None)

    limit = DEFAULT_SOURCES_LIMIT if sources_limit is None else int(sources_limit)
    limit = max(0, min(limit, MAX_SOURCES_LIMIT))
    offset = max(0, int(sources_offset))

    sources = []
    #: NFR-403 is a property of the *adapter*, not of each plan item that used
    #: it: ``extraction_success_rate`` is read from the one catalogue row.
    #: Appending per item said "board.eures (8%)" 228 times, "registry.kbo"
    #: 153 and "website.crawl" 149 - 561 entries naming 6 adapters - and the
    #: banner that renders them buried the failure list this screen exists to
    #: show.  Keyed by adapter, so each broken adapter is named once.
    breakage: dict[str, dict] = {}
    outcome_states: dict[str, int] = {}
    remaining_seconds = 0
    for item in items:
        entry = catalogue.get(item["adapter_key"], {})
        rate = entry.get("extraction_success_rate")
        # A board that is gone and a source we declined are finished, however
        # many pages they were planned for: counting them as time remaining is
        # how an estimate stays wrong for the rest of the campaign (FR-185).
        if item["status"] not in TERMINAL_STATUSES and not item["excluded_by_user"]:
            remaining_seconds += int(item["estimated_seconds"] or 0)
        state = item.get("outcome_state") or None
        if state:
            outcome_states[str(state)] = outcome_states.get(str(state), 0) + 1
        sources.append(_compact_source(item, entry))
        if rate is not None and rate < BREAKAGE_RATE:
            breakage[item["adapter_key"]] = {
                "adapter_key": item["adapter_key"],
                "extraction_success_rate": rate,
            }

    ordered = _interleave_by_adapter(sources)
    page = ordered[offset : offset + limit] if limit else []
    return {
        "campaign_id": campaign_id,
        "status": campaign.get("status"),
        "stage": campaign.get("stage"),
        "caps": Caps.from_campaign(campaign).to_dict(),
        "started_at": campaign.get("started_at"),
        "finished_at": campaign.get("finished_at"),
        # The collection job's progress and the raw query/rationale it carries
        # are small; the job history is bounded by ``list_jobs``'s own limit.
        # ``checkpoint`` is stripped of its completed-id list - see _compact_job.
        "job": _compact_job(job),
        "jobs": [_compact_job(row) for row in jobs],
        "progress": {
            "pages_done": (job or {}).get("progress_done") or 0,
            "pages_total": (job or {}).get("progress_total"),
            "estimated_seconds_remaining": remaining_seconds,
        },
        # A page of the plan, not the plan.  ``source_groups`` and
        # ``sources_total`` carry the exact shape of what was left out.
        "sources": page,
        "sources_total": len(sources),
        "sources_returned": len(page),
        "sources_limit": limit,
        "sources_offset": offset,
        "sources_truncated": offset + len(page) < len(sources),
        "source_groups": _source_groups(sources),
        # How the plan ended, counted by state.  The number an operator is
        # asked to act on is ``outcome_states['failed']``; the rest are
        # expected outcomes and are reported as such rather than as 538
        # collection errors.  Named apart from the API layer's richer
        # ``outcomes`` block, which groups the same items and adds the reasons.
        "outcome_states": outcome_states,
        "outcomes": outcome_rules.collection_outcomes(sources, catalogue),
        "collected": repo.collected_counts(campaign_id),
        "llm": repo.llm_totals(campaign_id),
        "budget": {
            "token_budget": campaign.get("token_budget"),
            "tokens_used": campaign.get("tokens_used"),
            "cost_eur": campaign.get("cost_eur"),
        },
        "adapter_breakage": sorted(breakage.values(), key=lambda r: r["extraction_success_rate"]),
        "rev": _status_rev(campaign, items, job),
    }


def source_rows(
    campaign_id: str,
    job_seeker_id: str,
    *,
    adapter_key: str | None = None,
    query: str = "",
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """The full per-source list, paginated, for the dashboard's table.

    The status poll sends a bounded page to stay cheap; a reader who expands an
    adapter (or searches) fetches the rows from here.  Rows are the same compact
    shape the status page carries, so the two cannot disagree.
    """
    campaign = repo.get_campaign(campaign_id, job_seeker_id)
    if campaign is None:
        raise LookupError(f"No campaign {campaign_id} for this job seeker")
    catalogue = {c["adapter_key"]: c for c in repo.list_catalogue(enabled_only=False)}
    items = repo.list_plan_items(campaign_id)
    rows = [_compact_source(item, catalogue.get(item["adapter_key"], {})) for item in items]
    if adapter_key:
        rows = [row for row in rows if row["adapter_key"] == adapter_key]
    needle = (query or "").strip().lower()
    if needle:
        rows = [
            row
            for row in rows
            if needle
            in f"{row.get('label') or ''} {row.get('label_detail') or ''} "
            f"{row.get('adapter_key') or ''}".lower()
        ]
    limit = max(1, min(int(limit), MAX_SOURCES_LIMIT))
    offset = max(0, int(offset))
    page = rows[offset : offset + limit]
    return {
        "campaign_id": campaign_id,
        "adapter_key": adapter_key,
        "items": page,
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "truncated": offset + len(page) < len(rows),
    }



# ---------------------------------------------------------------------------
# Stage registry (NFR-603)
# ---------------------------------------------------------------------------

StageRunner = Callable[..., "Awaitable[dict] | dict"]

_STAGES: dict[str, dict[str, Any]] = {}

# Stages owned by other slices.  A slice joins the re-run surface either by
# calling ``register_stage`` at import time, or simply by exposing
# ``rerun(campaign_id, job_seeker_id, **options)`` in one of these modules -
# they are imported lazily, so a module that does not exist yet costs nothing.
_OPTIONAL_STAGES: dict[str, tuple[str, str]] = {
    "profiling": ("dreamjob.pipeline.composite", "rerun"),
    "enrichment": ("dreamjob.pipeline.enrichment", "rerun"),
    "company_profile": ("dreamjob.pipeline.company_profile", "rerun"),
    "financials": ("dreamjob.pipeline.financial", "rerun"),
    "opportunities": ("dreamjob.pipeline.opportunities", "rerun"),
    "speculative": ("dreamjob.pipeline.speculative", "rerun"),
    "scoring": ("dreamjob.pipeline.scoring", "rerun"),
    "generation": ("dreamjob.pipeline.generation", "rerun"),
}

#: What an optional stage does, in the words the re-run screen shows.  A stage
#: with no entry here falls back to naming its module, which is honest but says
#: nothing to a user deciding whether to press the button.
_OPTIONAL_STAGE_DESCRIPTIONS: dict[str, str] = {
    "profiling": "Re-synthesise the composite profile from its sources (FR-121..126)",
    "enrichment": "Re-run enrichment over the collected records (FR-201..207)",
    "company_profile": "Rebuild the company profiles from what is stored (FR-221..225)",
    "financials": "Re-read the filings and re-score ability to pay (FR-241..245)",
    "opportunities": "Re-synthesise opportunities from the collected vacancies (FR-261)",
    "speculative": (
        "Propose unadvertised roles for companies with no matching vacancy (FR-262)"
    ),
    "scoring": "Re-score every opportunity in the campaign (FR-281)",
    "generation": "Regenerate the application documents (FR-301..306)",
}

# Stages whose owning slice already re-runs a whole campaign from its persisted
# artefacts under its own name.  These are the fall-back: a module that grows a
# ``rerun`` of its own takes precedence, because only its author knows what its
# re-run should mean.
_CAMPAIGN_STAGES: dict[str, tuple[str, str, str]] = {
    "opportunities": (
        "dreamjob.pipeline.opportunities",
        "synthesise_campaign",
        "Re-synthesise opportunities from the collected vacancies (FR-261)",
    ),
    "scoring": (
        "dreamjob.pipeline.scoring",
        "score_campaign",
        "Re-score every opportunity in the campaign (FR-281)",
    ),
}


def register_stage(name: str, fn: StageRunner, *, description: str = "") -> None:
    """Make a pipeline stage independently re-runnable (NFR-603)."""
    _STAGES[name] = {"fn": fn, "description": description}


def _load(module_name: str, attribute: str) -> Any | None:
    """Import another slice's entry point, or ``None`` while it does not exist."""
    try:
        return getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError):
        return None


def _campaign_shim(fn: Any) -> StageRunner:
    """Adapt a ``fn(campaign, **options)`` stage to the re-run signature.

    The campaign is loaded with the seeker's own id, so the re-run cannot reach
    another job seeker's campaign (FR-101).
    """

    def run(campaign_id: str, job_seeker_id: str, **options: Any) -> Any:
        campaign = repo.get_campaign(campaign_id, job_seeker_id)
        if campaign is None:
            raise LookupError(f"No campaign {campaign_id} for this job seeker")
        return fn(campaign, **options)

    return run


def _resolve_stage(name: str) -> dict | None:
    if name in _STAGES:
        return _STAGES[name]
    target = _OPTIONAL_STAGES.get(name)
    if target:
        fn = _load(*target)
        if fn is not None:
            description = _OPTIONAL_STAGE_DESCRIPTIONS.get(name) or f"provided by {target[0]}"
            register_stage(name, fn, description=description)
            return _STAGES[name]
    fallback = _CAMPAIGN_STAGES.get(name)
    if fallback:
        module_name, attribute, description = fallback
        fn = _load(module_name, attribute)
        if fn is not None:
            register_stage(name, _campaign_shim(fn), description=description)
            return _STAGES[name]
    return None


def available_stages() -> list[dict]:
    for name in (*_OPTIONAL_STAGES, *_CAMPAIGN_STAGES):
        _resolve_stage(name)
    return [
        {"stage": name, "description": meta["description"]}
        for name, meta in sorted(_STAGES.items())
    ]


async def rerun_stage(stage: str, campaign_id: str, job_seeker_id: str, **options: Any) -> dict:
    """Re-run one stage from what the previous stage persisted (NFR-603)."""
    meta = _resolve_stage(stage)
    if meta is None:
        raise KeyError(f"Unknown pipeline stage {stage!r}")
    repo.record_audit(
        "campaign.stage_rerun",
        job_seeker_id=job_seeker_id,
        entity_type="campaign",
        entity_id=campaign_id,
        detail={"stage": stage, "options": options},
    )
    fn = meta["fn"]
    if inspect.iscoroutinefunction(fn):
        result = await fn(campaign_id, job_seeker_id, **options)
    else:
        # A synchronous stage (planning calls the LLM) runs off the event loop
        # so a re-run never blocks the collection jobs already in flight.
        result = await asyncio.to_thread(fn, campaign_id, job_seeker_id, **options)
    return _as_dict(stage, result)


def _as_dict(stage: str, result: Any) -> dict:
    """Every slice reports its own way; the API contract is a JSON object."""
    if isinstance(result, dict):
        return result
    for method in ("as_dict", "to_dict"):
        rendered = getattr(result, method, None)
        if callable(rendered):
            value = rendered()
            if isinstance(value, dict):
                return value
    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        return dataclasses.asdict(result)
    return {"stage": stage, "result": result}


def _stage_planning(campaign_id: str, job_seeker_id: str, **options: Any) -> dict:
    """Re-plan from the persisted directives, composite profile and dream job model."""
    return planning.generate_plan(campaign_id, job_seeker_id, **options)


def _stage_reuse(campaign_id: str, job_seeker_id: str, **options: Any) -> dict:
    """Re-run the knowledge-base comparison against the persisted plan (FR-342)."""
    campaign = repo.get_campaign(campaign_id, job_seeker_id)
    if campaign is None:
        raise LookupError(f"No campaign {campaign_id} for this job seeker")
    inputs = repo.load_planning_inputs(campaign)
    countries = planning.target_countries(inputs["directives"])
    return knowledge_base.assess_reuse(campaign_id, countries=countries, **options).to_dict()


async def _stage_collection(campaign_id: str, job_seeker_id: str, **options: Any) -> dict:
    """Re-collect from the persisted source plan; counters start clean."""
    if options.get("reset", True):
        repo.reset_plan_progress(campaign_id)
    job_id = await launch(campaign_id, job_seeker_id)
    return {"stage": "collection", "job_id": job_id}


register_stage("planning", _stage_planning, description="Regenerate the source plan (FR-161..166)")
register_stage("reuse", _stage_reuse, description="Re-assess knowledge-base reuse (FR-342)")
register_stage(
    "collection", _stage_collection, description="Re-run collection from the plan (FR-181..186)"
)
