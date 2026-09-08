"""Geocoding and travel-time estimation for location directives (FR-144, IR-102).

Location directives are entered by auto-complete backed by a geocoder and are
stored as coordinates, so that a radius or a maximum commute time can be turned
into the radius/latitude/longitude filters that job boards actually accept
(FR-162) and into a post-filter for records whose source had no radius search.

The geocoder is Nominatim (OpenStreetMap).  Its usage policy is the binding
constraint here and this module implements all of it:

* at most one request per second, enforced process-wide by ``_paced_fetch``
  and not by the per-domain limiter alone, so a configuration change to
  ``DREAMJOB_PER_DOMAIN_RPS`` cannot make us impolite;
* an identifying User-Agent with a contact address - set once by the egress
  layer (``settings.user_agent``), never overridden here;
* results are cached: in process for the life of the request, and on disk for
  thirty days through the egress HTTP cache, because place coordinates do not
  move.  A geocoded ``LocationArea`` also keeps its coordinates in the directive
  set, so a saved directive never re-geocodes at all.

``robots.txt`` on nominatim.openstreetmap.org disallows ``/search`` and
``/reverse``.  Those rules exist to keep search-engine crawlers out of the
query interface; the same operator's usage policy explicitly permits
programmatic access under the conditions above, which is the mode we use.  The
dedicated client below is therefore constructed with ``respect_robots=False``
- scoped to this one API, while every crawling path in Dream Job keeps
FR-182's robots compliance.

Every entry point degrades gracefully: with no network, a blocked request or a
rate-limited geocoder, ``geocode`` returns ``None`` and the caller keeps the
free-text place label the job seeker typed.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel, Field

from dreamjob.config import get_settings
from dreamjob.db.connection import utcnow
from dreamjob.egress.client import EgressClient

log = logging.getLogger(__name__)

# Nominatim usage policy: absolute maximum of one request per second.
MIN_REQUEST_INTERVAL_SECONDS = 1.0
# Place coordinates are effectively static; cache them for a month (FR-182).
GEOCODE_CACHE_TTL_SECONDS = 30 * 24 * 3600

_pace_lock = asyncio.Lock()
_last_request_at = 0.0
_memo: dict[str, list[dict[str, Any]]] = {}
_MEMO_MAX = 512

EARTH_RADIUS_KM = 6371.0088


class GeocodeResult(BaseModel):
    """One resolved place.  Persisted inside the location directives (FR-144)."""

    query: str
    display_name: str
    latitude: float
    longitude: float
    country_code: str | None = None
    place_type: str | None = None
    osm_id: str | None = None
    importance: float | None = None
    bounding_box: list[float] | None = None
    geocoded_at: str = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Commute model (FR-144)
# ---------------------------------------------------------------------------

# A defensible, documented speed model - not a routing engine.
#
# Straight-line (haversine) distance always understates travel distance, so it
# is multiplied by a circuity factor before dividing by a speed.  Published
# circuity factors for European road networks sit around 1.2-1.4; walking and
# cycling networks are denser and score lower.
#
# Speed itself depends on distance: a 3 km car trip is city driving, a 40 km
# trip is mostly motorway.  Each mode therefore has an urban and an open-road
# speed, blended linearly over the first 25 road-kilometres.
#
# The fixed overhead covers what happens before and after moving: parking and
# the walk from it, locking a bike, or the walk-wait-transfer time that makes
# public transport slower than its vehicle speed suggests.
#
#   mode              urban   open   circuity   overhead (min)
#   walk               4.8     4.8     1.15         0
#   bike              14.0    17.0     1.20         3
#   ebike             19.0    23.0     1.20         3
#   public_transport  18.0    45.0     1.25        12
#   car               25.0    70.0     1.30         8
#   motorcycle        30.0    75.0     1.25         5
COMMUTE_MODES: dict[str, tuple[float, float, float, float]] = {
    "walk": (4.8, 4.8, 1.15, 0.0),
    "bike": (14.0, 17.0, 1.20, 3.0),
    "ebike": (19.0, 23.0, 1.20, 3.0),
    "public_transport": (18.0, 45.0, 1.25, 12.0),
    "car": (25.0, 70.0, 1.30, 8.0),
    "motorcycle": (30.0, 75.0, 1.25, 5.0),
}
DEFAULT_COMMUTE_MODE = "car"
_BLEND_DISTANCE_KM = 25.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres between two WGS-84 points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def commute_minutes(distance_km: float, mode: str = DEFAULT_COMMUTE_MODE) -> float:
    """Estimated door-to-door travel time for a straight-line distance.

    See the model documented above: circuity correction, distance-blended
    speed, fixed per-mode overhead.  Unknown modes fall back to the car model.
    """
    urban, open_road, circuity, overhead = COMMUTE_MODES.get(
        mode, COMMUTE_MODES[DEFAULT_COMMUTE_MODE]
    )
    if distance_km <= 0:
        return overhead
    road_km = distance_km * circuity
    blend = min(1.0, road_km / _BLEND_DISTANCE_KM)
    speed = urban + (open_road - urban) * blend
    return overhead + road_km / speed * 60.0


def max_radius_km(minutes: float, mode: str = DEFAULT_COMMUTE_MODE) -> float:
    """Invert the commute model: the radius reachable within ``minutes``.

    Used to translate "at most 45 minutes by car" into the radius parameter a
    job board expects (FR-162).  ``commute_minutes`` is strictly increasing in
    distance, so a bisection converges quickly and needs no closed form.
    """
    _, _, _, overhead = COMMUTE_MODES.get(mode, COMMUTE_MODES[DEFAULT_COMMUTE_MODE])
    if minutes <= overhead:
        return 0.0
    low, high = 0.0, 1.0
    while commute_minutes(high, mode) < minutes and high < 2000.0:
        high *= 2
    for _ in range(40):
        mid = (low + high) / 2
        if commute_minutes(mid, mode) < minutes:
            low = mid
        else:
            high = mid
    return round((low + high) / 2, 2)


def within_commute(
    origin: tuple[float, float],
    destination: tuple[float, float],
    max_minutes: float,
    mode: str = DEFAULT_COMMUTE_MODE,
) -> bool:
    """True when the destination is reachable within the tolerated time."""
    distance = haversine_km(origin[0], origin[1], destination[0], destination[1])
    return commute_minutes(distance, mode) <= max_minutes


# ---------------------------------------------------------------------------
# Nominatim client
# ---------------------------------------------------------------------------


def _geocoder_client() -> EgressClient:
    """Egress client scoped to the geocoder API (see the module docstring)."""
    return EgressClient(
        max_concurrency=1,
        respect_robots=False,
        cache_ttl=GEOCODE_CACHE_TTL_SECONDS,
    )


async def _paced_fetch(client: EgressClient, url: str) -> Any:
    """Fetch under the one-request-per-second rule.

    The lock is held across the request rather than only before it, so two
    concurrent callers cannot both decide the second is free.  A response
    served from the HTTP cache costs the operator nothing, so it does not
    consume the rate budget and does not delay the next real request.
    """
    global _last_request_at
    async with _pace_lock:
        elapsed = time.monotonic() - _last_request_at
        if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
            await asyncio.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)
        result = await client.fetch(url, access_method="api")
        if not result.from_cache:
            _last_request_at = time.monotonic()
        return result


def _memoise(key: str, payload: list[dict[str, Any]]) -> None:
    if len(_memo) >= _MEMO_MAX:
        _memo.clear()
    _memo[key] = payload


async def _call(path: str, params: dict[str, Any], egress: EgressClient | None) -> list[dict]:
    """One Nominatim call.  Returns ``[]` rather than raising (NFR-104)."""
    settings = get_settings()
    query = {k: v for k, v in params.items() if v not in (None, "", [])}
    query.setdefault("format", "jsonv2")
    url = f"{settings.geocoder_url.rstrip('/')}/{path}?{urlencode(query)}"

    cached = _memo.get(url)
    if cached is not None:
        return cached

    async def _fetch(client: EgressClient) -> list[dict]:
        result = await _paced_fetch(client, url)
        if not result.ok:
            log.info("Geocoder returned HTTP %s for %s", result.status_code, params.get("q"))
            return []
        import json  # noqa: PLC0415 - local, the response is small

        data = json.loads(result.text)
        if isinstance(data, dict):
            data = [data]
        return [d for d in data if isinstance(d, dict)]

    try:
        if egress is not None:
            payload = await _fetch(egress)
        else:
            async with _geocoder_client() as client:
                payload = await _fetch(client)
    except Exception as exc:  # noqa: BLE001 - offline or blocked: degrade, never fail
        log.info("Geocoding unavailable (%s): %s", type(exc).__name__, exc)
        return []

    _memoise(url, payload)
    return payload


def _to_result(query: str, row: dict[str, Any]) -> GeocodeResult | None:
    try:
        latitude, longitude = float(row["lat"]), float(row["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    address = row.get("address") or {}
    box = row.get("boundingbox")
    return GeocodeResult(
        query=query,
        display_name=str(row.get("display_name") or row.get("name") or query),
        latitude=latitude,
        longitude=longitude,
        country_code=(address.get("country_code") or "").upper() or None,
        place_type=row.get("addresstype") or row.get("type"),
        osm_id=f"{row.get('osm_type', '')}/{row.get('osm_id', '')}".strip("/") or None,
        importance=float(row["importance"]) if row.get("importance") is not None else None,
        bounding_box=[float(v) for v in box] if isinstance(box, list) and len(box) == 4 else None,
    )


async def search(
    query: str,
    *,
    limit: int = 5,
    country_codes: list[str] | None = None,
    language: str = "en",
    egress: EgressClient | None = None,
) -> list[GeocodeResult]:
    """Auto-complete candidates for a place (FR-144).  Empty when unavailable."""
    text = (query or "").strip()
    if len(text) < 2:
        return []
    rows = await _call(
        "search",
        {
            "q": text,
            "limit": max(1, min(limit, 10)),
            "addressdetails": 1,
            "accept-language": language,
            "countrycodes": ",".join(c.lower() for c in country_codes) if country_codes else None,
        },
        egress,
    )
    out = [_to_result(text, row) for row in rows]
    return [r for r in out if r is not None]


async def geocode(
    query: str,
    *,
    country_codes: list[str] | None = None,
    language: str = "en",
    egress: EgressClient | None = None,
) -> GeocodeResult | None:
    """Resolve one place name to coordinates, or ``None`` if it cannot be resolved."""
    results = await search(
        query, limit=1, country_codes=country_codes, language=language, egress=egress
    )
    return results[0] if results else None


async def geocode_many(
    queries: list[str], *, country_codes: list[str] | None = None, language: str = "en"
) -> dict[str, GeocodeResult | None]:
    """Resolve several places over one client, still paced at 1 req/s."""
    out: dict[str, GeocodeResult | None] = {}
    unique = [q for q in dict.fromkeys(q.strip() for q in queries if q and q.strip())]
    if not unique:
        return out
    async with _geocoder_client() as client:
        for item in unique:
            out[item] = await geocode(
                item, country_codes=country_codes, language=language, egress=client
            )
    return out


async def reverse(
    latitude: float, longitude: float, *, language: str = "en", egress: EgressClient | None = None
) -> GeocodeResult | None:
    """Name the place at a coordinate - used to label a map-picked point."""
    rows = await _call(
        "reverse",
        {
            "lat": latitude,
            "lon": longitude,
            "zoom": 12,
            "addressdetails": 1,
            "accept-language": language,
        },
        egress,
    )
    for row in rows:
        result = _to_result(f"{latitude},{longitude}", row)
        if result is not None:
            return result
    return None
