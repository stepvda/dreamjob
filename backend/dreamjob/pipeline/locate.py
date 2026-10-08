"""Place every opportunity on the map, so a list can be cut to a radius (FR-144, FR-283).

Sources state where a job is as free text - "Ghent, Oost-Vlaanderen, Belgium",
"Antwerp / Ghent", "Leuven Materialise", "Bonn oder freie Arbeitsplatzwahl" - and
almost none of them send coordinates.  Placement turns that text, and the
employer's own addresses, into coordinates as part of adding an opportunity:

* synthesis places a new row from what is already known (:func:`place_now`).
  That costs a database read and no request, so the opportunities in a town
  anyone has asked about before are placed the moment they are added;
* the ``geolocate`` job (:func:`locate_pending`), which the scheduler keeps
  going while anything waits, asks the paced, cached geocoder
  (:mod:`dreamjob.pipeline.geocode`) about the rest.  The same job is the
  backfill: an opportunity added before placement existed is simply one that
  has not been decided yet.

For each opportunity, in order:

1. coordinates the posting itself carried are kept (``source``);
2. the town the posting names is found.  When the employer has a street
   address in that town the job is placed there (``address``), otherwise at
   the town (``locality``).  An agency's or a job board's address is the
   intermediary's office, not the job's (NFR-402), so theirs is never used;
3. a posting that names no town - a bare "BE", or nothing - is placed at the
   employer's site when the employer has exactly one (``employer_site``).  A
   remote role is not: it has no office to be at.

What it will not do is guess.  A country or a region is not a place: "Germany"
would land in the middle of the country and "Belgium" thirty kilometres from
Brussels, inside a radius around it by accident.  Those, "Remote job", and an
employer with several sites and no town to choose between them are left
without coordinates, and :func:`coverage` counts them so the screen can say
how many rows a radius filter cannot judge rather than silently dropping them.
A posting that lists several sites is placed at the first one.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from dreamjob.adapters.vacancy_source import country_from_location
from dreamjob.db.connection import from_json, query_all, query_one, utcnow, write_tx
from dreamjob.pipeline import geocode as geo

log = logging.getLogger(__name__)

#: Where an opportunity's coordinates came from (``opportunity.location_precision``).
SOURCE = "source"                # the posting carried them
ADDRESS = "address"              # the employer's street address, in the posting's town
LOCALITY = "locality"            # the town or postcode the posting names
EMPLOYER_SITE = "employer_site"  # no town on the posting: the employer's only site
PRECISIONS = (SOURCE, ADDRESS, LOCALITY, EMPLOYER_SITE)

#: What the geocoder calls a settlement is a place, whatever its rank or size.
#: A city whose boundary is also a state or a district is ranked as the larger
#: thing - Berlin 8, London 10, Munich and Bonn 12 - and Hamburg's takes in
#: islands 100 km out in the North Sea; each is still a city, and its point is
#: the city centre.
_SETTLEMENT_TYPES = frozenset(
    {"city", "town", "village", "hamlet", "municipality", "suburb", "borough",
     "city_district", "quarter", "neighbourhood", "postcode", "locality",
     "isolated_dwelling"}
)
#: Never somewhere anyone works, nor a hint at which town is meant.
_COUNTRY_TYPES = frozenset({"continent", "country"})
#: Any other area - a county, a region, a district - is a place only when it
#: is the size of a town: Würzburg is a county 18 km across, Vlaams-Brabant a
#: province of 100.
CITY_SPAN_KM = 40.0
#: Without a box to measure, ranks below this are a region or a province.
MIN_PLACE_RANK = 13
#: From this rank on an answer is a street or a building rather than a town.
ADDRESS_RANK = 26
#: How many of the geocoder's answers are read for one question.
CANDIDATES = 5
#: An employer's address this close to the town a posting names is where the
#: job is; one further away is another office.
SAME_PLACE_KM = 15.0
#: "No such place" is asked again after this long; the map keeps growing.
UNPLACEABLE_RETRY = timedelta(days=30)
#: An employer with more sites than this in one country is not narrowed down
#: by asking about every one of them.
MAX_SITES = 12
#: Employer roles whose address is an intermediary's office (NFR-402).
NOT_THE_EMPLOYER = ("agency", "board")

JOB_KIND = "geolocate"
BATCH_SIZE = 200
#: After the geocoder stopped answering, or a run failed, the scheduler waits
#: this long before starting the job again.
UNAVAILABLE_BACKOFF = timedelta(minutes=15)

#: Separators between alternative sites in one location string.
_ALTERNATIVES = re.compile(r"\s+/\s+|/|;|\||\s+or\s+|\s+oder\s+|\s+ou\s+", re.IGNORECASE)
#: A location that *starts* by saying it is remote names no office to place.
_REMOTE = re.compile(r"(remote|anywhere|telewerk|t[ée]l[ée]travail|home ?office)\b", re.I)
#: "3 Locations", "Multiple locations": a count of sites, not a site.
_NO_PLACE = re.compile(
    r"^(\d+|multiple|several|various|meerdere|plusieurs)\s+(locations?|sites?|places?|"
    r"vestigingen|lieux)\b", re.I,
)
#: "Brussels (BE)", "President Kennedypark(Kor) 35": asides the geocoder trips on.
_ASIDE = re.compile(r"\([^)]*\)")
#: A Belgian box number ("bus 301", "bte 4") is part of the post, not the place.
_BOX = re.compile(r",?\s*\b(?:bus|bte|boîte|box)\s*\w+", re.I)
_DIGIT = re.compile(r"\d")
_WORD = re.compile(r"[^\W\d_]{3,}")
_POSTCODE = re.compile(r"\b\d{4,5}\b")

#: A geocoder hit at least this prominent (Nominatim ``importance``) is taken
#: as the place meant even when the row's country says otherwise: "Barcelona"
#: on a row whose country came from the employer's Belgian address is the city,
#: not the Belgian street of that name the country-restricted search finds.
PROMINENT_IMPORTANCE = 0.6


def _clean(text: Any) -> str:
    return " ".join(_ASIDE.sub(" ", str(text or "")).split())


def _iso2(value: Any) -> str:
    """ISO-3166 alpha-2 for a country field, or ``""`` when it names none."""
    text = str(value or "").strip()
    if not text:
        return ""
    code = text.upper() if len(text) == 2 else (country_from_location(text) or "")
    return "GB" if code == "UK" else code if re.fullmatch(r"[A-Z]{2}", code) else ""


def place_queries(location: str | None) -> tuple[list[str], list[str]]:
    """The geocoder queries worth trying for one location string, best first.

    Returns ``(queries, single_words)``.  The single words cover a site name
    glued to the town ("Leuven Materialise", "Moore Brussel") and are only
    safe to try inside a known country.  Both lists are empty when the string
    names no single place.
    """
    text = _clean(location)
    first = _ALTERNATIVES.split(text, maxsplit=1)[0].strip(" ,-")
    # A country code ("BE", "DEU") is not a place; a three-letter town
    # ("Mol", "Ath", "Spa") is.
    if len(first) < 3 or (len(first) == 3 and first.isupper()):
        return [], []
    if _REMOTE.match(first) or _NO_PLACE.match(first):
        return [], []
    queries = [first]
    head = first.split(",")[0].strip()
    if head and head != first:
        queries.append(head)
    words = head.split()
    single = [w for w in (words[-1], words[0]) if len(w) >= 3] if len(words) > 1 else []
    return list(dict.fromkeys(queries)), list(dict.fromkeys(single))


def is_area(hit: geo.GeocodeResult) -> bool:
    """Whether a geocoder answer is a country, region or province, not a place."""
    kind = (hit.place_type or "").lower()
    if kind in _SETTLEMENT_TYPES:
        return False
    if kind in _COUNTRY_TYPES:
        return True
    box = hit.bounding_box  # south, north, west, east
    if box:
        return geo.haversine_km(box[0], box[2], box[1], box[3]) > CITY_SPAN_KM
    return hit.place_rank is not None and hit.place_rank < MIN_PLACE_RANK


def _inside(hit: geo.GeocodeResult, area: geo.GeocodeResult) -> bool:
    box = area.bounding_box
    return bool(box) and box[0] <= hit.latitude <= box[1] and box[2] <= hit.longitude <= box[3]


async def _first_place(
    query: str, codes: list[str] | None, client: Any
) -> geo.GeocodeResult | None:
    """The answer that is a place: the geocoder's first, or the town inside it.

    "Deggendorf" and "Bristol" come back first as the county of that name, the
    town second.  A settlement of the same query lying inside that county is
    the town meant; a country is never narrowed this way, or "Germany" would
    find a hamlet that happens to share the name.
    """
    hits = await geo.search(
        query, limit=CANDIDATES, country_codes=codes, egress=client, strict=True
    )
    if not hits:
        return None
    top = hits[0]
    if not is_area(top):
        return top
    if (top.place_type or "").lower() in _COUNTRY_TYPES:
        return None
    return next(
        (
            h for h in hits[1:]
            if (h.place_type or "").lower() in _SETTLEMENT_TYPES and _inside(h, top)
        ),
        None,
    )


async def _resolve(
    location: str, codes: list[str] | None, client: Any
) -> geo.GeocodeResult | None:
    """The coordinates one location string most plausibly means, or ``None``.

    The row's country is a hint, not a fact - it is often the employer's
    country - so the first query is tried unrestricted and kept when it lands
    in that country or is a prominent place; only then is the search narrowed
    to the country, and only then are bare single words tried.  An answer that
    is a whole country or region counts as no answer at every step.  Raises
    :class:`geo.GeocoderUnavailable` when the geocoder does not answer.
    """
    queries, single = place_queries(location)
    if not queries:
        return None
    open_hit = None
    if codes is not None:
        open_hit = await _first_place(queries[0], None, client)
        if open_hit is not None and (
            open_hit.country_code in codes
            or (open_hit.importance or 0.0) >= PROMINENT_IMPORTANCE
        ):
            return open_hit
        queries = queries + single
    for query in queries:
        hit = await _first_place(query, codes, client)
        if hit is not None:
            return hit
    # Nothing of that name in the hinted country: the hint was wrong ("Almere"
    # on a row whose country is the employer's), so the place found is it.
    return open_hit


# ---------------------------------------------------------------------------
# What is known about places
# ---------------------------------------------------------------------------

PLACE = "place"  # a location string from a posting, with its country hint
SITE = "site"    # an employer's address


@dataclass(frozen=True)
class Key:
    """One question for the geocoder."""

    kind: str
    text: str
    country: str

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.country}:{self.text.lower()}"


@dataclass(frozen=True)
class Point:
    latitude: float
    longitude: float
    rank: int | None = None

    @property
    def is_address(self) -> bool:
        return (self.rank or 0) >= ADDRESS_RANK

    def km_to(self, other: Point) -> float:
        return geo.haversine_km(self.latitude, self.longitude, other.latitude, other.longitude)


class _Unasked(Exception):
    """Nobody has asked the geocoder this yet."""

    def __init__(self, key: Key) -> None:
        super().__init__(key.id)
        self.key = key


class Gazetteer:
    """Answers about places, read through from ``geo_place`` once per key."""

    def __init__(self) -> None:
        self._known: dict[str, Point | None] = {}

    def get(self, key: Key) -> Point | None:
        """The answer on record, ``None`` for "no such place".

        Raises :class:`_Unasked` when there is none, or only an old "no".
        """
        if key.id in self._known:
            return self._known[key.id]
        row = query_one(
            "SELECT latitude, longitude, place_rank, resolved_at FROM geo_place "
            "WHERE query_key = ?",
            (key.id,),
        )
        if row is None:
            raise _Unasked(key)
        if row["latitude"] is None:
            if _older_than(row["resolved_at"], UNPLACEABLE_RETRY):
                raise _Unasked(key)
            point = None
        else:
            point = Point(float(row["latitude"]), float(row["longitude"]), row["place_rank"])
        self._known[key.id] = point
        return point

    async def ask(self, key: Key, client: Any) -> None:
        """Put the question to the geocoder and record the answer for everyone."""
        codes = [key.country] if key.country else None
        if key.kind == PLACE:
            hit = await _resolve(key.text, codes, client)
        else:
            hit = await _first_place(key.text, codes, client)
        with write_tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO geo_place (query_key, latitude, longitude, "
                "place_type, place_rank, country_code, display_name, resolved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key.id,
                    hit.latitude if hit else None,
                    hit.longitude if hit else None,
                    hit.place_type if hit else None,
                    hit.place_rank if hit else None,
                    hit.country_code if hit else None,
                    hit.display_name if hit else None,
                    utcnow(),
                ),
            )
        self._known[key.id] = Point(hit.latitude, hit.longitude, hit.place_rank) if hit else None


def _older_than(stamp: Any, age: timedelta) -> bool:
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return datetime.now(UTC) - moment > age


# ---------------------------------------------------------------------------
# The employer's sites
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Site:
    """One address an employer gave, as a question for the geocoder."""

    text: str
    country: str
    #: It names a street, so it can be more exact than the town.
    street: bool
    #: A street with no town ("Steenhouwersvest 11", as older register reads
    #: kept it): only usable next to the town the posting names, which is
    #: what says which Steenhouwersvest it is.
    needs_town: bool

    def key(self, town: str = "") -> Key:
        return Key(SITE, f"{self.text}, {town}" if self.needs_town else self.text, self.country)


#: Keys a structured address uses, in the order they are written on an envelope.
_ADDRESS_PARTS = (
    "street1", "street", "line1", "addressLine1", "street2", "zipcode", "postcode",
    "postalCode", "city",
)


def _address_text(value: Any) -> str:
    """An address as one line, also when a source stored it as a record."""
    if isinstance(value, dict):
        return ", ".join(str(value[k]) for k in _ADDRESS_PARTS if value.get(k))
    return str(value or "")


def employer_sites(company: dict | None) -> list[Site]:
    """The addresses on a company record, in any of the shapes its sources write."""
    if not company:
        return []
    raw = company.get("locations")
    raw = from_json(raw, None) if isinstance(raw, str) else raw
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    home = _iso2(company.get("country"))
    sites: dict[tuple[str, str], Site] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        street = _BOX.sub("", _clean(_address_text(entry.get("address") or entry.get("label"))))
        town = _clean(entry.get("city") or entry.get("municipality"))
        postcode = _clean(entry.get("postcode"))
        country = _iso2(entry.get("country")) or home
        if _DIGIT.search(street) and _WORD.search(street):
            locality = " ".join(p for p in (postcode, town) if p and p.lower() not in street.lower())
            site = Site(
                f"{street}, {locality}" if locality else street,
                country,
                street=True,
                needs_town=not (town or postcode or _POSTCODE.search(street)),
            )
        elif town:
            # "Global headquarters" in New York: the town, nothing finer.
            site = Site(" ".join(p for p in (postcode, town) if p), country, False, False)
        else:
            continue
        sites.setdefault((site.text.lower(), site.country), site)
    return list(sites.values())


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Placement:
    latitude: float | None = None
    longitude: float | None = None
    precision: str | None = None

    @classmethod
    def at(cls, point: Point, precision: str) -> Placement:
        return cls(point.latitude, point.longitude, precision)


UNPLACED = Placement()


def decide(
    row: dict, company: dict | None, role: str | None, known: Callable[[Key], Point | None]
) -> Placement:
    """Where one opportunity is, from what ``known`` can say about places.

    ``known`` raises :class:`_Unasked` for a question nobody has put to the
    geocoder yet, so the same decision serves synthesis, which only reads, and
    the job, which asks and decides again.
    """
    country = _iso2(row.get("country"))
    sites = [
        s for s in (employer_sites(company) if role not in NOT_THE_EMPLOYER else [])
        if not country or not s.country or s.country == country
    ]
    queries, _ = place_queries(row.get("location"))
    town = known(Key(PLACE, _clean(row.get("location")), country)) if queries else None

    if town is not None:
        best: Point | None = None
        for site in [s for s in sites if s.street][:MAX_SITES]:
            point = known(site.key(queries[-1]))
            if point is None or not point.is_address or point.km_to(town) > SAME_PLACE_KM:
                continue
            if best is None or point.km_to(town) < best.km_to(town):
                best = point
        return Placement.at(best, ADDRESS) if best else Placement.at(town, LOCALITY)

    if str(row.get("work_arrangement") or "").lower() == "remote" or not sites:
        return UNPLACED
    # No town on the posting: the employer's site, if everything it lists is
    # one place.  An address that needs a town to be found cannot be checked,
    # so an employer that has one is not known to be in one place.
    if len(sites) > MAX_SITES or any(s.needs_town for s in sites):
        return UNPLACED
    points = []
    for site in sites:
        point = known(site.key())
        if point is None:
            return UNPLACED
        points.append(point)
    anchor = max(points, key=lambda p: p.rank or 0)
    if any(p.km_to(anchor) > SAME_PLACE_KM for p in points):
        return UNPLACED
    return Placement.at(anchor, EMPLOYER_SITE)


def _role(company_id: Any) -> str | None:
    if not company_id:
        return None
    row = query_one(
        "SELECT employer_role FROM company_employer_kind WHERE company_id = ?", (company_id,)
    )
    return str(row["employer_role"]) if row else None


def place_now(record: dict, company: dict | None) -> dict:
    """Place an opportunity being added or refreshed, from what is known (FR-144).

    No request is made, so synthesis is not slowed to the geocoder's pace: when
    every answer the decision needs is on record the row is placed and
    decided; otherwise it is left for the ``geolocate`` job, placed at its town
    meanwhile when that much is known.  Fills ``latitude``, ``longitude``,
    ``location_precision`` and ``located_at`` on ``record`` and returns it.
    """
    if record.get("latitude") is not None and record.get("longitude") is not None:
        record.update(location_precision=SOURCE, located_at=utcnow())
        return record
    record.update(latitude=None, longitude=None, location_precision=None, located_at=None)
    role = _role(record.get("company_id")) if employer_sites(company) else None
    gazetteer = Gazetteer()
    try:
        placement = decide(record, company, role, gazetteer.get)
    except _Unasked:
        town = _known_town(record, gazetteer)
        if town is not None:
            record.update(
                latitude=town.latitude, longitude=town.longitude, location_precision=LOCALITY
            )
        return record
    record.update(
        latitude=placement.latitude,
        longitude=placement.longitude,
        location_precision=placement.precision,
        located_at=utcnow(),
    )
    return record


def _known_town(record: dict, gazetteer: Gazetteer) -> Point | None:
    if not place_queries(record.get("location"))[0]:
        return None
    key = Key(PLACE, _clean(record.get("location")), _iso2(record.get("country")))
    try:
        return gazetteer.get(key)
    except _Unasked:
        return None


# ---------------------------------------------------------------------------
# The job: everything not decided yet, newest first
# ---------------------------------------------------------------------------


def pending_count() -> int:
    row = query_one("SELECT COUNT(*) AS n FROM opportunity WHERE located_at IS NULL")
    return int((row or {}).get("n") or 0)


def _pending(limit: int) -> list[dict[str, Any]]:
    return query_all(
        "SELECT id, location, country, company_id, work_arrangement, latitude, longitude, "
        "location_precision FROM opportunity WHERE located_at IS NULL "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )


def _load_employers(
    rows: list[dict], companies: dict[str, dict | None], roles: dict[str, str | None]
) -> None:
    wanted = sorted({str(r["company_id"]) for r in rows if r.get("company_id")} - set(companies))
    if not wanted:
        return
    marks = ", ".join("?" for _ in wanted)
    found = {
        str(r["id"]): r
        for r in query_all(
            f"SELECT id, country, locations FROM company WHERE id IN ({marks})", tuple(wanted)
        )
    }
    verdicts = {
        str(r["company_id"]): str(r["employer_role"])
        for r in query_all(
            f"SELECT company_id, employer_role FROM company_employer_kind "
            f"WHERE company_id IN ({marks})",
            tuple(wanted),
        )
    }
    for company_id in wanted:
        companies[company_id] = found.get(company_id)
        roles[company_id] = verdicts.get(company_id)


async def _decide_asking(
    row: dict,
    company: dict | None,
    role: str | None,
    gazetteer: Gazetteer,
    client: Any,
    report: dict[str, Any],
) -> Placement:
    if row.get("latitude") is not None and row.get("location_precision") in (None, SOURCE):
        return Placement(row["latitude"], row["longitude"], SOURCE)
    while True:
        try:
            return decide(row, company, role, gazetteer.get)
        except _Unasked as unasked:
            await gazetteer.ask(unasked.key, client)
            report["asked"] += 1


def _store(decided: list[tuple[dict, Placement]]) -> None:
    """Write the decisions, unless the row changed under us.

    A refresh that moved the row (a new location, another employer) put it
    back in the queue with nothing decided; this decision is about the old
    place and must not land on it.
    """
    if not decided:
        return
    now = utcnow()
    with write_tx() as conn:
        conn.executemany(
            "UPDATE opportunity SET latitude = ?, longitude = ?, location_precision = ?, "
            "located_at = ? WHERE id = ? AND located_at IS NULL "
            "AND location IS ? AND company_id IS ?",
            [
                (p.latitude, p.longitude, p.precision, now, r["id"], r["location"],
                 r["company_id"])
                for r, p in decided
            ],
        )


async def locate_pending(
    *,
    progress: Callable[[int, int], None] | None = None,
    barrier: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Place every opportunity not decided yet, across all seekers.

    Runs until the queue is empty, including rows added while it runs, newest
    first so that what synthesis just added does not wait behind the backlog.
    Stops early, leaving the rest queued, when the geocoder stops answering.
    """
    report: dict[str, Any] = {"decided": 0, "unplaced": 0, "asked": 0}
    report.update({p: 0 for p in PRECISIONS})
    total = pending_count()
    gazetteer = Gazetteer()
    companies: dict[str, dict | None] = {}
    roles: dict[str, str | None] = {}
    try:
        async with geo._geocoder_client() as client:
            while rows := _pending(BATCH_SIZE):
                _load_employers(rows, companies, roles)
                decided: list[tuple[dict, Placement]] = []
                try:
                    for row in rows:
                        company_id = str(row["company_id"]) if row.get("company_id") else ""
                        placement = await _decide_asking(
                            row, companies.get(company_id), roles.get(company_id),
                            gazetteer, client, report,
                        )
                        decided.append((row, placement))
                        report["decided"] += 1
                        report[placement.precision or "unplaced"] += 1
                finally:
                    _store(decided)
                if progress is not None:
                    progress(report["decided"], max(total, report["decided"]))
                if barrier is not None:
                    await barrier()
    except geo.GeocoderUnavailable as exc:
        report["unavailable"] = str(exc)[:200]
        log.warning("Placement paused: the geocoder is not answering (%s)", exc)
    log.info("Placed opportunities: %s", report)
    return report


async def geolocate_worker(ctx: Any) -> None:
    report = await locate_pending(progress=ctx.progress, barrier=ctx.checkpoint_barrier)
    ctx.save_checkpoint(report=report)


def register_geolocate_worker() -> None:
    """Wire the worker into the job runner, so a restart can resume it (NFR-401)."""
    from dreamjob.jobs.runner import runner  # noqa: PLC0415 - avoids a cycle

    runner.register_worker(JOB_KIND, geolocate_worker)


async def ensure_running() -> dict[str, Any]:
    """Keep the ``geolocate`` job going while anything waits to be placed.

    Called by the scheduler.  Starts the job when rows wait and none is
    active, carries on with one a restart left behind, and leaves the geocoder
    alone for a while after it stopped answering.
    """
    from dreamjob.jobs.runner import runner  # noqa: PLC0415 - avoids a cycle

    waiting = await asyncio.to_thread(pending_count)
    if not waiting:
        return {"pending": 0}
    active = await asyncio.to_thread(
        query_one,
        "SELECT id, status FROM job_run WHERE kind = ? "
        "AND status IN ('pending', 'running', 'paused') ORDER BY created_at DESC LIMIT 1",
        (JOB_KIND,),
    )
    if active is not None:
        if runner.is_running(active["id"]) or active["status"] == "paused":
            return {"pending": waiting, "running": active["id"]}
        await runner.start(active["id"])
        return {"pending": waiting, "resumed": active["id"]}
    last = await asyncio.to_thread(
        query_one,
        "SELECT status, finished_at, checkpoint FROM job_run WHERE kind = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (JOB_KIND,),
    )
    if last is not None and not _older_than(last["finished_at"], UNAVAILABLE_BACKOFF):
        report = (from_json(last["checkpoint"], {}) or {}).get("report") or {}
        if report.get("unavailable"):
            return {"pending": waiting, "waiting": "the geocoder was not answering"}
        # A run that failed outright would fail the same way two minutes later.
        if last["status"] == "failed":
            return {"pending": waiting, "waiting": "the last run failed"}
    job_id = await asyncio.to_thread(runner.create, JOB_KIND, total=waiting)
    await runner.start(job_id)
    return {"pending": waiting, "started": job_id}


def coverage(job_seeker_id: str) -> dict[str, int]:
    """How many of the seeker's opportunities a radius filter can judge."""
    row = query_one(
        "SELECT COUNT(*) AS total, "
        "  SUM(CASE WHEN latitude IS NOT NULL THEN 1 ELSE 0 END) AS located, "
        "  SUM(CASE WHEN latitude IS NULL AND located_at IS NULL THEN 1 ELSE 0 END) AS pending, "
        "  SUM(CASE WHEN latitude IS NULL AND located_at IS NOT NULL "
        "      AND IFNULL(work_arrangement, '') = 'remote' THEN 1 ELSE 0 END) AS remote "
        "FROM opportunity WHERE job_seeker_id = ?",
        (job_seeker_id,),
    ) or {}
    total = int(row.get("total") or 0)
    located = int(row.get("located") or 0)
    pending = int(row.get("pending") or 0)
    remote = int(row.get("remote") or 0)
    return {
        "total": total,
        "located": located,
        "pending": pending,
        "remote_without_place": remote,
        "unplaced": max(total - located - pending - remote, 0),
    }


register_geolocate_worker()
