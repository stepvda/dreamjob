"""Event and community radar: matching, linking and the calendar (FR-462, FR-385).

:mod:`dreamjob.adapters.events.radar` collects the events; this module is what
makes them a radar rather than a list.  It answers the three questions FR-462
asks of every event:

* **Is it reachable?**  Distance from the seeker's home location against their
  travel tolerance (FR-144, FR-145); online events are always reachable.
* **Is anybody from a target company there?**  Speaker and organiser
  affiliations are resolved against the campaign's own companies, and the
  match is what links the event to those company profiles.
* **Is it about the right thing?**  Event title, topics and description against
  the dream job model's role families and the seeker's skills.

Adding an event to the calendar writes an iCalendar file that any calendar
application accepts, and hands off to the post-application calendar
integration when that slice is present - so the feature works with no OAuth
grant and improves when there is one (NFR-104).

Discretion mode is honoured here as everywhere else (FR-385): an event hosted
by an excluded company is dropped, and an event where an excluded contact
speaks is flagged rather than silently recommended.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dreamjob.adapters.events import radar as radar_adapter
from dreamjob.config import get_settings
from dreamjob.db.connection import from_json, utcnow
from dreamjob.db.repositories import campaigns as catalogue_repo
from dreamjob.db.repositories import intelligence as repo
from dreamjob.egress.client import EgressClient
from dreamjob.intelligence.text import fold, tokens
from dreamjob.pipeline import directives as dir_mod
from dreamjob.pipeline.geocode import haversine_km, max_radius_km
from dreamjob.pipeline.knowledge_base import KnowledgeBaseWriter

log = logging.getLogger(__name__)

#: How far the seeker will travel for an event, by FR-145 travel tolerance.
#: An event is a day or two, not a commute, so the ceiling is wider than the
#: commute radius - but "no travel" still means "no travel".
TRAVEL_CEILING_KM: dict[str, float] = {
    "none": 0.0,            # commute radius only, computed separately
    "occasional": 350.0,
    "regular": 900.0,
    "frequent": 2500.0,
    "extensive": 20000.0,
}

DEFAULT_HORIZON_DAYS = 120
DEFAULT_LIMIT = 50

STATUS_INTERESTED = "interested"
STATUS_GOING = "going"
STATUS_DISMISSED = "dismissed"


@dataclass
class EventMatch:
    """One event, scored against this job seeker's campaign (FR-462)."""

    event: dict[str, Any]
    distance_km: float | None
    reachable: bool
    travel_note: str
    target_companies: list[dict[str, Any]] = field(default_factory=list)
    target_attendees: list[dict[str, Any]] = field(default_factory=list)
    topic_score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    discretion_flags: list[str] = field(default_factory=list)
    interest: dict[str, Any] | None = None

    @property
    def score(self) -> float:
        """Who is there matters more than what it is about, and both matter
        more than how far away it is - a webinar with the hiring manager on it
        beats a perfect-topic conference nobody relevant attends."""
        people = min(len(self.target_attendees), 3) / 3
        companies = min(len(self.target_companies), 3) / 3
        proximity = 1.0 if self.distance_km is None else max(0.0, 1.0 - self.distance_km / 1000)
        return round(
            0.4 * people + 0.25 * companies + 0.25 * self.topic_score + 0.1 * proximity, 4
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.event,
            "match": {
                "score": self.score,
                "distance_km": self.distance_km,
                "reachable": self.reachable,
                "travel_note": self.travel_note,
                "topic_score": round(self.topic_score, 3),
                "target_companies": self.target_companies,
                "target_attendees": self.target_attendees,
                "reasons": self.reasons,
                "discretion_flags": self.discretion_flags,
            },
            "interest": self.interest,
        }


# ---------------------------------------------------------------------------
# Reachability (FR-144, FR-145)
# ---------------------------------------------------------------------------


def travel_ceiling_km(directives: Any | None) -> float:
    """The distance this job seeker will travel for an event."""
    if directives is None:
        return TRAVEL_CEILING_KM["occasional"]
    tolerance = str(
        getattr(getattr(directives, "work_arrangement", None), "travel_tolerance", "occasional")
    )
    tolerance = tolerance.split(".")[-1].lower()
    ceiling = TRAVEL_CEILING_KM.get(tolerance)
    if ceiling is None:
        return TRAVEL_CEILING_KM["occasional"]
    if ceiling == 0.0:
        location = getattr(directives, "location", None)
        minutes = getattr(location, "max_commute_minutes", None) or 60
        mode = str(getattr(getattr(location, "commute_mode", None), "value", "car"))
        return max_radius_km(minutes, mode)
    return ceiling


def _home(directives: Any | None) -> tuple[float, float] | None:
    location = getattr(directives, "location", None)
    home = getattr(location, "home_location", None) if location else None
    if home is not None and getattr(home, "is_geocoded", False):
        return float(home.latitude), float(home.longitude)
    for area in getattr(location, "areas", []) or []:
        if getattr(area, "is_geocoded", False):
            return float(area.latitude), float(area.longitude)
    return None


def reachability(
    event: dict[str, Any], directives: Any | None
) -> tuple[float | None, bool, str]:
    """Distance, verdict and the sentence explaining the verdict."""
    if str(event.get("format") or "") == "online":
        return None, True, "online - no travel"
    home = _home(directives)
    ceiling = travel_ceiling_km(directives)
    latitude, longitude = event.get("latitude"), event.get("longitude")
    if home is None or latitude is None or longitude is None:
        location = getattr(directives, "location", None)
        countries = {c.upper() for c in getattr(location, "countries", [])}
        country = str(event.get("country") or "").upper()
        if countries and country and country not in countries:
            return None, False, f"{country} is outside the countries in your directives"
        return None, True, "distance unknown - no home location or no coordinates on the event"
    distance = round(haversine_km(home[0], home[1], float(latitude), float(longitude)), 1)
    if distance <= ceiling:
        return distance, True, f"{distance:.0f} km, within your {ceiling:.0f} km travel tolerance"
    return distance, False, f"{distance:.0f} km, beyond your {ceiling:.0f} km travel tolerance"


# ---------------------------------------------------------------------------
# Target companies and attendees (FR-462)
# ---------------------------------------------------------------------------


def _company_index(companies: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for company in companies:
        for name in (company.get("name"), company.get("normalised_name")):
            key = dir_mod.normalise_company_name(name)
            if key:
                index[key] = company
    return index


def _people_of(event: dict[str, Any]) -> list[dict[str, Any]]:
    people: list[dict[str, Any]] = []
    for key in ("speakers", "attendees"):
        block = event.get(key)
        block = from_json(block, []) if not isinstance(block, list) else block
        for person in block or []:
            if isinstance(person, dict) and person.get("name"):
                people.append({**person, "kind": person.get("kind") or key.rstrip("s")})
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for person in people:
        key = fold(person["name"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(person)
    return unique


def match_people(
    event: dict[str, Any], index: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """People at this event who work for a target company, and those companies."""
    attendees: list[dict[str, Any]] = []
    companies: dict[str, dict[str, Any]] = {}
    for person in _people_of(event):
        key = dir_mod.normalise_company_name(person.get("company_name"))
        company = index.get(key) if key else None
        if company is None:
            continue
        attendees.append(
            {
                "name": person.get("name"),
                "role": person.get("role"),
                "kind": person.get("kind"),
                "company_id": company["id"],
                "company_name": company.get("name"),
            }
        )
        companies[company["id"]] = {"company_id": company["id"], "name": company.get("name")}

    organiser_key = dir_mod.normalise_company_name(event.get("organiser"))
    organiser = index.get(organiser_key) if organiser_key else None
    if organiser is not None:
        companies.setdefault(
            organiser["id"], {"company_id": organiser["id"], "name": organiser.get("name")}
        )
    for company_id in from_json(event.get("linked_company_ids"), []) or []:
        company = next((c for c in index.values() if c["id"] == company_id), None)
        if company is not None:
            companies.setdefault(
                company_id, {"company_id": company_id, "name": company.get("name")}
            )
    return attendees, list(companies.values())


def topic_relevance(event: dict[str, Any], wanted: set[str]) -> float:
    if not wanted:
        return 0.0
    topics = from_json(event.get("topics"), []) or []
    haystack = tokens(
        event.get("name"),
        " ".join(str(t) for t in topics),
        str(event.get("description") or "")[:2000],
    )
    if not haystack:
        return 0.0
    return round(len(wanted & haystack) / min(len(wanted), 12), 3)


def _wanted_terms(job_seeker_id: str, campaign: dict[str, Any] | None) -> set[str]:
    dream = repo.latest_dream_model(job_seeker_id, (campaign or {}).get("persona_id")) or (
        repo.latest_dream_model(job_seeker_id)
    )
    terms: set[str] = set()
    for key in ("role_families", "target_roles"):
        for item in from_json((dream or {}).get(key), []) or []:
            text = item.get("family") or item.get("title") if isinstance(item, dict) else item
            terms |= tokens(text)
    for row in repo.profile_skills(job_seeker_id):
        terms |= tokens(row.get("normalised_label"))
    return terms


# ---------------------------------------------------------------------------
# The radar
# ---------------------------------------------------------------------------


def radar(
    job_seeker_id: str,
    campaign_id: str | None = None,
    *,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    limit: int = DEFAULT_LIMIT,
    include_unreachable: bool = False,
) -> dict[str, Any]:
    """The FR-462 radar: upcoming events, matched to this job seeker."""
    campaign = (
        repo.get_campaign(campaign_id, job_seeker_id)
        if campaign_id
        else repo.latest_campaign(job_seeker_id)
    )
    directive_row = repo.directive_set(job_seeker_id, (campaign or {}).get("directive_set_id"))
    directives = dir_mod.coerce_directive_set(directive_row) if directive_row else None
    discretion = bool((directive_row or {}).get("discretion_mode"))

    company_ids = repo.campaign_company_ids(job_seeker_id, (campaign or {}).get("id"))
    companies = repo.companies_by_ids(company_ids)
    index = _company_index(companies)
    wanted = _wanted_terms(job_seeker_id, campaign)

    now = datetime.now(UTC)
    events = repo.upcoming_events(
        since=now.isoformat(timespec="seconds"),
        until=(now + timedelta(days=horizon_days)).isoformat(timespec="seconds"),
        limit=1000,
    )

    matches: list[EventMatch] = []
    excluded = 0
    for event in events:
        attendees, target_companies = match_people(event, index)
        if directives is not None and dir_mod.is_excluded(directives, event.get("organiser")):
            excluded += 1
            continue

        flags: list[str] = []
        if discretion and directives is not None:
            visible = [
                person
                for person in attendees
                if dir_mod.is_excluded_contact(
                    directives,
                    {"full_name": person.get("name"), "company_name": person.get("company_name")},
                )
            ]
            if visible:
                flags.append(
                    "discretion mode: "
                    + ", ".join(str(p.get("name")) for p in visible[:3])
                    + " would see you there"
                )

        distance, reachable, note = reachability(event, directives)
        if not reachable and not include_unreachable:
            continue

        reasons: list[str] = []
        if attendees:
            reasons.append(
                f"{len(attendees)} person/people from your target companies are named on the "
                "public page"
            )
        if target_companies:
            reasons.append(
                "linked to " + ", ".join(str(c["name"]) for c in target_companies[:3])
            )
        score = topic_relevance(event, wanted)
        if score:
            reasons.append(f"topic overlap with your dream job ({score:.0%})")
        reasons.append(note)

        matches.append(
            EventMatch(
                event=event,
                distance_km=distance,
                reachable=reachable,
                travel_note=note,
                target_companies=target_companies,
                target_attendees=attendees,
                topic_score=score,
                reasons=reasons,
                discretion_flags=flags,
                interest=repo.get_interest(job_seeker_id, event["id"]),
            )
        )

    matches.sort(key=lambda m: (-m.score, m.event.get("starts_at") or ""))
    top = matches[:limit]
    return {
        "campaign_id": (campaign or {}).get("id"),
        "horizon_days": horizon_days,
        "discretion_mode": discretion,
        "travel_ceiling_km": travel_ceiling_km(directives),
        "counts": {
            "in_window": len(events),
            "matched": len(matches),
            "returned": len(top),
            "excluded_by_discretion": excluded,
            "target_companies": len(company_ids),
        },
        "events": [m.as_dict() for m in top],
        "note": (
            "Speakers and organisers are read from public event pages only, in their "
            "professional capacity (NFR-302). Attendance is the job seeker's decision; "
            "nothing is registered or booked by the system."
        ),
    }


def _adapter_permitted(adapter: Any) -> bool:
    """IR-101, applied before the first request rather than after it.

    ``SourceAdapter.is_enabled`` treats an adapter with no catalogue row as
    enabled, which is right for a permitted source and wrong for one whose
    terms require an acknowledgement: absence of a row is absence of consent.
    """
    if not adapter.is_enabled():
        return False
    if not adapter.requires_ack:
        return True
    entry = catalogue_repo.get_catalogue_entry(adapter.key)
    return bool(entry and entry.get("acknowledged_at"))


async def collect(
    job_seeker_id: str,
    sources: list[Any],
    *,
    campaign_id: str | None = None,
    keywords: list[str] | None = None,
    location: str | None = None,
    use_social: bool = False,
) -> dict[str, Any]:
    """Run the event adapters over the given calendars and listing pages (FR-462).

    ``use_social`` selects the restricted Meetup/Eventbrite adapter, which stays
    disabled until an administrator acknowledges those platforms' terms
    (IR-101); the call reports the refusal rather than quietly collecting
    nothing.
    """
    adapter_cls = (
        radar_adapter.SocialEventAdapter if use_social else radar_adapter.EventRadarAdapter
    )
    caps: dict[str, Any] = {
        adapter_cls.plan_key: sources or [],
        "keywords": keywords or [],
        "location": location or "",
    }
    written = 0
    errors: list[str] = []
    async with EgressClient() as egress:
        adapter = adapter_cls(egress=egress)
        if not _adapter_permitted(adapter):
            return {
                "adapter": adapter.key,
                "collected": 0,
                "skipped": True,
                "reason": (
                    f"{adapter.display_name} is disabled: its terms of service require an "
                    "administrator acknowledgement first (IR-101)."
                ),
            }
        items = adapter.plan({}, {}, caps)
        writer = KnowledgeBaseWriter(adapter_key=adapter.key, campaign_id=campaign_id)
        for item in items:
            try:
                records = await adapter.run(item)
            except Exception as exc:  # noqa: BLE001 - one bad source, not the whole run
                log.info("[%s] %s failed: %s", adapter.key, item.native_query, exc)
                errors.append(str(exc)[:200])
                continue
            written += len(writer.write_many(records))
    return {
        "adapter": adapter_cls.key,
        "planned": len(caps[adapter_cls.plan_key]) or len(keywords or []),
        "collected": written,
        "errors": errors,
        "extraction_rate": None,
    }


def link_to_companies(job_seeker_id: str, event_id: str, campaign_id: str | None = None) -> dict:
    """Resolve the event's speakers and organiser onto company profiles (FR-462)."""
    event = repo.get_event(event_id)
    if event is None:
        raise LookupError(f"No event {event_id}")
    company_ids = repo.campaign_company_ids(job_seeker_id, campaign_id)
    index = _company_index(repo.companies_by_ids(company_ids))
    names = [
        p.get("company_name") for p in _people_of(event) if p.get("company_name")
    ] + [event.get("organiser")]
    index.update(_company_index(repo.companies_by_names([str(n) for n in names if n])))
    _attendees, companies = match_people(event, index)
    resolved = sorted({c["company_id"] for c in companies})
    if resolved:
        repo.link_event_companies(event_id, resolved)
    return {"event_id": event_id, "linked_company_ids": resolved, "companies": companies}


# ---------------------------------------------------------------------------
# The calendar (FR-462: "events shall be addable to the calendar")
# ---------------------------------------------------------------------------


def _ics_escape(value: str) -> str:
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def _ics_stamp(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def ics_document(event: dict[str, Any], *, organiser: str = "Dream Job") -> str:
    """One VEVENT, as any calendar application will import it."""
    start = _ics_stamp(event.get("starts_at"))
    end = _ics_stamp(event.get("ends_at")) or _ics_stamp(
        (
            datetime.fromisoformat(str(event["starts_at"])) + timedelta(hours=2)
        ).isoformat(timespec="seconds")
        if event.get("starts_at")
        else None
    )
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Dream Job//Event radar (FR-462)//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:dreamjob-event-{event['id']}@dreamjob",
        f"DTSTAMP:{_ics_stamp(utcnow())}",
        f"SUMMARY:{_ics_escape(event.get('name'))}",
    ]
    if start:
        lines.append(f"DTSTART:{start}")
    if end:
        lines.append(f"DTEND:{end}")
    if event.get("location"):
        lines.append(f"LOCATION:{_ics_escape(event['location'])}")
    if event.get("url"):
        lines.append(f"URL:{event['url']}")
    description = str(event.get("description") or "")[:900]
    if event.get("organiser"):
        description = f"Organiser: {event['organiser']}\n{description}".strip()
    if description:
        lines.append(f"DESCRIPTION:{_ics_escape(description)}")
    lines += [f"ORGANIZER;CN={_ics_escape(organiser)}:MAILTO:noreply@dreamjob.invalid",
              "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def _calendar_module() -> Any | None:
    """The post-application calendar integration, when that slice is present.

    FR-462 asks for events to be addable to the calendar; FR-423 owns the OAuth
    grant that can write to it.  Importing it lazily keeps the radar working -
    as a downloadable ``.ics`` - whether or not that integration exists yet.
    """
    try:
        from dreamjob.postapp import calendar_sync  # noqa: PLC0415
    except ImportError:
        return None
    return calendar_sync


def add_to_calendar(
    job_seeker_id: str,
    event_id: str,
    *,
    campaign_id: str | None = None,
    note: str | None = None,
    status: str = STATUS_GOING,
) -> dict[str, Any]:
    """Write the calendar entry for one event and record the seeker's interest."""
    event = repo.get_event(event_id)
    if event is None:
        raise LookupError(f"No event {event_id}")

    settings = get_settings()
    directory = Path(settings.generated_dir) / "events" / job_seeker_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{event_id}.ics"
    path.write_text(ics_document(event), encoding="utf-8")

    values: dict[str, Any] = {
        "campaign_id": campaign_id,
        "status": status,
        "note": note,
        "ics_path": str(path),
    }

    module = _calendar_module()
    if module is not None:
        creator = getattr(module, "create_event", None) or getattr(module, "add_event", None)
        if callable(creator):
            try:
                created = creator(
                    job_seeker_id,
                    title=event.get("name"),
                    starts_at=event.get("starts_at"),
                    ends_at=event.get("ends_at"),
                    location=event.get("location"),
                    description=event.get("url"),
                ) or {}
                values.update(
                    {
                        "calendar_account_id": created.get("calendar_account_id"),
                        "calendar_event_id": created.get("calendar_event_id"),
                        "calendar_event_url": created.get("calendar_event_url"),
                        "last_error": None,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - the .ics is the working fallback
                log.info("Calendar hand-off failed for event %s: %s", event_id, exc)
                values["last_error"] = str(exc)[:500]

    interest_id = repo.upsert_interest(job_seeker_id, event_id, values)
    return {
        "id": interest_id,
        "event_id": event_id,
        "status": status,
        "ics_path": str(path),
        "calendar_event_url": values.get("calendar_event_url"),
        "note": (
            "The .ics file imports into any calendar. A connected calendar (FR-423) is used "
            "automatically when one is available."
        ),
    }


def set_interest(
    job_seeker_id: str,
    event_id: str,
    *,
    status: str = STATUS_INTERESTED,
    note: str | None = None,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    if repo.get_event(event_id) is None:
        raise LookupError(f"No event {event_id}")
    if status == STATUS_DISMISSED:
        repo.delete_interest(job_seeker_id, event_id)
        return {"event_id": event_id, "status": status}
    interest_id = repo.upsert_interest(
        job_seeker_id, event_id, {"status": status, "note": note, "campaign_id": campaign_id}
    )
    return {"id": interest_id, "event_id": event_id, "status": status}


def list_interests(job_seeker_id: str, status: str | None = None) -> list[dict[str, Any]]:
    return repo.list_interests(job_seeker_id, status=status)


def for_company(company_id: str, limit: int = 25) -> list[dict[str, Any]]:
    """Events linked to a company, for its profile screen (FR-462)."""
    return repo.events_for_company(company_id, limit)
