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
* an adapter that is missing, disabled or blocked by robots.txt fails its own
  plan item and nothing else (IR-101, FR-182).

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

from dreamjob.adapters.base import PlanItem, get_adapter
from dreamjob.config import get_settings
from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import campaigns as repo
from dreamjob.db.repositories import hygiene as hygiene_repo
from dreamjob.db.repositories import knowledge as kb_repo
from dreamjob.egress.client import EgressClient, FetchResult, RobotsDisallowed
from dreamjob.jobs.runner import JobCancelled, JobContext, runner
from dreamjob.pipeline import knowledge_base, planning


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
        self.refusals: list[str] = []

    def __getattr__(self, name: str) -> Any:
        # Everything not about accounting belongs to the shared client.
        return getattr(self._client, name)

    async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        self.requests += 1
        try:
            result = await self._client.fetch(url, **kwargs)
        except RobotsDisallowed as exc:
            self.refusals.append(f"{url}: robots.txt disallows this source (FR-182): {exc}")
            raise
        if not result.ok:
            self.refusals.append(f"{url}: HTTP {result.status_code}")
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
    records: int = 0
    errors: int = 0
    companies: set[str] = field(default_factory=set)
    people: set[str] = field(default_factory=set)
    by_adapter: dict[str, dict[str, int]] = field(default_factory=dict)
    stopped_by: str | None = None

    def adapter(self, key: str) -> dict[str, int]:
        return self.by_adapter.setdefault(key, {"pages": 0, "records": 0, "errors": 0})

    def to_dict(self) -> dict:
        return {
            "pages": self.pages,
            "records": self.records,
            "errors": self.errors,
            "companies": len(self.companies),
            "people": len(self.people),
            "by_adapter": self.by_adapter,
            "stopped_by": self.stopped_by,
        }


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
    else:
        admin = admin_cache.setdefault(key, administrator_caps(key))
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
    errors: int = 0
    refused: int = 0             # requests the server or robots.txt turned down
    blocked: bool = False        # robots.txt refused the source (FR-182)
    last_error: str | None = None

    @property
    def written(self) -> int:
        return self.created + self.updated

    @property
    def extraction_rate(self) -> float | None:
        """NFR-403: pages that yielded a record over pages that were fetched.

        Measured at the fetch boundary, not inside the parser, so an adapter
        that returns nothing at all is visible.  The old rate was computed per
        raw record, so an adapter with zero raw records had no rate - the
        detector could only see sources that were already working.
        """
        if not self.pages:
            return None
        return self.productive_pages / self.pages

    def state(self) -> str:
        """One of five distinct answers, never folded into "done"."""
        if self.written:
            return "succeeded"
        if self.errors:
            return "failed"
        if self.dropped:
            return "rejected"
        if self.parsed or self.normalised:
            return "normalised_nothing"
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
            "blocked_by_robots": self.blocked,
            "extraction_rate": self.extraction_rate,
        }


# How each measured state is written into ``source_plan_item.status``.  Only
# "succeeded" is ``done``: a source that fetched nothing, was refused, or
# produced records the knowledge base would not take has not done its job, and
# recording that as ``done`` is what hid this failure for months.
_STATE_STATUS: dict[str, str] = {
    "succeeded": "done",
    "failed": "failed",
    "rejected": "failed",
    "normalised_nothing": "failed",
    "extracted_nothing": "failed",
    "no_work": "skipped",
}


def _state_message(outcome: ItemOutcome) -> str | None:
    state = outcome.state()
    if state == "succeeded":
        # A source that collected records and was refused some of what it asked
        # for has not fully succeeded, and the refusal outlives the run that saw
        # it rather than being cleared with the previous run's errors (FR-185).
        return outcome.last_error if outcome.refused else None
    if state == "failed":
        return outcome.last_error
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
    #: Companies this item collected for, whether it created them or found them.
    company_ids: set[str] = field(default_factory=set)
    #: The board's company, resolved once per item rather than once per posting.
    board_company_id: str | None = None
    board_company_resolved: bool = False

    @property
    def id(self) -> str:
        return str(self.item["id"])

    @property
    def adapter_key(self) -> str:
        return str(self.item["adapter_key"])

    @property
    def remaining(self) -> int:
        return max(0, self.pages - self.done)


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


async def _run_page(unit: _Unit, page: int) -> ItemOutcome:
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
        step.requests = int(getattr(egress, "requests", 0)) - requests_before
        step.refused = len(getattr(egress, "refusals", ()) or ()) - refusals_before

    attempts_before, successes_before = _extraction_counters(unit.adapter)
    failures_before = len(getattr(unit.writer, "failures", ()) or ())
    try:
        records = await unit.adapter.run(plan_item)
    except RobotsDisallowed as exc:
        measure()
        step.blocked = True
        step.errors = 1
        step.last_error = f"robots.txt disallows this source (FR-182): {exc}"
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
    step.normalised = len(records)
    _link_board_company(unit, records)
    written = unit.writer.write_many(records)
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
        detail = (getattr(egress, "refusals", None) or ["no detail recorded"])[-1]
        if not step.dropped:
            step.last_error = (
                f"{step.refused} request(s) refused - {detail}"
                if step.normalised
                else f"{step.refused} request(s) refused and nothing collected - {detail}"
            )
        if not step.normalised:
            # Nothing survived the refusal, so this page is a failed page.
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
    total.blocked = total.blocked or step.blocked
    if step.last_error:
        total.last_error = step.last_error


async def collection_worker(ctx: JobContext) -> None:
    """Execute every active plan item of a campaign, page by page (FR-181)."""
    campaign_id = ctx.campaign_id or ""
    campaign = repo.get_campaign_any(campaign_id)
    if campaign is None:
        raise LookupError(f"No campaign {campaign_id}")

    caps = Caps.from_campaign(campaign)
    stats = CollectionStats()
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
                _build_units(items, catalogue, egress, campaign_id, caps, completed, admin_caps)
            )
            # FR-186: the denominator is settled once, after the sources with no
            # work have been taken out, so the bar cannot end at "1 of 8" on a
            # run in which seven of the eight sources were never runnable.
            total_pages = stats.pages + sum(u.remaining for u in units)
            ctx.progress(stats.pages, total_pages)

            # FR-186: every runnable source is guaranteed a floor of the budget
            # before any source is allowed to take more than its share, so an
            # exhausted budget shortens the run instead of deleting its tail.
            floor = max(1, caps.max_pages // max(1, len(units))) if units else 1
            harvested = False
            # The same 20 slots the egress layer already bounds itself by
            # (DREAMJOB_HTTP_MAX_CONCURRENCY), spent on buckets rather than
            # left unused by a serial loop.
            concurrency = int(getattr(egress.settings, "http_max_concurrency", 20) or 20)

            def harvest_now() -> int:
                """Plan the boards this run's own discovery found (FR-181)."""
                nonlocal harvested
                harvested = True
                units.extend(
                    _expand_harvest_stage(campaign_id, catalogue, egress, items, units,
                                          caps, admin_caps,
                                          budget=max(0, caps.max_pages - stats.pages))
                )
                return stats.pages + sum(u.remaining for u in units)

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
                        total_pages = harvest_now()
                        ctx.progress(stats.pages, total_pages)
                        continue
                    ran_stages.add(stage)
                    await _run_wave(
                        [u for u in pending if u.stage == stage],
                        pass_limit, ctx, stats, caps, started, completed,
                        total_pages, concurrency,
                    )
                if stats.stopped_by:
                    break
                if not harvested:
                    # No ATS source was planned - which is the normal shape of a
                    # first campaign, when no company had a known board yet.  The
                    # discovery stages have now run, so ask again.
                    total_pages = harvest_now()
                    ctx.progress(stats.pages, total_pages)

            for unit in units:
                _settle(unit, stats, campaign_id)
    except JobCancelled:
        for unit in units:
            if not unit.running:
                continue
            # NFR-401: an interrupted item is rewound so it is runnable again.
            # Left at 'running' it reads as a source that has been fetching for
            # hours on a job that is not running at all.  Every bucket that was
            # in flight is rewound, not just the one that noticed the cancel.
            repo.update_plan_item(
                unit.id,
                {"status": "planned", "last_error": "cancelled while running; resumable"},
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
        for unit in units:
            if not unit.running:
                continue
            repo.update_plan_item(
                unit.id,
                {"status": "failed", "last_error": f"{type(exc).__name__}: {exc}"},
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
    ctx.save_checkpoint(completed=completed, stats=stats.to_dict(), finished_at=utcnow())


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


def _build_units(
    items: list[dict],
    catalogue: dict[str, dict],
    egress: EgressClient,
    campaign_id: str,
    caps: Caps,
    completed: dict[str, int],
    admin_caps: dict[str, dict],
) -> list[_Unit]:
    """Turn the plan into runnable work, settling what cannot run (IR-101)."""
    units: list[_Unit] = []
    for item in items:
        try:
            unit = _build_unit(item, catalogue, egress, campaign_id, caps, completed, admin_caps)
        except Exception as exc:  # noqa: BLE001 - the item fails, the campaign does not
            # Every decision about one plan item belongs to that plan item.  The
            # skip decision used to sit outside all exception handling, so a
            # malformed native_query took the whole job down with it and left
            # every source looking untouched - status planned, 0 errors.
            log.exception("[%s] could not be prepared for collection", item.get("adapter_key"))
            repo.update_plan_item(
                item["id"], {"status": "failed", "last_error": f"{type(exc).__name__}: {exc}"}
            )
            repo.bump_plan_item(item["id"], errors=1)
            continue
        if unit is not None:
            units.append(unit)
    units.sort(key=lambda u: (u.stage, u.adapter_key))
    return units


def _build_unit(
    item: dict,
    catalogue: dict[str, dict],
    egress: EgressClient,
    campaign_id: str,
    caps: Caps,
    completed: dict[str, int],
    admin_caps: dict[str, dict],
) -> _Unit | None:
    """Prepare one plan item, or settle it and return ``None``."""
    entry = catalogue.get(item["adapter_key"], {})
    # The adapter fetches through this item's own view of the shared client, so
    # its requests and refusals are its own even when other buckets are running.
    view = _UnitEgress(egress)
    adapter = _load_adapter(item["adapter_key"], view)
    if adapter is None:
        repo.update_plan_item(
            item["id"],
            {"status": "skipped", "last_error": "no adapter registered for this source"},
        )
        return None
    # An ATS plan item with no board slug has nothing to read.  It is left
    # runnable rather than skipped: the company whose board it needs may be
    # discovered by this run, and a terminal 'skipped' would never be
    # revisited even once that company exists (FR-162).
    if _is_ats(adapter) and not _has_ats_slug(adapter, item):
        repo.update_plan_item(
            item["id"],
            {
                "status": "planned",
                "last_error": (
                    "no company with a known board for this source yet - run the "
                    "discovery sources first (FR-162)"
                ),
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


def _expand_harvest_stage(
    campaign_id: str,
    catalogue: dict[str, dict],
    egress: EgressClient,
    items: list[dict],
    units: list[_Unit],
    caps: Caps,
    admin_caps: dict[str, dict],
    budget: int,
) -> list[_Unit]:
    """Plan the ATS boards the discovery stages just found (FR-162, FR-181).

    This is the second half of the two-phase order: discovery writes
    ``company.ats_vendor``/``ats_slug``, and the harvest stage reads them back.
    Without it a board found by this run's own website crawl could only be read
    by the *next* campaign, which is why every ATS source was permanently dark.
    """
    covered = {
        (str(row.get("adapter_key")), str((row.get("native_query") or {}).get("slug") or "").lower())
        for row in [*items, *(u.item for u in units)]
    }
    added: list[_Unit] = []
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
    ctx: JobContext,
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
            if unit.remaining <= 0:
                continue
            if slots is None:
                await _run_unit(unit, min(pass_limit, unit.pages), ctx, stats, caps,
                                started, completed, total_pages)
            else:
                async with slots:
                    await _run_unit(unit, min(pass_limit, unit.pages), ctx, stats, caps,
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
    ctx: JobContext,
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
    """
    unit.running = True
    await _drive_unit(unit, limit, ctx, stats, caps, started, completed, total_pages)
    unit.running = False


async def _drive_unit(
    unit: _Unit,
    limit: int,
    ctx: JobContext,
    stats: CollectionStats,
    caps: Caps,
    started: float,
    completed: dict[str, int],
    total_pages: int,
) -> None:
    """Run one source up to ``limit`` pages, recording what it did (FR-181)."""
    if not unit.started:
        unit.started = True
        repo.update_plan_item(unit.id, {"status": "running"})
    bucket = stats.adapter(unit.adapter_key)
    while unit.done < min(limit, unit.pages):
        reason = _cap_hit(stats, caps, started)
        if reason:
            stats.stopped_by = reason
            return
        await ctx.checkpoint_barrier()  # FR-185 pause / cancel
        page = unit.done + 1
        try:
            step = await _run_page(unit, page)
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - the item fails, the run continues
            log.exception("[%s] failed outside the page loop", unit.adapter_key)
            step = ItemOutcome(errors=1, last_error=f"{type(exc).__name__}: {exc}")
        _absorb(unit, step)
        unit.done = page

        if step.errors:
            repo.bump_plan_item(unit.id, errors=step.errors, last_error=step.last_error)
            ctx.record_error(step.last_error or "collection error")
            bucket["errors"] += step.errors
            stats.errors += step.errors
            unit.pages = unit.done  # a failed source is not paged through further
        elif step.refused:
            # The page produced records and part of what it asked for was still
            # refused.  The records are kept and the refusal is counted anyway:
            # a source that half answered is not a source that answered (N7).
            repo.bump_plan_item(unit.id, errors=step.refused, last_error=step.last_error)
            ctx.record_error(step.last_error or "request refused")
            bucket["errors"] += step.refused
            stats.errors += step.refused
        elif not step.pages:
            # The adapter issued no request and returned no material for this
            # query.  Page two will not either, and asking for it would charge
            # the run's budget for work that never happens (FR-186).
            unit.pages = unit.done
        if step.written:
            repo.bump_plan_item(unit.id, records=step.written)
        stats.pages += step.charged
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
        ctx.save_checkpoint(completed=completed, stats=stats.to_dict())
        ctx.progress(stats.pages, total_pages)
        if step.errors:
            return


def _settle(unit: _Unit, stats: CollectionStats, campaign_id: str) -> None:
    """Write down what this source did, in words that distinguish the cases."""
    outcome = unit.outcome
    _record_extraction(unit, campaign_id)
    if not unit.started:
        if unit.remaining <= 0:
            # Every page of this item was already fetched by the run this one
            # resumed (NFR-401): it is finished, not waiting.
            repo.update_plan_item(unit.id, {"status": "done", "last_error": None})
        elif stats.stopped_by:
            repo.update_plan_item(
                unit.id,
                {
                    "status": "planned",
                    "last_error": f"not started: {stats.stopped_by} (FR-186)",
                },
            )
        return
    if stats.stopped_by and unit.remaining > 0 and not outcome.errors:
        # Bounded by a cap, not finished: leave it runnable so that raising the
        # cap continues it from its checkpoint (FR-186).
        repo.update_plan_item(
            unit.id,
            {
                "status": "planned",
                "last_error": f"stopped by cap: {stats.stopped_by} (FR-186)",
            },
        )
        return
    status = _STATE_STATUS.get(outcome.state(), "failed")
    message = _state_message(outcome)
    if outcome.state() not in ("succeeded", "failed") and message:
        # A source that fetched and produced nothing has not raised, so nothing
        # has counted an error for it yet.  It counts as one now: "done, 0
        # records, 0 errors" is the shape this whole failure hid behind.
        repo.bump_plan_item(unit.id, errors=1)
        stats.errors += 1
        stats.adapter(unit.adapter_key)["errors"] += 1
    caps_payload = dict(unit.item.get("caps") or {})
    caps_payload["outcome"] = outcome.to_dict()
    repo.update_plan_item(
        unit.id,
        {"status": status, "last_error": message, "caps": caps_payload},
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


def status(campaign_id: str, job_seeker_id: str) -> dict:
    """Per-adapter progress, errors, cost and remaining time for one campaign."""
    campaign = repo.get_campaign(campaign_id, job_seeker_id)
    if campaign is None:
        raise LookupError(f"No campaign {campaign_id} for this job seeker")
    items = repo.list_plan_items(campaign_id)
    catalogue = {c["adapter_key"]: c for c in repo.list_catalogue(enabled_only=False)}
    jobs = repo.list_jobs(campaign_id)
    job = next((j for j in jobs if j["kind"] == JOB_KIND), None)

    sources = []
    breakage = []
    remaining_seconds = 0
    for item in items:
        entry = catalogue.get(item["adapter_key"], {})
        rate = entry.get("extraction_success_rate")
        if item["status"] not in ("done", "skipped") and not item["excluded_by_user"]:
            remaining_seconds += int(item["estimated_seconds"] or 0)
        item_caps = item.get("caps") if isinstance(item.get("caps"), dict) else {}
        outcome = (item_caps or {}).get("outcome") or {}
        sources.append(
            {
                "plan_item_id": item["id"],
                "adapter_key": item["adapter_key"],
                "display_name": entry.get("display_name") or item["adapter_key"],
                "status": item["status"],
                # FR-185: what the source actually did, not just whether the
                # worker reached the end of its page loop.
                "outcome": outcome.get("state"),
                "requests_issued": outcome.get("requests"),
                # FR-185: how much of what it asked for was turned down, which
                # a source that collected something used to be able to hide.
                "requests_refused": outcome.get("refused"),
                "records_dropped": outcome.get("dropped"),
                "excluded_by_user": bool(item["excluded_by_user"]),
                "records_collected": item["records_collected"],
                "error_count": item["error_count"],
                "last_error": item["last_error"],
                "estimated_pages": item["estimated_pages"],
                "extraction_success_rate": rate,
            }
        )
        if rate is not None and rate < BREAKAGE_RATE:
            breakage.append({"adapter_key": item["adapter_key"], "extraction_success_rate": rate})

    return {
        "campaign_id": campaign_id,
        "status": campaign.get("status"),
        "stage": campaign.get("stage"),
        "caps": Caps.from_campaign(campaign).to_dict(),
        "started_at": campaign.get("started_at"),
        "finished_at": campaign.get("finished_at"),
        "job": job,
        "jobs": jobs,
        "progress": {
            "pages_done": (job or {}).get("progress_done") or 0,
            "pages_total": (job or {}).get("progress_total"),
            "estimated_seconds_remaining": remaining_seconds,
        },
        "sources": sources,
        "collected": repo.collected_counts(campaign_id),
        "llm": repo.llm_totals(campaign_id),
        "budget": {
            "token_budget": campaign.get("token_budget"),
            "tokens_used": campaign.get("tokens_used"),
            "cost_eur": campaign.get("cost_eur"),
        },
        "reuse_report": campaign.get("reuse_report"),
        "adapter_breakage": breakage,
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
    "scoring": ("dreamjob.pipeline.scoring", "rerun"),
    "generation": ("dreamjob.pipeline.generation", "rerun"),
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
            register_stage(name, fn, description=f"provided by {target[0]}")
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
