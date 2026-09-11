"""Directive editor API (FR-141..149, FR-385, NFR-501).

The endpoints here back a structured editor, not a text box: ``/vocabulary``
fills every drop-down in the interface language (NFR-501), ``/titles`` and
``/locations/search`` back the two auto-complete fields (FR-142, FR-144),
``/propose`` pre-fills the form from the composite profile and ``/estimate``
answers "how much will this collect?" before launch (FR-147).

Static paths are declared before ``/{directive_set_id}`` so that they are not
swallowed by the id route.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from dreamjob.api.deps import CurrentSeeker, current_seeker
from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import directives as repo
from dreamjob.pipeline import geocode as geo
from dreamjob.pipeline.directives import (
    CollectionEstimate,
    CommuteMode,
    CompanyReference,
    DirectiveProposal,
    DirectiveSet,
    DirectiveSetPayload,
    ExcludedCompany,
    ExcludedContact,
    TitleSuggestion,
    coerce_directive_set,
    contact_exclusion_reason,
    estimate_collection,
    exclusion_reason,
    propose_directives,
    resolve_locations,
    suggest_titles,
    vocabulary,
)
from dreamjob.security.audit import record_audit

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class ProposeIn(BaseModel):
    persona_id: str | None = None
    name: str = "Proposed directives"
    geocode: bool = True


class RebuildIn(BaseModel):
    """FR-147: rebuild and save, in one step.  Name is derived when omitted."""

    persona_id: str | None = None
    name: str | None = None
    geocode: bool = True


class EstimateIn(BaseModel):
    """Either an existing set or an unsaved draft from the editor (FR-147)."""

    directive_set_id: str | None = None
    directives: DirectiveSetPayload | None = None


class DiscretionIn(BaseModel):
    """FR-385 discretion flags, which live on the directive set."""

    discretion_mode: bool = True
    current_employer: CompanyReference | None = None
    excluded_companies: list[ExcludedCompany] = Field(default_factory=list)
    excluded_contacts: list[ExcludedContact] = Field(default_factory=list)


class DiscretionCheckIn(BaseModel):
    company: dict | None = None
    contact: dict | None = None


class Point(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class CommuteIn(BaseModel):
    origin: Point
    destination: Point
    mode: CommuteMode = CommuteMode.CAR


def _model(row: dict) -> DirectiveSet:
    return coerce_directive_set(row)


def _load(directive_set_id: str, seeker_id: str) -> dict:
    row = repo.get(directive_set_id, seeker_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Directive set not found")
    return row


# ---------------------------------------------------------------------------
# Vocabulary and auto-complete (FR-141, FR-142, FR-144, NFR-501)
# ---------------------------------------------------------------------------


@router.get("/vocabulary")
def get_vocabulary(
    locale: str = Query("en", pattern="^[a-z]{2}$"),
    seeker: CurrentSeeker = Depends(current_seeker),
) -> dict:
    """Every drop-down's options, labelled in the interface language (NFR-501)."""
    return vocabulary(locale or seeker.locale)


@router.get("/titles")
def autocomplete_titles(
    q: str = Query("", max_length=100),
    locale: str = Query("en", pattern="^[a-z]{2}$"),
    family: str | None = None,
    limit: int = Query(10, ge=1, le=50),
    seeker: CurrentSeeker = Depends(current_seeker),
) -> list[TitleSuggestion]:
    """Title auto-complete with synonyms, from the bundled catalogue (FR-142)."""
    return suggest_titles(q, locale=locale, limit=limit, family=family)


@router.get("/locations/search")
async def search_locations(
    q: str = Query(..., min_length=2, max_length=200),
    limit: int = Query(5, ge=1, le=10),
    countries: str | None = Query(None, description="Comma-separated ISO-2 codes"),
    locale: str = Query("en", pattern="^[a-z]{2}$"),
    seeker: CurrentSeeker = Depends(current_seeker),
) -> list[geo.GeocodeResult]:
    """Geocoder-backed place auto-complete (FR-144).

    Returns an empty list rather than an error when the geocoder cannot be
    reached, so the editor stays usable offline and keeps the typed label.
    """
    codes = [c.strip() for c in (countries or "").split(",") if c.strip()] or None
    return await geo.search(q, limit=limit, country_codes=codes, language=locale)


@router.post("/locations/commute")
def estimate_commute(
    body: CommuteIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    """Distance and door-to-door time between two points (FR-144)."""
    distance = geo.haversine_km(
        body.origin.latitude,
        body.origin.longitude,
        body.destination.latitude,
        body.destination.longitude,
    )
    return {
        "distance_km": round(distance, 2),
        "mode": body.mode.value,
        "minutes": round(geo.commute_minutes(distance, body.mode.value), 1),
        "model": "circuity-corrected straight-line distance, distance-blended speed",
    }


# ---------------------------------------------------------------------------
# Proposal and estimate (FR-147)
# ---------------------------------------------------------------------------


@router.post("/propose")
async def propose(
    body: ProposeIn = Body(default_factory=ProposeIn),
    seeker: CurrentSeeker = Depends(current_seeker),
) -> DirectiveProposal:
    """Pre-fill directives from the composite profile (FR-147).

    Nothing is saved: the job seeker reviews the proposal, edits it, and posts
    it back to ``/`` when it is right.
    """
    composite = repo.latest_composite_profile(seeker.id, body.persona_id)
    dream = repo.latest_dream_job_model(seeker.id, body.persona_id)
    profile = repo.latest_profile_version(seeker.id)
    if composite is None and profile is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No profile to propose from yet; upload a profile first (FR-102).",
        )

    proposal = propose_directives(composite, dream, profile, name=body.name)
    if body.persona_id:
        proposal.directives.persona_id = body.persona_id
    if body.geocode:
        proposal.directives.location = await resolve_locations(
            proposal.directives.location, language=seeker.locale
        )
        if any(a.is_geocoded for a in proposal.directives.location.areas):
            proposal.unresolved = [
                u for u in proposal.unresolved if u != "location.areas[0].coordinates"
            ]
    return proposal


@router.post("/rebuild", status_code=status.HTTP_201_CREATED)
async def rebuild(
    body: RebuildIn = Body(default_factory=lambda: RebuildIn()),
    seeker: CurrentSeeker = Depends(current_seeker),
) -> dict:
    """Rebuild the search from the current profile in one step (FR-147).

    Proposes a fresh directive set from the composite profile and the dream-job
    model, geocodes it, and **saves it as a new version** rather than asking the
    seeker to review a form first.  This is the repair path for a search that
    has drifted: the directives behind it were proposed once, from an older
    profile, and every campaign planned since has inherited whatever they said.

    Saving a new version is deliberate - the previous set is untouched, so a
    campaign that ran under it stays explainable and the change can be reverted
    by loading the older version (FR-148).
    """
    composite = repo.latest_composite_profile(seeker.id, body.persona_id)
    dream = repo.latest_dream_job_model(seeker.id, body.persona_id)
    profile = repo.latest_profile_version(seeker.id)
    if composite is None and profile is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No profile to rebuild from yet; upload a profile first (FR-102).",
        )

    name = body.name or f"Rebuilt from profile {utcnow()[:10]}"
    proposal = propose_directives(composite, dream, profile, name=name)
    if body.persona_id:
        proposal.directives.persona_id = body.persona_id
    if body.geocode:
        proposal.directives.location = await resolve_locations(
            proposal.directives.location, language=seeker.locale
        )
        if any(a.is_geocoded for a in proposal.directives.location.areas):
            proposal.unresolved = [
                u for u in proposal.unresolved if u != "location.areas[0].coordinates"
            ]

    saved = await asyncio.to_thread(repo.create, seeker.id, proposal.directives)
    await asyncio.to_thread(
        record_audit,
        "directives.rebuilt",
        "directive_set",
        saved,
        seeker_id=seeker.id,
        detail={
            "titles": proposal.directives.job_content.target_titles[:6],
            "seniority": [
                str(proposal.directives.job_content.seniority_min),
                str(proposal.directives.job_content.seniority_max),
            ],
            "countries": proposal.directives.location.countries,
        },
    )
    return {
        "directive_set": repo.get(saved, seeker.id),
        "provenance": proposal.provenance,
        "unresolved": proposal.unresolved,
    }


@router.post("/estimate")
def estimate(
    body: EstimateIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> CollectionEstimate:
    """Sources, queries and pages this campaign would collect (FR-147, FR-163)."""
    if body.directive_set_id:
        directives = _load(body.directive_set_id, seeker.id)
    elif body.directives is not None:
        directives = body.directives
    else:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Provide either directive_set_id or directives",
        )
    return estimate_collection(directives, repo.list_enabled_sources())


# ---------------------------------------------------------------------------
# Directive sets (FR-141, FR-148)
# ---------------------------------------------------------------------------


@router.get("/")
def list_directive_sets(
    all_versions: bool = False, seeker: CurrentSeeker = Depends(current_seeker)
) -> list[DirectiveSet]:
    """Saved sets, latest version of each name unless all_versions (FR-148)."""
    return [_model(row) for row in repo.list_for_seeker(seeker.id, all_versions=all_versions)]


@router.post("/", status_code=status.HTTP_201_CREATED)
def create_directive_set(
    payload: DirectiveSetPayload, seeker: CurrentSeeker = Depends(current_seeker)
) -> DirectiveSet:
    """Save a directive set.  Re-using a name adds a version (FR-148)."""
    new_id = repo.create(seeker.id, payload)
    return _model(_load(new_id, seeker.id))


@router.get("/{directive_set_id}")
def get_directive_set(
    directive_set_id: str, seeker: CurrentSeeker = Depends(current_seeker)
) -> DirectiveSet:
    return _model(_load(directive_set_id, seeker.id))


@router.put("/{directive_set_id}")
def update_directive_set(
    directive_set_id: str,
    payload: DirectiveSetPayload,
    seeker: CurrentSeeker = Depends(current_seeker),
) -> DirectiveSet:
    """Edit a set in place.  Once a campaign has used it, version it instead."""
    _load(directive_set_id, seeker.id)
    if repo.campaign_usage(directive_set_id, seeker.id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This directive set has been used by a campaign; POST a new version instead.",
        )
    row = repo.update_in_place(directive_set_id, seeker.id, payload)
    return _model(row or _load(directive_set_id, seeker.id))


@router.post("/{directive_set_id}/versions", status_code=status.HTTP_201_CREATED)
def create_version(
    directive_set_id: str,
    payload: DirectiveSetPayload,
    seeker: CurrentSeeker = Depends(current_seeker),
) -> DirectiveSet:
    """Save an edited set as the next version of the same name (FR-148).

    The name is taken from the set being versioned, whatever the body says: a
    version belongs to its name line.  Use ``/duplicate`` to start a new one.
    """
    current = _load(directive_set_id, seeker.id)
    payload.name = current["name"]
    new_id = repo.create(seeker.id, payload)
    return _model(_load(new_id, seeker.id))


@router.get("/{directive_set_id}/versions")
def list_set_versions(
    directive_set_id: str, seeker: CurrentSeeker = Depends(current_seeker)
) -> list[DirectiveSet]:
    row = _load(directive_set_id, seeker.id)
    return [_model(v) for v in repo.list_versions(seeker.id, row["name"])]


@router.post("/{directive_set_id}/duplicate", status_code=status.HTTP_201_CREATED)
def duplicate_directive_set(
    directive_set_id: str,
    name: str = Body(..., embed=True, min_length=1, max_length=120),
    seeker: CurrentSeeker = Depends(current_seeker),
) -> DirectiveSet:
    """Re-use a set for another campaign under a new name (FR-148)."""
    source = coerce_directive_set(_load(directive_set_id, seeker.id))
    payload = DirectiveSetPayload.model_validate(
        source.model_dump(exclude={"id", "job_seeker_id", "version", "created_at"})
    )
    payload.name = name
    return _model(_load(repo.create(seeker.id, payload), seeker.id))


@router.delete("/{directive_set_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_directive_set(
    directive_set_id: str, seeker: CurrentSeeker = Depends(current_seeker)
) -> None:
    _load(directive_set_id, seeker.id)
    try:
        repo.delete(directive_set_id, seeker.id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/{directive_set_id}/estimate")
def estimate_for_set(
    directive_set_id: str, seeker: CurrentSeeker = Depends(current_seeker)
) -> CollectionEstimate:
    """FR-147 estimate for a saved set."""
    return estimate_collection(_load(directive_set_id, seeker.id), repo.list_enabled_sources())


# ---------------------------------------------------------------------------
# Discretion mode (FR-385)
# ---------------------------------------------------------------------------


@router.put("/{directive_set_id}/discretion")
def set_discretion(
    directive_set_id: str,
    body: DiscretionIn,
    seeker: CurrentSeeker = Depends(current_seeker),
) -> DirectiveSet:
    """Turn discretion mode on and list what must stay out of the campaign (FR-385)."""
    _load(directive_set_id, seeker.id)
    excluded = list(body.excluded_companies)
    if body.current_employer is not None:
        employer = ExcludedCompany(
            **body.current_employer.model_dump(), reason="current_employer"
        )
        excluded = [employer, *(e for e in excluded if e.name != employer.name)]
    row = repo.update_columns(
        directive_set_id,
        seeker.id,
        {
            "discretion_mode": 1 if body.discretion_mode else 0,
            "discretion_excluded_companies": [e.model_dump(mode="json") for e in excluded],
            "discretion_excluded_contacts": [
                c.model_dump(mode="json") for c in body.excluded_contacts
            ],
        },
    )
    return _model(row or _load(directive_set_id, seeker.id))


@router.post("/{directive_set_id}/discretion/check")
def check_discretion(
    directive_set_id: str,
    body: DiscretionCheckIn,
    seeker: CurrentSeeker = Depends(current_seeker),
) -> dict:
    """Explain how a company or contact is judged under the current flags (FR-385).

    The editor uses it to show the effect of an exclusion - including the group
    entities caught by name prefix and shared domain - before a campaign runs.
    """
    directives = _load(directive_set_id, seeker.id)
    result: dict = {"discretion_mode": bool(directives["discretion_mode"])}
    if body.company is not None:
        reason = exclusion_reason(directives, body.company)
        result["company"] = {"excluded": reason is not None, "reason": reason}
    if body.contact is not None:
        reason = contact_exclusion_reason(directives, body.contact)
        result["contact"] = {"excluded": reason is not None, "reason": reason}
    return result
