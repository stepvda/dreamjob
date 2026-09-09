"""Dream-job intelligence API (FR-381..385, FR-443).

The four screens behind this router all answer the same question from
different angles - "the market is not offering my dream job; now what?" - so
they share one shape: a stored reading that can be re-read cheaply, and an
explicit recompute that may call the model.

**FR-385 runs through every response.**  ``discretion_mode`` is returned by
every route in this module, because the requirement asks for it to be visibly
indicated *throughout* the UI: a screen cannot show the badge it was never
told about.  Exclusions themselves are enforced in the modules below, through
``directives.is_excluded`` / ``is_excluded_contact``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from dreamjob.api.deps import CurrentSeeker, current_seeker
from dreamjob.db.connection import from_json
from dreamjob.db.repositories import intelligence as repo
from dreamjob.intelligence import gap_analysis as gaps_mod
from dreamjob.intelligence import linkedin_advice as linkedin_mod
from dreamjob.intelligence import stepping_stones as stones_mod
from dreamjob.intelligence import values_match as values_mod

router = APIRouter()

# FR-101: every route is scoped to the authenticated job seeker.
Seeker = Annotated[CurrentSeeker, Depends(current_seeker)]


class ComputeRequest(BaseModel):
    campaign_id: str | None = None
    use_llm: bool = True


class SteppingStoneRequest(ComputeRequest):
    #: FR-382 fires when nothing clears the threshold; ``force`` asks for the
    #: longer route anyway.
    force: bool = False
    threshold: float | None = Field(default=None, ge=0, le=100)


class ThresholdRequest(BaseModel):
    threshold: float = Field(ge=0, le=100)


class LinkedInRequest(ComputeRequest):
    language: str = Field(default="en", max_length=8)


# ---------------------------------------------------------------------------
# FR-385: the flag every screen needs
# ---------------------------------------------------------------------------


def discretion_state(job_seeker_id: str, campaign_id: str | None = None) -> dict[str, Any]:
    """The FR-385 badge, plus what it is currently suppressing."""
    campaign, directives = repo.directives_in_force(job_seeker_id, campaign_id)
    on = bool((directives or {}).get("discretion_mode"))
    return {
        "discretion_mode": on,
        "directive_set_id": (directives or {}).get("id"),
        "campaign_id": (campaign or {}).get("id"),
        "excluded_companies": len(
            from_json((directives or {}).get("discretion_excluded_companies"), []) or []
        ),
        "excluded_contacts": len(
            from_json((directives or {}).get("discretion_excluded_contacts"), []) or []
        ),
        "suppressed": (
            [
                "LinkedIn profile edits and the 'open to work' banner",
                "contacts likely to expose the search",
                "excluded companies and their group entities",
            ]
            if on
            else []
        ),
    }


@router.get("/discretion")
def discretion(seeker: Seeker, campaign_id: str | None = None) -> dict:
    """FR-385: is discretion mode on, and what is it hiding?"""
    return discretion_state(seeker.id, campaign_id)


@router.get("")
def overview(seeker: Seeker, campaign_id: str | None = None) -> dict:
    """What this seeker's dream-job intelligence currently says."""
    state = discretion_state(seeker.id, campaign_id)
    campaign_id = state["campaign_id"]
    stored_gap = repo.get_gap_analysis(seeker.id, campaign_id)
    assessment = stones_mod.assess(seeker.id, campaign_id)
    advice = repo.latest_advice(seeker.id, campaign_id)
    return {
        "campaign_id": campaign_id,
        "discretion_mode": state["discretion_mode"],
        "gap_analysis": {
            "computed_at": (stored_gap or {}).get("updated_at")
            or (stored_gap or {}).get("created_at"),
            "gaps": len((stored_gap or {}).get("gaps") or []),
            "summary": (stored_gap or {}).get("summary"),
        },
        "dream_fit": {
            "threshold": assessment["threshold"],
            "best": assessment["best_dream_fit"],
            "destinations": len(assessment["destinations"]),
            "stepping_stones_needed": assessment["triggered"],
            "paths_stored": len(repo.list_stepping_stones(seeker.id, campaign_id)),
        },
        "linkedin_advice": {
            "generated_at": (advice or {}).get("created_at"),
            "edited": bool((advice or {}).get("edited_text")),
        },
    }


# ---------------------------------------------------------------------------
# FR-381: gap analysis
# ---------------------------------------------------------------------------


@router.get("/gap-analysis")
def get_gap_analysis(seeker: Seeker, campaign_id: str | None = None) -> dict:
    """The stored analysis (FR-381).  ``analysis`` is null until it is run."""
    stored = gaps_mod.stored(seeker.id, campaign_id)
    return {
        "analysis": stored,
        "discretion_mode": discretion_state(seeker.id, campaign_id)["discretion_mode"],
    }


@router.post("/gap-analysis")
def run_gap_analysis(seeker: Seeker, payload: ComputeRequest) -> dict:
    """Recompute the gap analysis (FR-381)."""
    report = gaps_mod.analyse(seeker.id, payload.campaign_id, use_llm=payload.use_llm)
    return report.as_dict()


# ---------------------------------------------------------------------------
# FR-382: stepping stones
# ---------------------------------------------------------------------------


@router.get("/stepping-stones")
def get_stepping_stones(seeker: Seeker, campaign_id: str | None = None) -> dict:
    assessment = stones_mod.assess(seeker.id, campaign_id)
    return {
        **assessment,
        "paths": stones_mod.stored(seeker.id, campaign_id),
        "discretion_mode": discretion_state(seeker.id, campaign_id)["discretion_mode"],
    }


@router.post("/stepping-stones")
def run_stepping_stones(seeker: Seeker, payload: SteppingStoneRequest) -> dict:
    if payload.threshold is not None:
        stones_mod.set_threshold(payload.threshold, seeker.id)
    report = stones_mod.propose(
        seeker.id, payload.campaign_id, use_llm=payload.use_llm, force=payload.force
    )
    return report.as_dict()


@router.put("/stepping-stones/threshold")
def put_threshold(seeker: Seeker, payload: ThresholdRequest) -> dict:
    """FR-382's threshold is configurable; it is an application setting."""
    if not seeker.id:  # pragma: no cover - defensive; the dependency guarantees it
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return {"threshold": stones_mod.set_threshold(payload.threshold, seeker.id)}


@router.post("/stepping-stones/tags")
def tag_opportunities(
    seeker: Seeker, campaign_id: Annotated[str | None, Body(embed=True)] = None
) -> dict:
    """Apply the FR-382 tags to the ranked list, on the seeker's explicit request."""
    return stones_mod.apply_tags(seeker.id, campaign_id)


# ---------------------------------------------------------------------------
# FR-384: values match
# ---------------------------------------------------------------------------


@router.get("/values-match/{company_id}")
def values_for_company(
    seeker: Seeker,
    company_id: str,
    campaign_id: str | None = None,
    use_llm: bool = Query(default=False),
) -> dict:
    """The explicit warnings FR-384 asks for on the company profile."""
    try:
        match = values_mod.for_company(
            seeker.id, company_id, campaign_id=campaign_id, use_llm=use_llm
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return match.as_dict()


@router.get("/values-match/{company_id}/document")
def values_for_document(seeker: Seeker, company_id: str, campaign_id: str | None = None) -> dict:
    """What the motivation and fit document should carry (FR-384 -> FR-330)."""
    try:
        return values_mod.warnings_for_document(seeker.id, company_id, campaign_id=campaign_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


# ---------------------------------------------------------------------------
# FR-443: LinkedIn advice
# ---------------------------------------------------------------------------


@router.get("/linkedin")
def get_linkedin_advice(seeker: Seeker, campaign_id: str | None = None) -> dict:
    advice = linkedin_mod.latest(seeker.id, campaign_id)
    return {
        "advice": advice,
        "discretion_mode": discretion_state(seeker.id, campaign_id)["discretion_mode"],
        "note": (
            "Dream Job never signs in to LinkedIn and never edits a profile; these are "
            "suggestions to apply by hand (FR-443)."
        ),
    }


@router.get("/linkedin/history")
def list_linkedin_advice(seeker: Seeker, limit: int = Query(default=20, ge=1, le=100)) -> list:
    return repo.list_advice(seeker.id, limit)


@router.post("/linkedin")
def generate_linkedin_advice(seeker: Seeker, payload: LinkedInRequest) -> dict:
    advice = linkedin_mod.suggest(
        seeker.id, payload.campaign_id, language=payload.language, use_llm=payload.use_llm
    )
    return advice.as_dict()


@router.patch("/linkedin/{advice_id}")
def edit_linkedin_advice(
    seeker: Seeker, advice_id: str, text: Annotated[str, Body(embed=True)]
) -> dict:
    """FR-443: the suggestions are editable text; this stores the seeker's edit."""
    updated = linkedin_mod.save_edit(seeker.id, advice_id, text)
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "advice not found")
    return updated


@router.delete("/linkedin/{advice_id}")
def delete_linkedin_advice(seeker: Seeker, advice_id: str) -> dict:
    deleted = repo.delete_advice(advice_id, seeker.id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "advice not found")
    return {"deleted": deleted}
