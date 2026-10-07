"""Place opportunities on the map, so a list can be cut to a radius (FR-144, FR-283).

Sources state where a job is as free text - "Ghent, Oost-Vlaanderen, Belgium",
"Antwerp / Ghent", "Leuven Materialise", "Bonn oder freie Arbeitsplatzwahl" - and
almost none of them send coordinates.  This pass turns each *distinct* place
string into coordinates once, through the paced, cached geocoder
(:mod:`dreamjob.pipeline.geocode`), and writes them onto the opportunity rows
and the shared vacancy rows that carry that string.

What it will not do is guess.  A bare country code ("BE") would geocode to the
middle of the country and pass a 30 km radius around Brussels by accident, and
"Remote job" names no place at all; both are left without coordinates and
counted as *unplaceable*, so the screen can say how many rows a radius filter
cannot judge rather than silently dropping them.  A posting that lists several
sites is placed at the first one.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from dreamjob.db.connection import execute, query_all, query_one
from dreamjob.pipeline import geocode as geo

log = logging.getLogger(__name__)

#: Separators between alternative sites in one location string.
_ALTERNATIVES = re.compile(r"\s+/\s+|/|;|\||\s+or\s+|\s+oder\s+|\s+ou\s+", re.IGNORECASE)
#: A location that *starts* by saying it is remote names no office to place.
_REMOTE = re.compile(r"(remote|anywhere|telewerk|t[ée]l[ée]travail|home ?office)\b", re.I)
#: "3 Locations", "Multiple locations": a count of sites, not a site.
_NO_PLACE = re.compile(
    r"^(\d+|multiple|several|various|meerdere|plusieurs)\s+(locations?|sites?|places?|"
    r"vestigingen|lieux)\b", re.I,
)

#: A geocoder hit at least this prominent (Nominatim ``importance``) is taken
#: as the place meant even when the row's country says otherwise: "Barcelona"
#: on a row whose country came from the employer's Belgian address is the city,
#: not the Belgian street of that name the country-restricted search finds.
PROMINENT_IMPORTANCE = 0.6


def place_queries(location: str | None) -> tuple[list[str], list[str]]:
    """The geocoder queries worth trying for one location string, best first.

    Returns ``(queries, single_words)``.  The single words cover a site name
    glued to the town ("Leuven Materialise", "Moore Brussel") and are only
    safe to try inside a known country.  Both lists are empty when the string
    names no single place.
    """
    text = " ".join(str(location or "").split())
    first = _ALTERNATIVES.split(text, maxsplit=1)[0].strip(" ,")
    if len(first) <= 3 or _REMOTE.match(first) or _NO_PLACE.match(first):
        return [], []
    queries = [first]
    head = first.split(",")[0].strip()
    if head and head != first:
        queries.append(head)
    words = head.split()
    single = [w for w in (words[-1], words[0]) if len(w) >= 3] if len(words) > 1 else []
    return list(dict.fromkeys(queries)), list(dict.fromkeys(single))


def coverage(job_seeker_id: str) -> dict[str, int]:
    """How many of the seeker's opportunities a radius filter can judge."""
    row = query_one(
        "SELECT COUNT(*) AS total, "
        "  SUM(CASE WHEN latitude IS NOT NULL THEN 1 ELSE 0 END) AS located, "
        "  SUM(CASE WHEN latitude IS NULL AND IFNULL(work_arrangement, '') = 'remote' "
        "      THEN 1 ELSE 0 END) AS remote "
        "FROM opportunity WHERE job_seeker_id = ?",
        (job_seeker_id,),
    ) or {}
    total = int(row.get("total") or 0)
    located = int(row.get("located") or 0)
    remote = int(row.get("remote") or 0)
    return {
        "total": total,
        "located": located,
        "remote_without_place": remote,
        "unlocated": max(total - located - remote, 0),
    }


def _pending_places(job_seeker_id: str, limit: int | None) -> list[dict[str, Any]]:
    sql = (
        "SELECT location, UPPER(IFNULL(country, '')) AS country, COUNT(*) AS n "
        "FROM opportunity WHERE job_seeker_id = ? AND latitude IS NULL "
        "AND IFNULL(location, '') <> '' "
        "GROUP BY location, UPPER(IFNULL(country, '')) ORDER BY n DESC"
    )
    params: tuple[Any, ...] = (job_seeker_id,)
    if limit:
        sql += " LIMIT ?"
        params = (*params, int(limit))
    return query_all(sql, params)


def _store(job_seeker_id: str, location: str, country: str, hit: geo.GeocodeResult) -> int:
    changed = execute(
        "UPDATE opportunity SET latitude = ?, longitude = ? "
        "WHERE job_seeker_id = ? AND location = ? AND UPPER(IFNULL(country, '')) = ? "
        "AND latitude IS NULL",
        (hit.latitude, hit.longitude, job_seeker_id, location, country),
    )
    # The vacancy is shared knowledge base; where a job is located is a fact
    # about the posting, and the next synthesis copies it from there.  Only the
    # vacancies behind this seeker's rows are touched: ``vacancy.location`` has
    # no index, and matching on it scanned the whole corpus once per place -
    # about thirty seconds each on a full database.
    execute(
        "UPDATE vacancy SET latitude = ?, longitude = ? "
        "WHERE latitude IS NULL AND id IN ("
        "  SELECT vacancy_id FROM opportunity WHERE job_seeker_id = ? AND location = ? "
        "  AND UPPER(IFNULL(country, '')) = ? AND vacancy_id IS NOT NULL)",
        (hit.latitude, hit.longitude, job_seeker_id, location, country),
    )
    return int(changed or 0)


async def _resolve(
    location: str, codes: list[str] | None, client: Any
) -> geo.GeocodeResult | None:
    """The coordinates one location string most plausibly means, or ``None``.

    The row's country is a hint, not a fact - it is often the employer's
    country - so the first query is tried unrestricted and kept when it lands
    in that country or is a prominent place; only then is the search narrowed
    to the country, and only then are bare single words tried.
    """
    queries, single = place_queries(location)
    if not queries:
        return None
    open_hit = None
    if codes is not None:
        open_hit = await geo.geocode(queries[0], egress=client)
        if open_hit is not None and (
            open_hit.country_code in codes
            or (open_hit.importance or 0.0) >= PROMINENT_IMPORTANCE
        ):
            return open_hit
        queries = queries + single
    for query in queries:
        hit = await geo.geocode(query, country_codes=codes, egress=client)
        if hit is not None:
            return hit
    # Nothing of that name in the hinted country: the hint was wrong ("Almere"
    # on a row whose country is the employer's), so the place found is it.
    return open_hit


async def locate_opportunities(
    job_seeker_id: str, *, limit: int | None = None, progress: Any = None
) -> dict[str, Any]:
    """Geocode every distinct place on the seeker's unlocated opportunities.

    Paced at the geocoder's one request per second and cached for a month, so
    a second run only pays for places it has not seen.  ``progress`` is called
    as ``progress(done, total)`` when given.
    """
    places = _pending_places(job_seeker_id, limit)
    report = {"places": len(places), "resolved": 0, "unplaceable": 0, "opportunities": 0}
    async with geo._geocoder_client() as client:
        for index, place in enumerate(places, start=1):
            location, country = place["location"], place["country"]
            codes = [country] if re.fullmatch(r"[A-Z]{2}", country or "") else None
            hit = await _resolve(location, codes, client)
            if hit is None:
                report["unplaceable"] += 1
            else:
                report["resolved"] += 1
                report["opportunities"] += _store(job_seeker_id, location, country, hit)
            if progress is not None:
                progress(index, len(places))
    log.info("Located opportunities for %s: %s", job_seeker_id, report)
    return report
