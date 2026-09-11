"""Opportunities API: ranking, comparison, user control (FR-261..285, FR-383).

The route order follows how the screen is used: look at the ranked list, filter
it, open one, compare a few side by side, then take control of the order.

Two rules run through every response in this module.

**FR-263** - a speculative opening is never presented as a vacancy.  Every
opportunity that leaves here carries ``kind``, ``is_speculative``,
``kind_label`` and ``disclosure_note``, produced by
:mod:`dreamjob.pipeline.speculative`, so no client can render one without the
distinction being available.

**NFR-305 / CR-405** - scoring is advisory.  Recalculation writes scores and
nothing else; the manual order, pins, tags and statuses belong to the job
seeker and no automated pass may touch them (FR-284).
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from dreamjob.api.deps import CurrentSeeker, current_seeker, owned_or_404
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.db.repositories import opportunities as repo
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.pipeline import compensation as comp_mod
from dreamjob.pipeline import employer_product, scoring, speculative
from dreamjob.pipeline import opportunities as synth

router = APIRouter()

# FR-101: every route is scoped to the authenticated job seeker.
Seeker = Annotated[CurrentSeeker, Depends(current_seeker)]

USER_STATUSES = ("new", "interested", "not_interested", "applied")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class UserControls(BaseModel):
    """FR-284: everything the job seeker decides for themselves."""

    selected: bool | None = None
    pinned: bool | None = None
    user_status: str | None = None
    not_interested_reason: str | None = Field(default=None, max_length=1000)
    tags: list[str] | None = None
    language: str | None = Field(default=None, max_length=8)


class ReorderRequest(BaseModel):
    """FR-284: the seeker's own order, top-first.  Survives recalculation."""

    campaign_id: str
    ordered_ids: list[str] = Field(min_length=1, max_length=500)


class ClearOrderRequest(BaseModel):
    campaign_id: str
    opportunity_ids: list[str] | None = None


class CampaignRequest(BaseModel):
    campaign_id: str
    background: bool = False


class SynthesiseRequest(CampaignRequest):
    include_knowledge_base: bool = True
    window_days: int | None = Field(default=None, ge=1, le=365)


class SpeculativeRequest(CampaignRequest):
    max_companies: int = Field(default=40, ge=1, le=200)
    max_openings: int = Field(default=speculative.DEFAULT_MAX_OPENINGS, ge=1, le=8)
    language: str = "en"
    background: bool = True


class RecalculateRequest(CampaignRequest):
    use_llm: bool = True
    language: str | None = None
    background: bool = True


class ScoreUnscoredRequest(BaseModel):
    """FR-281 backfill.  No campaign means every campaign that needs it."""

    campaign_id: str | None = None
    limit: int | None = Field(default=None, ge=1)
    page: int = Field(default=500, ge=50, le=2000)


class PruneRequest(BaseModel):
    """FR-142/FR-144 clean-up.  Previews unless ``dry_run`` is turned off."""

    campaign_id: str | None = None
    dry_run: bool = True


class WeightsIn(BaseModel):
    """FR-281: the weights are configurable, per job seeker."""

    weights: dict[str, float]


class CompareRequest(BaseModel):
    opportunity_ids: list[str] = Field(min_length=2, max_length=6)


# ---------------------------------------------------------------------------
# Presentation (FR-263, FR-283)
# ---------------------------------------------------------------------------

COMPARISON_FIELDS = (
    "title", "company_name", "kind", "score", "score_profile_fit", "score_dream_fit",
    "score_directive_fit", "score_company", "score_compensation", "score_plausibility",
    "score_reachability", "seniority", "function_family", "location", "country",
    "work_arrangement", "remote_days", "contract_type", "fte_percentage",
    "comp_min", "comp_max", "comp_currency", "comp_confidence", "comp_is_stated",
    "employer_rating", "plausibility", "posted_at", "timing_flag", "user_status",
)


def _employer_tag(row: dict, locale: str | None = None) -> dict:
    """The employer-kind badge for one list row (FR-143, FR-263, NFR-502).

    Rendered from the columns ``_LIST_SELECT`` already joined, so a page of
    fifty rows costs no extra query.  A company with no verdict comes back as
    *not researched* rather than blank: an empty cell reads as "employer",
    which is the assumption this axis exists to stop.  The evidence is not
    here on purpose - the popover fetches it from ``/api/employers/{id}/kind``
    when a reader asks for it.
    """
    return employer_product.badge_from_row(row, locale or "en")


def _present(opportunity: dict, locale: str | None = None) -> dict:
    """Add the FR-263 labelling and the FR-283 links to one row.

    ``locale`` is the *reader's*, not the posting's: the badge is a sentence
    the product says to a job seeker, so it follows the interface language,
    while the disclosure note quotes the advertisement in the advertisement's
    own words.  Reading the badge off ``opportunity['language']`` put a Dutch
    badge on an English screen.
    """
    out = dict(opportunity)
    out.update(speculative.presentation(opportunity))
    out["employer"] = _employer_tag(opportunity, locale)
    company_id = opportunity.get("company_id")
    out["links"] = {
        "company_profile": f"/api/companies/{company_id}" if company_id else None,
        "source_vacancy": opportunity.get("source_url") or opportunity.get("vacancy_source_url"),
        "knowledge_base_vacancy": (
            f"/api/opportunities/{opportunity['id']}/vacancy"
            if opportunity.get("vacancy_id") else None
        ),
    }
    return out


def _campaign_or_404(campaign_id: str, seeker_id: str) -> dict:
    return owned_or_404("campaign", campaign_id, seeker_id)


def _opportunity_or_404(opportunity_id: str, seeker_id: str) -> dict:
    row = repo.get_opportunity(opportunity_id, seeker_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "opportunity not found")
    return row


async def _run(kind: str, campaign: dict, work: Any, *, background: bool) -> dict:
    """Run a pipeline pass inline, or as a resumable job (FR-185, NFR-401).

    The pipeline passes are synchronous and can be slow, so a background run
    hands them to a worker thread rather than blocking the event loop.
    """
    if not background:
        return await asyncio.to_thread(work)

    job_id = runner.create(
        kind, campaign_id=campaign["id"], job_seeker_id=campaign["job_seeker_id"]
    )

    async def worker(ctx: JobContext) -> None:
        result = await asyncio.to_thread(work)
        ctx.save_checkpoint(result=result)

    await runner.start(job_id, worker)
    return {"job_id": job_id, "status": "running", "campaign_id": campaign["id"]}


# ---------------------------------------------------------------------------
# The ranked list (FR-283, FR-284)
# ---------------------------------------------------------------------------


@router.get("")
def list_opportunities(
    seeker: Seeker,
    campaign_id: str | None = None,
    company_id: str | None = None,
    kind: str | None = None,
    user_status: Annotated[list[str] | None, Query()] = None,
    tag: str | None = None,
    q: str | None = None,
    country: str | None = None,
    work_arrangement: str | None = None,
    contract_type: str | None = None,
    seniority: str | None = None,
    function_family: str | None = None,
    timing_flag: str | None = None,
    min_score: Annotated[float | None, Query(ge=0, le=100)] = None,
    min_plausibility: Annotated[float | None, Query(ge=0, le=1)] = None,
    selected: bool | None = None,
    pinned: bool | None = None,
    has_compensation: bool = False,
    exclude_not_interested: bool = False,
    sort: str = "score",
    respect_manual_order: bool = True,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """The ranked list, filtered and sorted (FR-283).

    ``respect_manual_order`` defaults to on: the job seeker's own positions come
    first, then pins, then the computed score (FR-284).
    """
    if sort not in repo.SORT_EXPRESSIONS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"sort must be one of {sorted(repo.SORT_EXPRESSIONS)}",
        )
    filters: dict[str, Any] = {
        "campaign_id": campaign_id,
        "company_id": company_id,
        "kind": kind,
        "user_status": user_status,
        "tag": tag,
        "q": q,
        "country": country,
        "work_arrangement": work_arrangement,
        "contract_type": contract_type,
        "seniority": seniority,
        "function_family": function_family,
        "timing_flag": timing_flag,
        "min_score": min_score,
        "min_plausibility": min_plausibility,
        "selected": selected,
        "pinned": pinned,
        "has_compensation": has_compensation,
        "exclude_not_interested": exclude_not_interested,
    }
    rows = repo.list_opportunities(
        seeker.id, sort=sort, respect_manual_order=respect_manual_order,
        limit=limit, offset=offset, **filters,
    )
    return {
        "items": [_present(r, seeker.locale) for r in rows],
        "total": repo.count_opportunities(seeker.id, **filters),
        "limit": limit,
        "offset": offset,
        "sort": sort,
        "respect_manual_order": respect_manual_order,
        "advisory": scoring.ADVISORY_NOTE,
    }


@router.get("/facets")
def facets(seeker: Seeker, campaign_id: str | None = None) -> dict:
    """Values present in this seeker's opportunities, for the filter controls."""
    return repo.facets(seeker.id, campaign_id)


@router.get("/summary")
def summary(seeker: Seeker, campaign_id: str) -> dict:
    _campaign_or_404(campaign_id, seeker.id)
    totals = repo.campaign_totals(seeker.id, campaign_id)
    return {**totals, "advisory": scoring.ADVISORY_NOTE}


@router.post("/compare")
def compare(seeker: Seeker, payload: CompareRequest) -> dict:
    """Side-by-side comparison of a hand-picked set (FR-283)."""
    rows = repo.get_by_ids(payload.opportunity_ids, seeker.id)
    if len(rows) < 2:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "at least two of your opportunities are needed"
        )
    presented = [_present(r, seeker.locale) for r in rows]
    return {
        "items": presented,
        "fields": list(COMPARISON_FIELDS),
        "matrix": {
            field_name: [row.get(field_name) for row in presented]
            for field_name in COMPARISON_FIELDS
        },
        "dream_fit_detail": [row.get("dream_fit_detail") for row in presented],
        "advisory": scoring.ADVISORY_NOTE,
    }


# ---------------------------------------------------------------------------
# Weights and feedback (FR-281, FR-285)
# ---------------------------------------------------------------------------


@router.get("/weights")
def get_weights(seeker: Seeker) -> dict:
    stored = repo.get_weights(seeker.id)
    return {
        "weights": scoring.load_weights(seeker.id),
        "defaults": scoring.DEFAULT_WEIGHTS,
        "components": list(scoring.COMPONENTS),
        "learned_from": (stored or {}).get("learned_from"),
        "updated_at": (stored or {}).get("updated_at"),
    }


@router.put("/weights")
def put_weights(seeker: Seeker, payload: WeightsIn) -> dict:
    """FR-281: the seeker sets the weights; unknown keys are rejected."""
    unknown = set(payload.weights) - set(scoring.DEFAULT_WEIGHTS)
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"unknown weight component(s): {sorted(unknown)}"
        )
    weights = scoring.save_weights(seeker.id, payload.weights, {"source": "set_by_user"})
    return {"weights": weights, "note": "recalculate to apply the new weights"}


@router.post("/weights/learn")
def learn_weights(seeker: Seeker, apply: bool = True) -> dict:
    """FR-285: re-tune the weights from pins and rejections."""
    return scoring.learn_weights(seeker.id, apply=apply)


@router.get("/directive-suggestions")
def directive_suggestions(seeker: Seeker) -> dict:
    """FR-285: what the rejections suggest about the directives.  Suggestions only."""
    return {
        "suggestions": scoring.suggest_directive_refinements(seeker.id),
        "advisory": scoring.ADVISORY_NOTE,
    }


# ---------------------------------------------------------------------------
# Manual order (FR-284)
# ---------------------------------------------------------------------------


@router.post("/reorder")
def reorder(seeker: Seeker, payload: ReorderRequest) -> dict:
    """FR-284: write the seeker's own order.  Recalculation never touches it."""
    _campaign_or_404(payload.campaign_id, seeker.id)
    written = repo.set_manual_order(seeker.id, payload.campaign_id, payload.ordered_ids)
    if written == 0:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "none of those opportunities are in that campaign"
        )
    return {
        "reordered": written,
        "note": "manual order overrides the computed order and survives recalculation",
    }


@router.post("/reorder/clear")
def clear_order(seeker: Seeker, payload: ClearOrderRequest) -> dict:
    _campaign_or_404(payload.campaign_id, seeker.id)
    cleared = repo.clear_manual_order(seeker.id, payload.campaign_id, payload.opportunity_ids)
    return {"cleared": cleared}


# ---------------------------------------------------------------------------
# Pipeline passes (FR-261, FR-262, FR-264, FR-281)
# ---------------------------------------------------------------------------


@router.post("/synthesise")
async def synthesise(seeker: Seeker, payload: SynthesiseRequest) -> dict:
    """FR-261: normalise every collected vacancy into an opportunity record."""
    campaign = _campaign_or_404(payload.campaign_id, seeker.id)

    def work() -> dict:
        result = synth.synthesise_campaign(
            campaign,
            include_knowledge_base=payload.include_knowledge_base,
            window_days=payload.window_days,
        ).as_dict()
        # FR-149: a spontaneous-application campaign plans no job board and no
        # ATS, so synthesis alone can only ever hand back an empty list.  The
        # track that gives such a campaign its opportunities is the speculative
        # one, and it runs here rather than waiting for a button the seeker has
        # no reason to know they must press.
        if speculative.is_spontaneous_campaign(campaign):
            try:
                result["speculative"] = speculative.generate_campaign(campaign).as_dict()
            except speculative.ConsentRequired as exc:
                result["speculative"] = {"error": "consent_required", "detail": str(exc)}
        # FR-264, by the same argument: pricing is pure corpus work, it costs
        # nothing, and a freshly synthesised list that shows no range anywhere
        # reads as a missing feature rather than as a pass not yet run.
        result["compensation"] = comp_mod.enrich_campaign(seeker.id, payload.campaign_id)
        # FR-281, last and for the same reason: a synthesised opportunity with no
        # score sorts to the bottom of the ranked list and reads as broken.  The
        # rows just created are scored immediately, so the list is usable the
        # moment synthesis returns.
        result["scored"] = scoring.score_unscored(seeker.id, payload.campaign_id)
        return result

    return await _run("scoring", campaign, work, background=payload.background)


@router.post("/speculative")
async def generate_speculative(seeker: Seeker, payload: SpeculativeRequest) -> dict:
    """FR-262: openings for interesting companies with no matching vacancy.

    Under budget pressure the low-ranked companies are skipped first (NFR-104);
    the report says which, and why.
    """
    campaign = _campaign_or_404(payload.campaign_id, seeker.id)

    def work() -> dict:
        try:
            result = speculative.generate_campaign(
                campaign,
                max_companies=payload.max_companies,
                max_openings=payload.max_openings,
                language=payload.language,
            ).as_dict()
        except speculative.ConsentRequired as exc:
            return {"error": "consent_required", "detail": str(exc)}
        # FR-281: a speculative opening is an opportunity like any other and must
        # not appear unscored either.
        result["scored"] = scoring.score_unscored(seeker.id, payload.campaign_id)
        return result

    return await _run("generation", campaign, work, background=payload.background)


@router.post("/compensation")
async def enrich_compensation(seeker: Seeker, payload: CampaignRequest) -> dict:
    """FR-264/FR-265: estimate a range with confidence and sources, per opportunity."""
    campaign = _campaign_or_404(payload.campaign_id, seeker.id)

    def work() -> dict:
        return comp_mod.enrich_campaign(seeker.id, payload.campaign_id)

    return await _run("scoring", campaign, work, background=payload.background)


@router.post("/recalculate")
async def recalculate(seeker: Seeker, payload: RecalculateRequest) -> dict:
    """FR-281/282: recompute every score.  Manual order is left untouched (FR-284).

    The FR-264 compensation pass runs first.  It costs no tokens and no network,
    and the compensation sub-score reads the stored estimate rather than
    recomputing it - so without this, a recalculation would score every
    unpriced opportunity against a range that nothing had ever filled in.
    """
    campaign = _campaign_or_404(payload.campaign_id, seeker.id)

    def work() -> dict:
        compensation = comp_mod.enrich_campaign(seeker.id, payload.campaign_id)
        report = scoring.score_campaign(
            campaign, use_llm=payload.use_llm, language=payload.language
        ).as_dict()
        return {**report, "compensation": compensation}

    return await _run("scoring", campaign, work, background=payload.background)


@router.post("/prune-irrelevant")
def prune_irrelevant_endpoint(seeker: Seeker, payload: PruneRequest) -> dict:
    """Delete opportunities the current directives no longer admit (FR-142, FR-144).

    The gates run when a vacancy becomes an opportunity.  A campaign collected
    before they existed - or before the directives were rebuilt - keeps rows
    they would reject today.  This re-applies them and removes what fails,
    never touching a row the seeker has decided about (NFR-305).
    """
    if payload.campaign_id:
        _campaign_or_404(payload.campaign_id, seeker.id)
    report = synth.prune_irrelevant(seeker.id, payload.campaign_id, dry_run=payload.dry_run)
    return report.as_dict()


@router.post("/score-unscored")
def score_unscored_endpoint(seeker: Seeker, payload: ScoreUnscoredRequest) -> dict:
    """Backfill: score the opportunities that have no score yet (FR-281).

    Distinct from ``/recalculate``, which re-scores everything. This touches
    only rows whose ``score`` is null, so it is the right tool after a change
    that added opportunities without ranking them - and it is safe to run when
    nothing is missing, because then it finds nothing to do.
    """
    if payload.campaign_id:
        campaigns = [_campaign_or_404(payload.campaign_id, seeker.id)]
    else:
        campaigns = [
            c
            for c in (
                campaign_repo.get_campaign_any(cid)
                for cid in repo.campaign_ids_with_unscored(seeker.id)
            )
            if c is not None
        ]

    scored = 0
    per_campaign: list[dict] = []
    for campaign in campaigns:
        before = repo.count_unscored(seeker.id, campaign["id"])
        if not before:
            continue
        n = scoring.score_unscored(
            seeker.id, campaign["id"], limit=payload.limit, page=payload.page
        )
        scored += n
        per_campaign.append(
            {
                "campaign_id": campaign["id"],
                "name": campaign.get("name"),
                "unscored": before,
                "scored": n,
            }
        )

    return {
        "scored": scored,
        "campaigns": per_campaign,
        "remaining_unscored": repo.count_unscored(seeker.id),
    }


# ---------------------------------------------------------------------------
# One opportunity
# ---------------------------------------------------------------------------


@router.get("/{opportunity_id}")
def get_opportunity(seeker: Seeker, opportunity_id: str) -> dict:
    """One opportunity with its sub-scores, fit meter, contacts and links (FR-283)."""
    opportunity = _opportunity_or_404(opportunity_id, seeker.id)
    presented = _present(opportunity, seeker.locale)
    presented["contacts"] = repo.contacts_for_company(opportunity.get("company_id")) \
        if opportunity.get("company_id") else []
    presented["introduction_paths"] = repo.introduction_paths(seeker.id, opportunity_id)
    presented["advisory"] = scoring.ADVISORY_NOTE
    return presented


@router.get("/{opportunity_id}/vacancy")
def source_vacancy(seeker: Seeker, opportunity_id: str) -> dict:
    """FR-283: the shared vacancy row this opportunity was synthesised from.

    Reached through the opportunity rather than by vacancy id, so the shared
    knowledge base is only ever read outwards from a row the caller owns.
    """
    opportunity = _opportunity_or_404(opportunity_id, seeker.id)
    vacancy_id = opportunity.get("vacancy_id")
    if not vacancy_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "this is a speculative opening; it has no source vacancy (FR-263)",
        )
    vacancy = repo.get_vacancy(vacancy_id)
    if vacancy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source vacancy no longer in the base")
    return vacancy


@router.get("/{opportunity_id}/dream-fit")
def dream_fit(seeker: Seeker, opportunity_id: str) -> dict:
    """FR-383: the fit meter, distinct from the overall score."""
    opportunity = _opportunity_or_404(opportunity_id, seeker.id)
    detail = opportunity.get("dream_fit_detail")
    if not detail:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "this opportunity has not been scored yet"
        )
    return {
        "opportunity_id": opportunity_id,
        "overall_score": opportunity.get("score"),
        "dream_fit_score": opportunity.get("score_dream_fit"),
        **detail,
    }


@router.get("/{opportunity_id}/compensation")
def get_compensation(seeker: Seeker, opportunity_id: str) -> dict:
    """FR-264: the stored range, its confidence and the source list."""
    opportunity = _opportunity_or_404(opportunity_id, seeker.id)
    return {
        "opportunity_id": opportunity_id,
        "comp_min": opportunity.get("comp_min"),
        "comp_max": opportunity.get("comp_max"),
        "currency": opportunity.get("comp_currency"),
        "confidence": opportunity.get("comp_confidence"),
        "is_stated": bool(opportunity.get("comp_is_stated")),
        "sources": opportunity.get("comp_sources"),
        "employer_rating": opportunity.get("employer_rating"),
        "employer_review_themes": opportunity.get("employer_review_themes"),
        "note": "Employer-review signals are advisory input only (FR-265).",
    }


@router.post("/{opportunity_id}/compensation")
def refresh_compensation(seeker: Seeker, opportunity_id: str) -> dict:
    opportunity = _opportunity_or_404(opportunity_id, seeker.id)
    result = comp_mod.enrich_opportunity(opportunity)
    return {"opportunity_id": opportunity_id, **result.as_columns()}


@router.post("/{opportunity_id}/rescore")
def rescore_one(seeker: Seeker, opportunity_id: str, use_llm: bool = True) -> dict:
    """Recompute one opportunity, leaving its manual position alone (FR-284)."""
    opportunity = _opportunity_or_404(opportunity_id, seeker.id)
    # FR-264 first, for the same reason as the campaign-wide recalculation: the
    # compensation sub-score reads the stored estimate, it does not build one.
    comp_mod.enrich_opportunity(opportunity)
    opportunity = _opportunity_or_404(opportunity_id, seeker.id)
    campaign = owned_or_404("campaign", opportunity["campaign_id"], seeker.id)
    ctx = scoring.build_context(campaign)
    llm = None
    if use_llm:
        from dreamjob.llm.client import LLMClient  # noqa: PLC0415

        client = LLMClient(campaign_id=campaign["id"], job_seeker_id=seeker.id)
        if client.settings.deepseek_api_key or client.settings.local_llm_base_url:
            llm = client
    columns = scoring.score_opportunity(opportunity, ctx, llm=llm)
    repo.save_scores(opportunity_id, columns, job_seeker_id=seeker.id)
    return _present(_opportunity_or_404(opportunity_id, seeker.id), seeker.locale)


@router.patch("/{opportunity_id}")
def update_controls(seeker: Seeker, opportunity_id: str, payload: UserControls) -> dict:
    """FR-284: check/uncheck, pin, tag, mark not interested with a reason."""
    _opportunity_or_404(opportunity_id, seeker.id)
    values = payload.model_dump(exclude_unset=True)
    if "user_status" in values and values["user_status"] not in USER_STATUSES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"user_status must be one of {list(USER_STATUSES)}"
        )
    if values.get("user_status") == "not_interested" and not values.get(
        "not_interested_reason"
    ):
        # FR-285 learns from the reason, so an unexplained rejection is refused.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "not_interested requires not_interested_reason (FR-284, FR-285)",
        )
    if values.get("tags") is not None:
        values["tags"] = [str(t).strip()[:40] for t in values["tags"] if str(t).strip()][:12]
    updated = repo.set_user_controls(opportunity_id, seeker.id, values)
    return _present(updated or {}, seeker.locale)


@router.post("/{opportunity_id}/tags")
def add_tags(
    seeker: Seeker, opportunity_id: str, tags: Annotated[list[str], Body(embed=True)]
) -> dict:
    """FR-382: tag an opportunity as a destination or a stepping stone."""
    opportunity = _opportunity_or_404(opportunity_id, seeker.id)
    merged = list(dict.fromkeys([*(opportunity.get("tags") or []), *tags]))
    updated = repo.set_user_controls(
        opportunity_id, seeker.id, {"tags": [str(t).strip()[:40] for t in merged][:12]}
    )
    return _present(updated or {}, seeker.locale)


@router.post("/{opportunity_id}/check-material")
def check_material(
    seeker: Seeker, opportunity_id: str, text: Annotated[str, Body(embed=True)]
) -> dict:
    """FR-263: does this generated text claim a vacancy that does not exist?

    Exposed so a reviewer can check a draft the generator produced elsewhere;
    the generators themselves call
    :func:`dreamjob.pipeline.speculative.check_generated_material` before a draft
    is ever shown.
    """
    opportunity = _opportunity_or_404(opportunity_id, seeker.id)
    claims = speculative.check_generated_material(
        text, opportunity.get("kind", ""), strict=False
    )
    return {
        "opportunity_id": opportunity_id,
        "kind": opportunity.get("kind"),
        "is_speculative": speculative.is_speculative(opportunity),
        "false_vacancy_claims": claims,
        "safe": not claims,
        "disclosure_note": speculative.disclosure_note(
            opportunity.get("kind", ""), opportunity.get("language") or "en"
        ),
    }
