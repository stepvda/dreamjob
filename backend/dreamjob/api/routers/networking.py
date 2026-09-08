"""Networking and export API (FR-461, FR-462, FR-463, FR-385, NFR-301).

Three screens live here, and they are the ones that take the campaign outside
the application:

* **introductions** (FR-461) - the routes into a target company, ranked by
  strength and relevance, with the message to the intermediary;
* **events** (FR-462) - the radar, the calendar entry and the link back to the
  company profile;
* **exports** (FR-463) - the self-contained package for a career coach.

Every response carries ``discretion_mode`` so the FR-385 badge can be shown
here too, and every private read is scoped to the authenticated job seeker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from dreamjob.api.deps import CurrentSeeker, current_seeker
from dreamjob.api.routers.intelligence import discretion_state
from dreamjob.db.repositories import opportunities as opportunity_repo
from dreamjob.exporting import campaign_export
from dreamjob.exporting.json_export import ForeignDataError
from dreamjob.intelligence import events as events_mod
from dreamjob.pipeline import introductions as intro_mod

router = APIRouter()

Seeker = Annotated[CurrentSeeker, Depends(current_seeker)]

MAX_OPPORTUNITIES_PER_COMPANY = 3


class ProposeIntroductionsRequest(BaseModel):
    opportunity_id: str
    use_llm: bool = True
    language: str | None = Field(default=None, max_length=8)
    limit: int = Field(default=10, ge=1, le=50)


class PathStatusRequest(BaseModel):
    status: str = Field(max_length=40)


class CollectEventsRequest(BaseModel):
    """FR-462: the calendars and programme pages to read."""

    sources: list[str] = Field(default_factory=list, max_length=50)
    campaign_id: str | None = None
    keywords: list[str] = Field(default_factory=list, max_length=10)
    location: str | None = Field(default=None, max_length=120)
    use_social: bool = False


class EventInterestRequest(BaseModel):
    status: str = Field(default=events_mod.STATUS_INTERESTED, max_length=20)
    note: str | None = Field(default=None, max_length=1000)
    campaign_id: str | None = None


class ExportRequest(BaseModel):
    campaign_id: str
    language: str = Field(default="en", max_length=8)
    include_pdf: bool = True
    include_documents: bool = True
    include_contacts: bool = True


# ---------------------------------------------------------------------------
# FR-461: introduction routes
# ---------------------------------------------------------------------------


@router.get("/introductions")
def introductions(seeker: Seeker, opportunity_id: str) -> dict:
    """The stored routes and the cold-e-mail alternative for one opportunity."""
    try:
        options = intro_mod.outreach_options(seeker.id, opportunity_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {**options, "discretion_mode": discretion_state(seeker.id)["discretion_mode"]}


@router.post("/introductions")
def propose_introductions(seeker: Seeker, payload: ProposeIntroductionsRequest) -> dict:
    """Rank the routes and draft the message to the intermediary (FR-461)."""
    try:
        routes = intro_mod.propose_paths(
            seeker.id,
            payload.opportunity_id,
            limit=payload.limit,
            use_llm=payload.use_llm,
            language=payload.language,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {
        "opportunity_id": payload.opportunity_id,
        "routes": [route.as_dict() for route in routes],
        "count": len(routes),
        "discretion_mode": discretion_state(seeker.id)["discretion_mode"],
    }


@router.get("/introductions/company/{company_id}")
def introductions_for_company(
    seeker: Seeker,
    company_id: str,
    campaign_id: str | None = None,
    limit: int = Query(default=10, ge=1, le=50),
) -> dict:
    """FR-461 reads per *company*: the routes into it, however many roles it has.

    A company with three open roles has one network around it, so the routes
    are built per opportunity and then merged, keeping the best rank each
    intermediary achieved and remembering which role it was for.
    """
    filters: dict[str, Any] = {"company_id": company_id}
    if campaign_id:
        filters["campaign_id"] = campaign_id
    opportunities = opportunity_repo.list_opportunities(
        seeker.id, limit=MAX_OPPORTUNITIES_PER_COMPANY, **filters
    )
    if not opportunities:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "No opportunity at this company for this job seeker"
        )

    merged: dict[str, dict[str, Any]] = {}
    for opportunity in opportunities:
        for route in intro_mod.build_routes(seeker.id, str(opportunity["id"]), limit=limit):
            row = route.as_dict()
            row["opportunity_id"] = opportunity["id"]
            row["opportunity_title"] = opportunity.get("title")
            key = str(route.member_id or route.name)
            # ``IntroductionRoute.as_dict`` publishes the strength/relevance
            # rank under "score"; reading anything else would silently keep the
            # first route seen and leave the list unranked (FR-461).
            if key not in merged or route.rank_score > (merged[key].get("score") or 0):
                merged[key] = row
    routes = sorted(merged.values(), key=lambda r: -(r.get("score") or 0))[:limit]
    return {
        "company_id": company_id,
        "opportunities": [
            {"id": o["id"], "title": o.get("title")} for o in opportunities
        ],
        "routes": routes,
        "count": len(routes),
        "discretion_mode": discretion_state(seeker.id, campaign_id)["discretion_mode"],
        "note": (
            "Routes are ranked by strength (will they help) against relevance (are they "
            "close to the decision). Approaching anyone is the job seeker's decision "
            "(NFR-305)."
        ),
    }


@router.post("/introductions/{path_id}/status")
def set_path_status(seeker: Seeker, path_id: str, payload: PathStatusRequest) -> dict:
    from dreamjob.db.repositories import contacts as contacts_repo  # noqa: PLC0415

    updated = contacts_repo.set_path_status(path_id, seeker.id, payload.status)
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "introduction path not found")
    return updated


# ---------------------------------------------------------------------------
# FR-462: the event and community radar
# ---------------------------------------------------------------------------


@router.get("/events")
def events(
    seeker: Seeker,
    campaign_id: str | None = None,
    horizon_days: int = Query(default=events_mod.DEFAULT_HORIZON_DAYS, ge=1, le=730),
    limit: int = Query(default=events_mod.DEFAULT_LIMIT, ge=1, le=200),
    include_unreachable: bool = False,
) -> dict:
    """Upcoming events, matched to location, travel tolerance and targets."""
    return events_mod.radar(
        seeker.id,
        campaign_id,
        horizon_days=horizon_days,
        limit=limit,
        include_unreachable=include_unreachable,
    )


@router.post("/events/collect")
async def collect_events(seeker: Seeker, payload: CollectEventsRequest) -> dict:
    """Read the given calendars and programme pages for upcoming events (FR-462)."""
    if not payload.sources and not (payload.keywords and payload.location):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Give at least one source URL, or keywords together with a location",
        )
    result = await events_mod.collect(
        seeker.id,
        list(payload.sources),
        campaign_id=payload.campaign_id,
        keywords=list(payload.keywords),
        location=payload.location,
        use_social=payload.use_social,
    )
    return result


@router.get("/events/company/{company_id}")
def events_for_company(seeker: Seeker, company_id: str) -> dict:
    """Events linked to one company profile (FR-462)."""
    return {
        "company_id": company_id,
        "events": events_mod.for_company(company_id),
        "discretion_mode": discretion_state(seeker.id)["discretion_mode"],
    }


@router.get("/events/interests")
def event_interests(seeker: Seeker, status_filter: str | None = None) -> list:
    return events_mod.list_interests(seeker.id, status_filter)


@router.post("/events/{event_id}/interest")
def set_event_interest(seeker: Seeker, event_id: str, payload: EventInterestRequest) -> dict:
    try:
        return events_mod.set_interest(
            seeker.id,
            event_id,
            status=payload.status,
            note=payload.note,
            campaign_id=payload.campaign_id,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/events/{event_id}/calendar")
def add_event_to_calendar(
    seeker: Seeker,
    event_id: str,
    campaign_id: Annotated[str | None, Body(embed=True)] = None,
    note: Annotated[str | None, Body(embed=True)] = None,
) -> dict:
    """FR-462: add the event to the calendar (an .ics file, or a linked calendar)."""
    try:
        return events_mod.add_to_calendar(
            seeker.id, event_id, campaign_id=campaign_id, note=note
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.get("/events/{event_id}/calendar.ics", response_class=PlainTextResponse)
def event_ics(seeker: Seeker, event_id: str) -> PlainTextResponse:
    from dreamjob.db.repositories import intelligence as repo  # noqa: PLC0415

    event = repo.get_event(event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")
    return PlainTextResponse(
        events_mod.ics_document(event),
        media_type="text/calendar",
        headers={"Content-Disposition": f'attachment; filename="event-{event_id}.ics"'},
    )


@router.post("/events/{event_id}/link-companies")
def link_event_companies(
    seeker: Seeker, event_id: str, campaign_id: Annotated[str | None, Body(embed=True)] = None
) -> dict:
    try:
        return events_mod.link_to_companies(seeker.id, event_id, campaign_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


# ---------------------------------------------------------------------------
# FR-463: campaign export
# ---------------------------------------------------------------------------


@router.get("/exports")
def list_exports(seeker: Seeker, campaign_id: str | None = None) -> list:
    return campaign_export.list_exports(seeker.id, campaign_id)


@router.post("/exports")
def create_export(seeker: Seeker, payload: ExportRequest) -> dict:
    """Build the self-contained package for one campaign (FR-463)."""
    try:
        result = campaign_export.build_package(
            seeker.id,
            payload.campaign_id,
            language=payload.language,
            include_pdf=payload.include_pdf,
            include_documents=payload.include_documents,
            include_contacts=payload.include_contacts,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ForeignDataError as exc:  # pragma: no cover - the guard that must never fire
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return result.as_dict()


@router.get("/exports/{export_id}")
def get_export(seeker: Seeker, export_id: str) -> dict:
    export = campaign_export.get_export(seeker.id, export_id)
    if export is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "export not found")
    return export


@router.get("/exports/{export_id}/download")
def download_export(seeker: Seeker, export_id: str) -> FileResponse:
    export = campaign_export.get_export(seeker.id, export_id)
    if export is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "export not found")
    path = Path(str(export.get("zip_path") or ""))
    if not path.is_file():
        raise HTTPException(status.HTTP_410_GONE, "the export file is no longer on disk")
    return FileResponse(path, media_type="application/zip", filename=path.name)
