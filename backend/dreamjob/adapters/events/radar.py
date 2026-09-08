"""Event and community radar (FR-462, IR-101, IR-102, NFR-601).

FR-462 wants the conferences, meetups, webinars and industry events where
people from the target companies speak or attend.  Three kinds of page carry
that information in machine-readable form, and this adapter reads all three
through the same four-step contract:

* **schema.org ``Event`` JSON-LD**, which conference sites, Eventbrite and
  most ticketing platforms embed because search engines ask them to;
* **published iCalendar feeds** (``.ics``), which community and university
  calendars serve directly;
* the visible page text, as a last resort, for a start date and a title.

Terms of service are declared honestly rather than conveniently (IR-101).
Reading a conference's own programme page or a published ``.ics`` calendar is
ordinary web access, so :class:`EventRadarAdapter` is ``permitted``.  Meetup's
and Eventbrite's terms reserve automated collection to their APIs, so
:class:`SocialEventAdapter` is a separate adapter, marked ``restricted`` and
requiring an administrator acknowledgement before it will run.

Speakers are collected only where the page names them publicly, and only their
professional details - name, role, affiliation - which is what NFR-302 allows
and what FR-462 needs to say "somebody from your target company is there".
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote_plus, urlparse

from dreamjob.adapters.base import (
    AccessMethod,
    AdapterCapabilities,
    NormalisedRecord,
    PlanItem,
    RawRecord,
    SourceAdapter,
    SourceType,
    ToSStatus,
    register_adapter,
)
from dreamjob.egress.client import EgressClient, RobotsDisallowed

log = logging.getLogger(__name__)

EVENT_TYPES = frozenset(
    {
        "event", "businessevent", "educationevent", "socialevent", "festival",
        "exhibitionevent", "courseinstance", "publicationevent", "screeningevent",
    }
)

KIND_CONFERENCE = "conference"
KIND_MEETUP = "meetup"
KIND_WEBINAR = "webinar"
KIND_OTHER = "other"

FORMAT_IN_PERSON = "in_person"
FORMAT_ONLINE = "online"
FORMAT_HYBRID = "hybrid"

_CONFERENCE_WORDS = ("conference", "summit", "congress", "symposium", "convention", "expo",
                     "devcon", "forum", "days", "kongres")
_MEETUP_WORDS = ("meetup", "user group", "usergroup", "community night", "borrel")
_WEBINAR_WORDS = ("webinar", "online session", "livestream", "live stream", "virtual event")

_SCRIPT_LD_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_ICS_UNFOLD_RE = re.compile(r"\r?\n[ \t]")

MAX_EVENTS_PER_PAGE = 60
MAX_PAGE_BYTES = 3_000_000


# ---------------------------------------------------------------------------
# Parsed shape
# ---------------------------------------------------------------------------


@dataclass
class ParsedEvent:
    """One event as read from a page, before it becomes a knowledge-base row."""

    name: str
    starts_at: str | None = None
    ends_at: str | None = None
    location: str | None = None
    country: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    organiser: str | None = None
    url: str | None = None
    registration_url: str | None = None
    description: str = ""
    speakers: list[dict[str, Any]] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    kind: str = KIND_OTHER
    event_format: str = FORMAT_IN_PERSON
    language: str | None = None
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "location": self.location,
            "country": self.country,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "organiser": self.organiser,
            "url": self.url,
            "registration_url": self.registration_url,
            "description": self.description[:4000],
            "speakers": self.speakers,
            "topics": self.topics,
            "kind": self.kind,
            "format": self.event_format,
            "language": self.language,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html or "")).strip()


def normalise_datetime(value: Any) -> str | None:
    """Anything a page states as a date, as the canonical ISO string, or None."""
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    candidates = [text]
    if len(text) == 8 and text.isdigit():                       # ICS DATE
        candidates.append(f"{text[:4]}-{text[4:6]}-{text[6:8]}")
    if re.fullmatch(r"\d{8}T\d{6}Z?", text.rstrip("Z") + ("Z" if text.endswith("Z") else "")):
        base = text.rstrip("Z")
        candidates.append(
            f"{base[:4]}-{base[4:6]}-{base[6:8]}T{base[9:11]}:{base[11:13]}:{base[13:15]}"
            + ("+00:00" if text.endswith("Z") else "")
        )
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat(timespec="seconds")
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        return f"{match.group(0)}T00:00:00+00:00"
    return None


def classify_kind(name: str, url: str | None = None, description: str = "") -> str:
    haystack = f"{name} {url or ''} {description[:600]}".lower()
    if any(word in haystack for word in _WEBINAR_WORDS):
        return KIND_WEBINAR
    if any(word in haystack for word in _MEETUP_WORDS) or "meetup.com" in haystack:
        return KIND_MEETUP
    if any(word in haystack for word in _CONFERENCE_WORDS):
        return KIND_CONFERENCE
    return KIND_OTHER


def classify_format(attendance_mode: str, location: str | None, description: str = "") -> str:
    mode = (attendance_mode or "").lower()
    if "mixed" in mode:
        return FORMAT_HYBRID
    if "online" in mode:
        return FORMAT_ONLINE
    if "offline" in mode:
        return FORMAT_IN_PERSON
    haystack = f"{location or ''} {description[:400]}".lower()
    if any(word in haystack for word in ("online", "virtual", "webinar", "livestream", "zoom")):
        return FORMAT_ONLINE
    return FORMAT_IN_PERSON


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("@id") or "").strip()
    if isinstance(value, list):
        return ", ".join(t for t in (_text(v) for v in value) if t)
    return str(value or "").strip()


def _people(value: Any, kind: str) -> list[dict[str, Any]]:
    """Named speakers or performers, with the affiliation the page gives them."""
    items = value if isinstance(value, list) else [value]
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            affiliation = _text(item.get("affiliation") or item.get("worksFor"))
            role = str(item.get("jobTitle") or "").strip()
        else:
            name, affiliation, role = str(item or "").strip(), "", ""
        if not name or len(name) > 120:
            continue
        out.append(
            {"name": name, "role": role or None, "company_name": affiliation or None, "kind": kind}
        )
    return out[:60]


def _place(node: Any) -> tuple[str | None, str | None, float | None, float | None]:
    if not isinstance(node, dict):
        text = _text(node)
        return (text or None), None, None, None
    if str(node.get("@type", "")).lower() == "virtuallocation":
        return "online", None, None, None
    name = str(node.get("name") or "").strip()
    address = node.get("address")
    country = None
    parts = [name] if name else []
    if isinstance(address, dict):
        for key in ("streetAddress", "addressLocality", "addressRegion", "postalCode"):
            value = str(address.get(key) or "").strip()
            if value:
                parts.append(value)
        country = str(address.get("addressCountry") or "").strip() or None
        if isinstance(address.get("addressCountry"), dict):
            country = str(address["addressCountry"].get("name") or "").strip() or None
    elif address:
        parts.append(_text(address))
    geo = node.get("geo") if isinstance(node.get("geo"), dict) else {}
    latitude = _float(geo.get("latitude"))
    longitude = _float(geo.get("longitude"))
    location = ", ".join(dict.fromkeys(p for p in parts if p)) or None
    return location, _country_code(country), latitude, longitude


def _country_code(value: str | None) -> str | None:
    """ISO-2 where the page gives one, the country's name where it does not."""
    text = (value or "").strip()
    if not text:
        return None
    return text.upper() if len(text) == 2 else text[:64]


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# JSON-LD
# ---------------------------------------------------------------------------


def _iter_nodes(node: Any) -> Any:
    if isinstance(node, list):
        for item in node:
            yield from _iter_nodes(item)
        return
    if not isinstance(node, dict):
        return
    yield node
    for key in ("@graph", "subEvent", "subEvents", "itemListElement", "item", "events"):
        if key in node:
            yield from _iter_nodes(node[key])


def _is_event(node: dict) -> bool:
    types = node.get("@type") or node.get("type")
    values = types if isinstance(types, list) else [types]
    return any(str(v or "").lower() in EVENT_TYPES for v in values)


def parse_jsonld_events(html: str, source_url: str = "") -> list[ParsedEvent]:
    """Every schema.org ``Event`` embedded in a page (FR-462).

    The page is untrusted input, so a malformed block is skipped rather than
    raised (NFR-205, FR-183).
    """
    out: list[ParsedEvent] = []
    for block in _SCRIPT_LD_RE.findall(html or "")[:40]:
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for node in _iter_nodes(data):
            if not _is_event(node):
                continue
            name = str(node.get("name") or "").strip()
            if not name:
                continue
            location, country, latitude, longitude = _place(node.get("location"))
            description = strip_tags(str(node.get("description") or ""))
            url = str(node.get("url") or "").strip() or source_url or None
            speakers = _people(node.get("performer"), "speaker")
            speakers += _people(node.get("contributor"), "speaker")
            topics = [
                t
                for t in (
                    _text(node.get("about")),
                    _text(node.get("keywords")),
                )
                if t
            ]
            offers = node.get("offers")
            registration = ""
            if isinstance(offers, dict):
                registration = str(offers.get("url") or "").strip()
            elif isinstance(offers, list) and offers and isinstance(offers[0], dict):
                registration = str(offers[0].get("url") or "").strip()
            out.append(
                ParsedEvent(
                    name=name[:300],
                    starts_at=normalise_datetime(node.get("startDate")),
                    ends_at=normalise_datetime(node.get("endDate")),
                    location=location,
                    country=country,
                    latitude=latitude,
                    longitude=longitude,
                    organiser=_text(node.get("organizer")) or None,
                    url=url,
                    registration_url=registration or None,
                    description=description,
                    speakers=speakers,
                    topics=[t[:120] for t in topics],
                    kind=classify_kind(name, url, description),
                    event_format=classify_format(
                        _text(node.get("eventAttendanceMode")), location, description
                    ),
                    language=str(node.get("inLanguage") or "").strip()[:8] or None,
                    source=source_url,
                )
            )
            if len(out) >= MAX_EVENTS_PER_PAGE:
                return out
    return out


# ---------------------------------------------------------------------------
# iCalendar
# ---------------------------------------------------------------------------


def looks_like_ics(text: str) -> bool:
    return "BEGIN:VCALENDAR" in (text or "")[:2000].upper()


def parse_ics(text: str, source_url: str = "") -> list[ParsedEvent]:
    """Parse the VEVENTs of a published community calendar (FR-462)."""
    if not text:
        return []
    unfolded = _ICS_UNFOLD_RE.sub("", text)
    out: list[ParsedEvent] = []
    current: dict[str, str] | None = None
    for raw_line in unfolded.splitlines():
        line = raw_line.strip()
        if line.upper().startswith("BEGIN:VEVENT"):
            current = {}
            continue
        if line.upper().startswith("END:VEVENT"):
            if current and current.get("summary"):
                description = _unescape_ics(current.get("description", ""))
                name = _unescape_ics(current["summary"])[:300]
                url = current.get("url") or source_url or None
                location = _unescape_ics(current.get("location", "")) or None
                out.append(
                    ParsedEvent(
                        name=name,
                        starts_at=normalise_datetime(current.get("dtstart")),
                        ends_at=normalise_datetime(current.get("dtend")),
                        location=location,
                        organiser=_unescape_ics(current.get("organizer", "")).replace(
                            "mailto:", ""
                        )
                        or None,
                        url=url,
                        description=description,
                        kind=classify_kind(name, url, description),
                        event_format=classify_format("", location, description),
                        source=source_url,
                    )
                )
            current = None
            if len(out) >= MAX_EVENTS_PER_PAGE:
                break
            continue
        if current is None or ":" not in line:
            continue
        key, _, value = line.partition(":")
        name_part = key.split(";")[0].strip().lower()
        if name_part in ("summary", "dtstart", "dtend", "location", "description", "url",
                         "organizer"):
            current[name_part] = value.strip()
    return out


def _unescape_ics(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def parse_page(text: str, source_url: str = "") -> list[ParsedEvent]:
    """Whichever of the three readings the page supports."""
    if not text or len(text) > MAX_PAGE_BYTES:
        return []
    if looks_like_ics(text):
        return parse_ics(text, source_url)
    events = parse_jsonld_events(text, source_url)
    if events:
        return events
    # Last resort: a page that names one event and states one date.
    title_match = _TITLE_RE.search(text)
    if not title_match:
        return []
    name = strip_tags(title_match.group(1))[:300]
    date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    if not name or not date_match:
        return []
    return [
        ParsedEvent(
            name=name,
            starts_at=normalise_datetime(date_match.group(1)),
            url=source_url or None,
            kind=classify_kind(name, source_url),
            source=source_url,
        )
    ]


# ---------------------------------------------------------------------------
# Adapters (NFR-601)
# ---------------------------------------------------------------------------


@register_adapter
class EventRadarAdapter(SourceAdapter):
    """Conference programmes and published community calendars (FR-462)."""

    key = "events.radar"
    display_name = "Conference sites and community calendars"
    source_type = SourceType.EVENTS
    access_method = AccessMethod.HTTP
    capabilities = AdapterCapabilities(
        keyword_search=False,
        location_filter=False,
        company_lookup=True,
        pagination=False,
        max_results_per_query=MAX_EVENTS_PER_PAGE,
    )
    tos_status = ToSStatus.PERMITTED
    legal_notes = (
        "Public programme pages and published iCalendar feeds, fetched through the egress "
        "layer with robots.txt honoured and no authentication (FR-182, IR-102). Speaker "
        "names are collected only where the page publishes them, and only in their "
        "professional capacity (NFR-302)."
    )

    #: Sources supplied by the planner; there is no search endpoint to guess.
    plan_key = "event_sources"

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        """One plan item per calendar or programme page (FR-162)."""
        sources = caps.get(self.plan_key) or caps.get("events") or []
        items: list[PlanItem] = []
        for source in sources:
            url = source.get("url") if isinstance(source, dict) else str(source)
            if not url:
                continue
            items.append(
                PlanItem(
                    adapter_key=self.key,
                    native_query={
                        "url": url,
                        "company_id": source.get("company_id")
                        if isinstance(source, dict)
                        else None,
                        "label": source.get("label") if isinstance(source, dict) else None,
                    },
                    rationale=f"Read {urlparse(url).netloc or url} for upcoming events (FR-462)",
                    estimated_pages=1,
                    estimated_seconds=8,
                )
            )
        return items

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        query = item.native_query or {}
        url = str(query.get("url") or "")
        if not url:
            return []
        own_client = self.egress is None
        client = self.egress or EgressClient()
        if own_client:
            await client.__aenter__()
        try:
            result = await client.fetch(url)
        except RobotsDisallowed:
            log.info("[%s] robots.txt disallows %s", self.key, url)
            return []
        except Exception as exc:  # noqa: BLE001 - an unreachable calendar is normal
            log.info("[%s] %s unavailable: %s", self.key, url, exc)
            return []
        finally:
            if own_client:
                await client.__aexit__(None, None, None)
        if not result.ok:
            return []
        return [
            RawRecord(
                url=url,
                content=result.text,
                content_type="text/calendar" if looks_like_ics(result.text) else "text/html",
                raw_document_id=result.raw_document_id,
                meta={"company_id": query.get("company_id"), "label": query.get("label")},
            )
        ]

    def parse(self, raw: RawRecord) -> list[dict]:
        return [event.as_dict() for event in parse_page(raw.content, raw.url)]

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        """An event is worth storing when it has a name and a future start."""
        name = str(parsed.get("name") or "").strip()
        starts_at = parsed.get("starts_at")
        if not name or not starts_at:
            return None
        if starts_at < (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds"):
            return None

        company_id = (raw.meta or {}).get("company_id")
        data = {
            "name": name[:300],
            "starts_at": starts_at,
            "ends_at": parsed.get("ends_at"),
            "location": parsed.get("location"),
            "country": parsed.get("country"),
            "latitude": parsed.get("latitude"),
            "longitude": parsed.get("longitude"),
            "organiser": parsed.get("organiser"),
            "url": parsed.get("url") or raw.url,
            "registration_url": parsed.get("registration_url"),
            "description": (parsed.get("description") or "")[:4000],
            "speakers": parsed.get("speakers") or [],
            # FR-462's "attendees identified where public": a programme page
            # publishes speakers, not its audience.  The column stays empty
            # until a source names attendees, and the radar reads both.
            "attendees": [],
            "topics": parsed.get("topics") or [],
            "kind": parsed.get("kind") or KIND_OTHER,
            "format": parsed.get("format") or FORMAT_IN_PERSON,
            "language": parsed.get("language"),
            "linked_company_ids": [company_id] if company_id else [],
            "source": self.key,
            "access_method": self.access_method.value,
        }
        return NormalisedRecord(
            entity_type="event",
            data=data,
            confidence=0.75 if parsed.get("speakers") else 0.6,
            provenance={"adapter": self.key, "url": data["url"]},
        )


@register_adapter
class SocialEventAdapter(EventRadarAdapter):
    """Meetup and Eventbrite public listings (FR-462, IR-101).

    Kept apart from :class:`EventRadarAdapter` for one reason: both platforms'
    terms reserve automated collection to their APIs.  The adapter is therefore
    ``restricted`` and disabled until an administrator acknowledges the terms,
    which is exactly the mechanism IR-101 asks for.  The parsing is the same -
    both platforms publish schema.org ``Event`` JSON-LD on their public pages.
    """

    key = "events.social"
    display_name = "Meetup and Eventbrite (public listings)"
    tos_status = ToSStatus.RESTRICTED
    requires_ack = True
    legal_notes = (
        "Meetup's and Eventbrite's terms of use reserve automated access to their APIs. "
        "This adapter reads only public listing pages and only after an administrator has "
        "acknowledged those terms (IR-101); prefer an API key where one is available."
    )
    plan_key = "social_event_queries"

    SEARCH_TEMPLATES = {
        "meetup": "https://www.meetup.com/find/?keywords={q}&location={loc}",
        "eventbrite": "https://www.eventbrite.com/d/{loc}/{q}/",
    }

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        """Turn role families and the seeker's location into listing queries (FR-162)."""
        explicit = super().plan(directives, composite_profile, caps)
        if explicit:
            return explicit

        keywords = [k for k in (caps.get("keywords") or []) if str(k).strip()][:4]
        location = str(caps.get("location") or "").strip()
        if not keywords or not location:
            return []
        items: list[PlanItem] = []
        for platform, template in self.SEARCH_TEMPLATES.items():
            for keyword in keywords:
                items.append(
                    PlanItem(
                        adapter_key=self.key,
                        native_query={
                            "url": template.format(
                                q=quote_plus(str(keyword)), loc=quote_plus(location)
                            ),
                            "platform": platform,
                            "keyword": keyword,
                        },
                        rationale=(
                            f"Look for {keyword} events near {location} on {platform} (FR-462)"
                        ),
                        estimated_pages=1,
                        estimated_seconds=12,
                    )
                )
        return items
