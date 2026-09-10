"""Discovery: turning a source into the targets it will actually fetch (FR-162, FR-186).

Planning used to translate a *source* into a query and stop there, which meant
every company-scoped source - all seven ATS adapters, the registries, the
website crawler - was planned once, with an empty target list, and then reported
"done, 0 records, 0 errors".  A source is not a unit of work: a board is, a
region-and-sector slice of an aggregator is, a company is.  This module builds
that list of targets, deterministically and before the FR-162 translation step,
so the planner has something concrete to plan *per target* rather than one
speculative item per adapter.

Three properties are deliberate:

* **No LLM call.**  Every target here comes from evidence - a verified board
  registry, the knowledge base, or the job seeker's own directives.  A model
  that invents a board slug produces a 404 that is then stored, re-probed and
  counted (docs/Data_Gathering_Plan.md section 6.7), so discovery is
  deterministic and the planner spends its tokens on the keyword-shaped
  sources that genuinely need translating (FR-162, NFR-104).
* **No fetching.**  Discovery reads files and the knowledge base.  The one
  place it looks at a URL - ``detect_ats`` on a company the seeker named - is
  pure string matching over a URL the seeker supplied; bulk board discovery by
  crawling company websites costs ~30 requests per board found and is
  explicitly not done (section 6.6).
* **Boards travel as boards.**  Board targets are handed to the ATS adapters
  through ``caps["ats"]``, the per-vendor slug list
  :func:`dreamjob.adapters.ats.common.company_targets` already reads, and never
  through ``caps["companies"]``.  A registry board is a route to a vacancy
  list, not a company identity: putting thousands of them in the company
  inventory would have the registry adapters plan a statutory-filing lookup for
  each one, which at 0.5 rps is hours of requests for rows that carry no legal
  identifier at all (section 2.8).

Sources of targets, in the order they are trusted:

1. the job seeker's own directives (FR-143) - a named company, with its board
   resolved from the URL it was given with;
2. the knowledge base - ``company`` rows that already carry
   ``ats_vendor``/``ats_slug``, whether a previous campaign's crawl found them
   or an operator imported them (FR-342, DR-101);
3. the board registry - ``pipeline/data/board_registry.json``, refreshed
   monthly by an operator-run importer, holding one row per live
   (vendor, slug) board;
4. EURES, partitioned by NUTS-1 region x NACE section x publication period,
   because employer density collapses after ~20 pages of any one list.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dreamjob.adapters.ats.detect import detect_ats
from dreamjob.adapters.base import PlanItem
from dreamjob.pipeline.board_registry import without_gone

log = logging.getLogger(__name__)

#: Written by ``scripts/import_board_registry.py`` (operator-run, monthly).  Its
#: absence is normal - a fresh checkout has no registry yet - and means "no
#: registry boards", never an error.
DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent / "data" / "board_registry.json"

#: How many boards one campaign may carry.  Overridden by the campaign's own
#: ``max_companies`` cap (FR-186); the constant is the default that cap holds.
DEFAULT_MAX_BOARDS = 8_000

#: Knowledge-base companies whose board is already known are read in one go
#: rather than through the 200-row planning inventory.
KNOWLEDGE_BASE_BOARD_LIMIT = 5_000

ATS_ADAPTER_PREFIX = "ats."
EURES_ADAPTER_KEY = "board.eures"

# ---------------------------------------------------------------------------
# EURES partitioning (FR-162, FR-186)
# ---------------------------------------------------------------------------
#
# One unpartitioned EURES query returns 205 distinct employers in its first
# 1,000 rows and 4 in its next 300: the deep pages of a single list are the same
# large employers again.  Partitioning by region and sector is what keeps the
# distinct-employer rate at ~20% for every request spent
# (docs/Data_Gathering_Plan.md sections 2.4, 5.2 N2).

#: NUTS-1 regions per country.  Only the countries whose regions the plan
#: measured are listed; anywhere else the country itself is the partition, which
#: is honest (one region) rather than an invented list of codes.
NUTS1_REGIONS: dict[str, tuple[str, ...]] = {
    "BE": ("BE1", "BE2", "BE3"),          # Brussels, Flanders, Wallonia
    "NL": ("NL1", "NL2", "NL3", "NL4"),   # Noord, Oost, West, Zuid
    "LU": ("LU0",),
}

#: **NACE Rev 2.1** sections, which is the vocabulary EURES's sector facet
#: actually uses - not Rev 2, and the difference is not cosmetic.
#:
#: Rev 2.1 splits Rev 2's J into J (publishing, broadcasting, content) and K
#: (telecommunications, computing), so every later letter shifts by one:
#:
#:     N = professional, scientific and technical activities (69-75)
#:     O = administrative and support services (77-82), **including 78,
#:         employment activities - the staffing agencies**
#:     P = public administration
#:
#: The raw search response's ``NACE_CODE`` facet carries 22 letters a-v where
#: Rev 2 has 21 (A-U), which is the tell; and for the 24 employers whose KBO
#: entity resolved, Rev 2.1 letters explain 72% of the sections their rows were
#: fetched under against 18% for Rev 2 (docs/Interim_Agencies_Proposal.md
#: section 2.5).
#:
#: This list used to exclude N "because that is where the staffing agencies
#: sit".  Under the letters EURES uses, that skipped engineering consultancies,
#: law firms and architects - a live Flanders sweep puts 31,609 of 68,190 rows
#: (46%) in N - and removed **zero** agency rows, while the O partition was
#: collected to its 500-record cap and turned out to be 29 agencies out of 36
#: employers.  N is restored here.
#:
#: O is *not* excluded either, and swapping the letters would be the same
#: mistake with better spelling.  A vacancy is filed under every section on its
#: employer's NACE list, so excluding O would lose whole legitimate sectors
#: (cleaning, facility management, security - CleanLease, ISS, Petit
#: Forestier) and would still not remove agencies: NOEL FRANKLIN's 56 rows
#: arrive under C, F, N and O, and 55 of them under C alone.  **Agency
#: membership is a per-employer tag** (``company_employer_kind``, migration
#: 110), established downstream by ``pipeline/employer_resolver.py``, never a
#: partition exclusion.  Refusing to fetch a section is not a way of not
#: employing an agency; it is a way of not seeing an employer.
#:
#: What is left out is left out for yield alone: T (households as employers of
#: domestic personnel), U (extraterritorial bodies) and V (the 22nd letter the
#: Rev 2.1 facet carries and the taxonomy does not name) have no employers a
#: campaign can act on.  BE+NL is therefore 7 regions x 19 sections = 133
#: partitions.
NACE_SECTIONS: tuple[str, ...] = (
    "A", "B", "C", "D", "E", "F", "G", "H", "I", "J",
    "K", "L", "M", "N", "O", "P", "Q", "R", "S",
)
EXCLUDED_NACE_SECTIONS = frozenset({"T", "U", "V"})

#: How much of a partition's page allocation a section gets.  1.0 unless a
#: measurement says otherwise, and there is exactly one such measurement.
#:
#: O holds 59,241 of last week's 68,190 Flanders rows (86%), which makes it the
#: one partition that is not narrow: it behaves like the *unpartitioned* list
#: this whole scheme exists to avoid, and the unpartitioned list runs at 205
#: distinct employers in its first 1,000 rows and 4 in its next 300.  The
#: corpus bears that out - the O partition ran to its 500-record cap and
#: yielded 36 employers, 7% distinct against the ~20% the narrow partitions
#: sustain.  Half the pages is where the marginal request stops buying
#: identities, not a quota on a sector: O still runs, its employers still
#: arrive, and they also arrive under their other sections.  This is a *yield*
#: lever and nothing else - it is not an agency control, it does not know what
#: an agency is, and it must never be used as one.
SECTION_PAGE_WEIGHT: dict[str, float] = {"O": 0.5}

#: Freshness window of one sweep.  Campaign A sweeps LAST_MONTH; a repeat run
#: inside the vacancy staleness window asks for LAST_WEEK instead.
DEFAULT_PUBLICATION_PERIODS: tuple[str, ...] = ("LAST_MONTH",)

#: europa.eu publishes ``Crawl-delay: 10``, which the product honours (section
#: 6.4), so every EURES request costs ten seconds of the campaign's four hours.
#: 600 requests is 100 minutes - the long pole of Campaign A and the budget this
#: sweep is sized to.  Spending it wide (every partition, few pages) rather than
#: deep (few partitions, many pages) is what the density measurements say.
EURES_REQUEST_BUDGET = 600
EURES_MAX_PAGES_PER_PARTITION = 20
EURES_RESULTS_PER_PAGE = 50

#: An adapter that partitions its sweep says so by listing the query keys it
#: reads.  Until the EURES adapter declares them (plan item N2), the planner
#: would be emitting 133 items that all fetch the same country-wide list, so it
#: degrades to one item per country and period instead - fewer targets, but no
#: request is spent twice.
EURES_PARTITION_KEYS: tuple[str, ...] = ("nuts_codes", "nace_section", "publication_period")


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class Discovery:
    """What one campaign has to point its sources at (FR-162)."""

    #: vendor -> board rows, in the shape ``company_targets()`` reads.
    boards: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: adapter_key -> plan items discovery built itself (EURES partitions).
    items: dict[str, list[PlanItem]] = field(default_factory=dict)
    #: Adapters whose queries are evidenced, and so never sent to the model.
    deterministic_keys: set[str] = field(default_factory=set)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def board_count(self) -> int:
        return sum(len(v) for v in self.boards.values())

    @property
    def target_count(self) -> int:
        return self.board_count + sum(len(v) for v in self.items.values())


# ---------------------------------------------------------------------------
# The board registry (section 5.2 N3)
# ---------------------------------------------------------------------------

_registry_cache: dict[str, tuple[float, int, list[dict[str, Any]]]] = {}


def load_board_registry(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Every live board in the registry file, as ``{vendor, slug, name, country}``.

    The file is written by an operator-run importer, not by the app, so it is
    read defensively: a missing file, a partial write or a shape this version
    does not know is "no boards", never a failed plan.  Four shapes are
    accepted, because the importer's own is not yet fixed: a bare list, an
    object with ``boards``, an object with ``vendors``, and a mapping of vendor
    to slug list.
    """
    target = Path(path or DEFAULT_REGISTRY_PATH)
    try:
        stat = target.stat()
    except OSError:
        return []
    cached = _registry_cache.get(str(target))
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("Board registry %s could not be read; planning without it", target)
        return []
    boards = _read_registry(payload)
    _registry_cache[str(target)] = (stat.st_mtime, stat.st_size, boards)
    log.info("Board registry %s: %d live board(s)", target, len(boards))
    return boards


def _read_registry(payload: Any) -> list[dict[str, Any]]:
    rows: list[Any] = []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("boards", "entries", "records", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
            if isinstance(value, dict):
                rows = _from_vendor_map(value)
                break
        else:
            vendors = payload.get("vendors")
            rows = _from_vendor_map(
                vendors if isinstance(vendors, dict) else
                {k: v for k, v in payload.items() if isinstance(v, list)}
            )
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        board = _board_row(row)
        if board is None:
            continue
        key = (board["ats_vendor"], board["slug"].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(board)
    return out


def _from_vendor_map(mapping: dict[Any, Any]) -> list[Any]:
    rows: list[Any] = []
    for vendor, entries in mapping.items():
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, str):
                rows.append({"vendor": vendor, "slug": entry})
            elif isinstance(entry, dict):
                rows.append({"vendor": vendor, **entry})
    return rows


def _board_row(row: Any) -> dict[str, Any] | None:
    """One registry row, or ``None`` when it names no live board."""
    if not isinstance(row, dict):
        return None
    if row.get("live") is False or str(row.get("status") or "").lower() in ("dead", "gone"):
        return None
    vendor = str(row.get("vendor") or row.get("ats_vendor") or "").strip().lower()
    slug = str(row.get("slug") or row.get("ats_slug") or "").strip().strip("/")
    if not vendor or not slug:
        return None
    return {
        "ats_vendor": vendor,
        "slug": slug,
        "name": str(row.get("name") or row.get("company_name") or "").strip(),
        "country": str(row.get("country") or "").strip().upper()[:2],
        "source": str(row.get("source") or "board_registry"),
    }


# ---------------------------------------------------------------------------
# Board targets
# ---------------------------------------------------------------------------


def _vendor_of(company: dict) -> str:
    return str(company.get("ats_vendor") or company.get("vendor") or "").strip().lower()


def _slug_of(company: dict) -> str:
    return str(company.get("ats_slug") or company.get("slug") or "").strip().strip("/")


def company_boards(companies: list[dict]) -> list[dict[str, Any]]:
    """Board targets from company rows (FR-143, FR-342, DR-101).

    A row that already names its vendor and slug is a target as it stands.  A
    row that names neither, but was given with a URL, is read with
    :func:`detect_ats` - a URL that *is* a board (``jobs.lever.co/acme``) names
    its vendor and tenant unambiguously.  Nothing is fetched to find out: a
    company home page has to be crawled to reveal a board it does not link to
    from its URL, which costs ~30 requests per board found and is not how this
    product discovers boards in bulk (section 6.6).
    """
    out: list[dict[str, Any]] = []
    for company in companies:
        if not isinstance(company, dict):
            continue
        vendor, slug = _vendor_of(company), _slug_of(company)
        origin = "company"
        if not (vendor and slug):
            url = str(company.get("careers_url") or company.get("domain") or "").strip()
            if not url:
                continue
            detected_vendor, detected_slug = detect_ats("", url)
            if not (detected_vendor and detected_slug):
                continue
            vendor, slug = detected_vendor.lower(), detected_slug
            origin = "detect_ats"
        out.append(
            {
                "ats_vendor": vendor,
                "slug": slug,
                "name": company.get("name") or company.get("company_name") or "",
                "id": company.get("id") or company.get("company_id"),
                "country": str(company.get("country") or "").strip().upper()[:2],
                "source": origin,
            }
        )
    return out


def _board_sort_key(board: dict[str, Any], wanted: set[str]) -> tuple:
    """Campaign geography first, then unknown, then the rest - then stable.

    Boards are *not* filtered by country: only 900-1,500 of the ~14,600 live
    boards have a Benelux presence, and the company count is the design
    variable, so a BE/NL campaign still reads the EU-heavy vendors whole and
    lets scoring filter (sections 2.3, 6.10).  Ordering by geography only
    decides which boards survive the ``max_companies`` cut.
    """
    country = board.get("country") or ""
    rank = 0 if country and country in wanted else (1 if not country else 2)
    return (rank, board["ats_vendor"], board["slug"].lower())


def board_targets(
    *,
    companies: list[dict],
    registry: list[dict[str, Any]],
    knowledge_base: list[dict] | None = None,
    adapter_keys: set[str],
    countries: list[str] | None = None,
    limit: int = DEFAULT_MAX_BOARDS,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    """Every board this campaign may read, grouped by vendor (FR-162, FR-186).

    Only vendors with a selected, enabled catalogue entry survive: the registry
    holds Workable and Teamtailor boards long before those adapters exist, and a
    plan item for an adapter that is not registered is a plan item that fails
    (IR-101, FR-164).  Named companies and knowledge-base rows come first, so a
    ``max_companies`` cut falls on the registry tail rather than on the company
    the job seeker asked for by name.
    """
    wanted = {c.upper() for c in (countries or [])}
    seen: set[tuple[str, str]] = set()
    boards: dict[str, list[dict[str, Any]]] = {}
    counts = {"directives": 0, "knowledge_base": 0, "registry": 0, "unsupported_vendor": 0}
    total = 0

    ordered_registry = sorted(registry, key=lambda b: _board_sort_key(b, wanted))
    groups = (
        ("directives", company_boards(companies)),
        ("knowledge_base", company_boards(list(knowledge_base or []))),
        ("registry", ordered_registry),
    )
    for origin, group in groups:
        for board in group:
            vendor = board["ats_vendor"]
            if f"{ATS_ADAPTER_PREFIX}{vendor}" not in adapter_keys:
                counts["unsupported_vendor"] += 1
                continue
            key = (vendor, board["slug"].lower())
            if key in seen:
                continue
            if total >= limit:
                break
            seen.add(key)
            total += 1
            counts[origin] += 1
            boards.setdefault(vendor, []).append(
                {
                    "slug": board["slug"],
                    "ats_vendor": vendor,
                    "name": board.get("name") or "",
                    "id": board.get("id"),
                    # FR-166/NFR-402: the plan item says where its target came
                    # from, so a board that 404s can be traced to the registry
                    # row that named it.  Stable across re-plans, so it does not
                    # change the item's identity.
                    "native_query": {"discovered_via": board.get("source") or origin},
                }
            )
    counts["total"] = total
    return boards, counts


# ---------------------------------------------------------------------------
# EURES partitions
# ---------------------------------------------------------------------------


def _regions(countries: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for country in countries:
        code = country.upper()[:2]
        for region in NUTS1_REGIONS.get(code, (code,)):
            out.append((code, region))
    return out


def eures_partitions(
    *,
    countries: list[str],
    caps: dict,
    partitioned: bool = True,
    adapter_key: str = EURES_ADAPTER_KEY,
) -> list[PlanItem]:
    """One plan item per (NUTS-1 region x NACE section x period) (FR-162, FR-186).

    The page budget is spread across every partition rather than concentrated in
    a few: the first 10-20 pages of a partition run at ~20% distinct employers
    and the deep pages of one list run at ~1%, so width buys identities and
    depth buys duplicates (section 2.6).  ``max_pages_per_source`` still bounds
    each item, and the whole sweep is bounded by
    :data:`EURES_REQUEST_BUDGET` requests, which at europa.eu's stated
    ``Crawl-delay: 10`` is 100 minutes of the campaign's four hours.

    One partition is not like the others and is sized accordingly.  O carries
    86% of the region's rows, so its pages behave like the deep pages of an
    unpartitioned list; :data:`SECTION_PAGE_WEIGHT` gives it half the
    allocation, which is the lever the arithmetic actually offers.  Restoring N
    costs 7 partitions and 28 requests at four pages each; halving O gives 14
    of them back, so the corrected sweep is 518 requests against the previous
    504 - 2.3% more, and it stops discarding professional services.  A campaign
    that wants a different shape passes ``eures_section_page_weights``; the
    honest way to spend less on agency-heavy rows is fewer pages there, never
    a section the sweep refuses to look at.
    """
    codes = [c.upper()[:2] for c in countries if c] or ["BE"]
    periods = _periods(caps)
    if partitioned:
        partitions = [
            (country, region, section, period)
            for country, region in _regions(codes)
            for section in NACE_SECTIONS
            if section not in EXCLUDED_NACE_SECTIONS
            for period in periods
        ]
    else:
        # The adapter does not read the partition keys yet, so a partitioned
        # plan would issue the same country-wide search once per section.  One
        # item per country and period is what it can actually execute.
        partitions = [(country, "", "", period) for country in codes for period in periods]
    if not partitions:
        return []

    budget = int(caps.get("eures_request_budget") or EURES_REQUEST_BUDGET)
    ceiling = min(
        EURES_MAX_PAGES_PER_PARTITION,
        max(1, int(caps.get("max_pages_per_source") or EURES_MAX_PAGES_PER_PARTITION)),
    )
    weights = _section_weights(caps)
    # The budget buys "weighted partitions": a section worth half a partition's
    # pages consumes half a partition's share of it.
    unit = budget / max(1e-9, sum(weights.get(p[2], 1.0) for p in partitions))

    items: list[PlanItem] = []
    for country, region, section, period in partitions:
        pages = max(1, min(ceiling, int(unit * weights.get(section, 1.0))))
        native: dict[str, Any] = {
            # ISO-2 is what the adapter searches with today; the NUTS-1 code is
            # the partition it will narrow to once it reads the key (N2).
            "country_codes": [country],
            "pages": pages,
            "results_per_page": EURES_RESULTS_PER_PAGE,
            "language": str(caps.get("language") or "en"),
            # C2: the summary already carries the advert text and the employer,
            # so a detail request per vacancy buys nothing for 50x the requests.
            "fetch_details": False,
            "publication_period": period,
        }
        if region:
            native["nuts_codes"] = [region]
        if section:
            native["nace_section"] = section
        where = region or country
        scope = f"NACE {section}" if section else "all sectors"
        items.append(
            PlanItem(
                adapter_key=adapter_key,
                native_query=native,
                rationale=(
                    f"EURES {where}, {scope}, published {period.replace('_', ' ').lower()}: "
                    "the public employment services' own vacancies, partitioned so each "
                    "request buys new employers instead of deeper pages of the same list"
                ),
                estimated_pages=pages,
                estimated_seconds=pages * 10,   # europa.eu Crawl-delay: 10 (FR-182)
                caps={"max_records": pages * EURES_RESULTS_PER_PAGE},
            )
        )
    return items


def _section_weights(caps: dict) -> dict[str, float]:
    """Per-section page weights, with a campaign's override on top of the default.

    Read defensively, like every other cap: an override that is not a number, or
    is negative, is ignored rather than silently zeroing a section's pages - a
    weight of 0 would be an exclusion wearing a number, and exclusions are the
    thing this module got wrong for a year.  The floor of one page per partition
    in :func:`eures_partitions` enforces that too.
    """
    weights = dict(SECTION_PAGE_WEIGHT)
    stated = caps.get("eures_section_page_weights")
    if isinstance(stated, dict):
        for section, value in stated.items():
            try:
                weight = float(value)
            except (TypeError, ValueError):
                log.warning("Ignoring non-numeric EURES page weight %r=%r", section, value)
                continue
            if weight > 0:
                weights[str(section).upper()[:1]] = weight
    return weights


def _periods(caps: dict) -> tuple[str, ...]:
    stated = caps.get("eures_publication_periods") or caps.get("publication_periods")
    if isinstance(stated, str):
        stated = [stated]
    if isinstance(stated, list):
        periods = tuple(str(p).strip().upper() for p in stated if str(p).strip())
        if periods:
            return periods
    return DEFAULT_PUBLICATION_PERIODS


def reads_partitions(adapter: Any) -> bool:
    """Does this adapter read the partition keys the planner would write?"""
    declared = getattr(adapter, "PARTITION_QUERY_KEYS", ()) or ()
    try:
        return set(EURES_PARTITION_KEYS) <= {str(k) for k in declared}
    except TypeError:
        return False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def discover(
    *,
    selected: list[dict],
    companies: list[dict] | None = None,
    countries: list[str] | None = None,
    caps: dict | None = None,
    adapter_for: Callable[[str], Any] | None = None,
    registry_path: Path | str | None = None,
    knowledge_base_boards: Callable[[], list[dict]] | None = None,
) -> Discovery:
    """Build every fetchable target for the selected sources (FR-162, FR-186).

    Deterministic: given the same registry file, knowledge base and directives
    it returns the same targets in the same order, which is what lets a re-plan
    match its rows to the ones a collected record already points at (FR-166).
    """
    caps = dict(caps or {})
    adapter_keys = {str(e.get("adapter_key") or "") for e in selected}
    found = Discovery()

    if any(key.startswith(ATS_ADAPTER_PREFIX) for key in adapter_keys):
        limit = max(0, int(caps.get("max_companies") or DEFAULT_MAX_BOARDS))
        found.boards, counts = board_targets(
            companies=list(companies or []),
            # The shipped registry file cannot know what this installation's own
            # requests found - it ships ``last_verified: null`` on every row - so the
            # boards this installation has watched answer 404 twice are subtracted
            # here rather than planned, fetched and 404'd again (FR-181, FR-186,
            # FR-343).  One query, not one per board (CR-408).  This is *not* a
            # filter on "verified live": an unverified board has simply never been
            # asked, and 4,515 of 4,518 rows are unverified.
            registry=without_gone(load_board_registry(registry_path)),
            knowledge_base=_knowledge_base_boards(knowledge_base_boards),
            adapter_keys=adapter_keys,
            countries=list(countries or []),
            limit=limit,
        )
        found.stats["boards"] = counts
        found.deterministic_keys |= {
            f"{ATS_ADAPTER_PREFIX}{vendor}" for vendor, rows in found.boards.items() if rows
        }

    if EURES_ADAPTER_KEY in adapter_keys:
        adapter = adapter_for(EURES_ADAPTER_KEY) if adapter_for else None
        partitioned = reads_partitions(adapter) if adapter is not None else False
        items = eures_partitions(
            countries=list(countries or []), caps=caps, partitioned=partitioned
        )
        if items:
            found.items[EURES_ADAPTER_KEY] = items
            found.deterministic_keys.add(EURES_ADAPTER_KEY)
        found.stats["eures"] = {
            "partitions": len(items),
            "pages_each": items[0].estimated_pages if items else 0,
            "partitioned": partitioned,
            "excluded_nace_sections": sorted(EXCLUDED_NACE_SECTIONS),
            # What the sweep will actually spend, so the cost of including a
            # section is visible in the plan rather than only in the clock:
            # every request is ten seconds of the campaign's four hours.
            "requests": sum(i.estimated_pages for i in items),
            "pages_by_section": {
                section: pages
                for section, pages in sorted(
                    {
                        str(i.native_query.get("nace_section") or "-"): i.estimated_pages
                        for i in items
                    }.items()
                )
            },
        }

    found.stats["targets"] = found.target_count
    return found


def _knowledge_base_boards(reader: Callable[[], list[dict]] | None) -> list[dict]:
    """Companies whose board the knowledge base already knows (FR-342).

    Read through the repository, never with SQL of its own, and never fatally:
    a knowledge base that cannot be read is a campaign with fewer targets, not a
    campaign that cannot be planned.
    """
    try:
        if reader is not None:
            return list(reader() or [])
        from dreamjob.db.repositories import campaigns as repo  # noqa: PLC0415 - avoids a cycle

        return list(repo.companies_with_ats_board(limit=KNOWLEDGE_BASE_BOARD_LIMIT) or [])
    except Exception:  # noqa: BLE001 - discovery must survive a missing knowledge base
        log.exception("Could not read known ATS boards from the knowledge base")
        return []
