"""Company API: profile, refresh, competitors, signals, knowledge base.

Covers FR-221..226 (the standardised profile and its incremental refresh),
FR-224 (competitor suggestions and adopting them into the target list),
FR-225/FR-402 (hiring signals and the application window), FR-384 (values and
working style), NFR-402 (per-field confidence and provenance) and the
campaign-independent browse/search side of FR-341/FR-345.

Everything here reads or writes the *shared* knowledge base, which is why the
routes take no campaign: FR-341 makes company profiles common property of all
job seekers, and FR-344 forbids a shared row from carrying a link back to the
job seeker whose campaign produced it.  Authentication is still required - the
knowledge base is not public - and the two routes that touch private data
(adopting a peer onto the target list, and refreshing on behalf of a campaign)
pass the job seeker id explicitly.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from dreamjob.adapters.website import crawler
from dreamjob.api.deps import CurrentSeeker, current_admin, current_seeker, owned_or_404
from dreamjob.db.repositories import companies as repo
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.pipeline import company_profile, competitors, knowledge_base, signals
from dreamjob.security.audit import record_audit

router = APIRouter()

Seeker = Annotated[CurrentSeeker, Depends(current_seeker)]
Admin = Annotated[CurrentSeeker, Depends(current_admin)]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RefreshRequest(BaseModel):
    """FR-226: a refresh is normally incremental; ``force`` overrides the age check."""

    force: bool = False
    max_pages: int = Field(default=crawler.DEFAULT_MAX_PAGES, ge=1, le=120)
    max_depth: int = Field(default=crawler.DEFAULT_MAX_DEPTH, ge=1, le=5)
    use_llm: bool = True
    campaign_id: str | None = None
    include_signals: bool = True
    include_competitors: bool = True


class CompetitorRequest(BaseModel):
    limit: int = Field(default=competitors.DEFAULT_LIMIT, ge=1, le=50)
    min_suggestions: int = Field(default=competitors.MIN_SUGGESTIONS, ge=0, le=20)
    use_llm: bool = True


class SignalRefreshRequest(BaseModel):
    fetch_news: bool = True


class SuppressRequest(BaseModel):
    """Why a shared company row is being hidden - kept with the row and audited."""

    reason: str = Field(min_length=1, max_length=1000)


def _company_or_404(company_id: str, seeker: CurrentSeeker | None = None) -> dict:
    company = repo.get_company(company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "company not found")
    # A suppressed company is hidden from everyone but an administrator, who
    # still has to be able to open it in order to undo the decision.
    if company.get("suppressed") and not (seeker and seeker.is_admin):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "company not found")
    return company


# ---------------------------------------------------------------------------
# Browse and search, independent of any campaign (FR-345, FR-341)
# ---------------------------------------------------------------------------


@router.get("")
def search_companies(
    seeker: Seeker,
    q: str | None = None,
    country: str | None = None,
    sector: str | None = None,
    size_band: str | None = None,
    ats_vendor: str | None = Query(
        None,
        description=(
            "A vendor name, or 'any'/'none' for companies reached through an ATS "
            "board at all. The ATS route is the one that reaches a company's own "
            "postings, so being able to ask is how a starved harvest stage shows."
        ),
    ),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    """Full-text and faceted search over the shared company knowledge base."""
    return knowledge_base.browse_companies(
        q, country=country, sector=sector, size_band=size_band, ats_vendor=ats_vendor,
        limit=limit, offset=offset,
    )


@router.get("/vacancies")
def search_vacancies(
    seeker: Seeker,
    q: str | None = None,
    country: str | None = None,
    company_id: str | None = None,
    fresh_only: bool = False,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    """The vacancy half of the knowledge-base browser (FR-345)."""
    return knowledge_base.browse_vacancies(
        q,
        country=country,
        company_id=company_id,
        fresh_only=fresh_only,
        limit=limit,
        offset=offset,
    )


@router.get("/vacancies/{vacancy_id}")
def vacancy_detail(vacancy_id: str, seeker: Seeker) -> dict:
    detail = knowledge_base.vacancy_detail(vacancy_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "vacancy not found")
    return detail


@router.get("/stale")
def stale_profiles(
    seeker: Seeker,
    limit: int = Query(50, ge=1, le=200),
    max_age_days: int | None = Query(None, ge=1, le=3650),
) -> dict:
    """Profiles past the staleness policy, i.e. the re-crawl queue (FR-226, FR-343)."""
    rows = company_profile.due_for_refresh(limit=limit, max_age_days=max_age_days)
    return {
        "count": len(rows),
        "items": [
            {
                "id": row["id"],
                "name": row.get("name"),
                "domain": row.get("domain"),
                "age_days": round(company_profile.profile_age_days(row) or 0.0, 1),
                "refreshed_at": row.get("refreshed_at") or row.get("collected_at"),
            }
            for row in rows
        ],
    }


@router.get("/refresh-jobs/{job_id}")
def refresh_job_status(job_id: str, seeker: Seeker) -> dict:
    """Progress of a background refresh started by this router (FR-185).

    The company being profiled is shared, but the job row is not: it carries the
    seeker who started it, its errors and its checkpoint, so it is read through
    the ownership guard rather than by id alone (FR-101, FR-344).
    """
    return owned_or_404("job_run", job_id, seeker.id)


# ---------------------------------------------------------------------------
# The standardised profile (FR-222, FR-223, FR-384, NFR-402)
# ---------------------------------------------------------------------------


@router.get("/enrichment-coverage")
def enrichment_coverage(
    limit: int = Query(200, ge=1, le=1000),
    seeker: CurrentSeeker = Depends(current_seeker),
) -> dict:
    """What the knowledge base knows about each employer, and how fresh it is (FR-341).

    The passes that build a company - website crawl, filings, signals,
    competitors, employer kind - each write somewhere different, so "is this
    employer enriched?" had no single answer. This is that answer: aggregate
    counts for the operator, and per-company status for the list.
    """
    return repo.enrichment_coverage(limit=limit)


class EnrichRequest(BaseModel):
    """Run company enrichment now: for companies named, or the busiest ones."""

    company_ids: list[str] | None = None
    limit: int = Field(25, ge=1, le=200)


@router.post("/enrichment/run")
async def run_enrichment(
    payload: EnrichRequest | None = None,
    seeker: CurrentSeeker = Depends(current_seeker),
) -> dict:
    """Backfill company enrichment by hand (FR-221..246).

    The collection tail enriches each campaign's shortlist automatically and the
    scheduler sweeps the busiest employers every six hours; this is the button
    for when an operator wants it now.
    """
    from dreamjob.pipeline import company_enrichment  # noqa: PLC0415

    body = payload or EnrichRequest()
    ids = [str(c) for c in (body.company_ids or []) if c]
    if not ids:
        ids = repo.busiest_companies(body.limit)
    if not ids:
        return {"skipped": "no companies to enrich"}
    report = await company_enrichment.enrich_companies(
        ids, job_seeker_id=seeker.id, limit=body.limit
    )
    return report.as_dict()


# ---------------------------------------------------------------------------
# Suppression: hiding a shared company instead of deleting it (FR-341, NFR-302)
# ---------------------------------------------------------------------------


@router.post("/{company_id}/suppress")
def suppress_company(company_id: str, admin: Admin, payload: SuppressRequest) -> dict:
    """Hide a shared company from browse, counts and non-admin detail (FR-341).

    A knowledge-base row is shared property, so it is never hard-deleted; the
    suppression is the reversible, administrated way to take it out of
    circulation, and the admin detail view can still open it to undo this.
    """
    company = repo.suppress_company(company_id, payload.reason)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "company not found")
    record_audit(
        "company.suppressed",
        entity_type="company",
        entity_id=company_id,
        seeker_id=admin.id,
        detail={"reason": payload.reason},
    )
    return {
        "company_id": company_id,
        "suppressed": True,
        "suppressed_at": company.get("suppressed_at"),
        "suppressed_reason": company.get("suppressed_reason"),
    }


@router.delete("/{company_id}/suppress")
def unsuppress_company(company_id: str, admin: Admin) -> dict:
    """Restore a suppressed company to every browse, list and count."""
    company = repo.unsuppress_company(company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "company not found")
    record_audit(
        "company.unsuppressed",
        entity_type="company",
        entity_id=company_id,
        seeker_id=admin.id,
    )
    return {"company_id": company_id, "suppressed": False}


@router.get("/{company_id}")
def company_profile_view(company_id: str, seeker: Seeker) -> dict:
    """The fixed-schema company profile, in the layout FR-222 prescribes."""
    # A suppressed company 404s here for every non-admin; an admin keeps the
    # door open because the un-suppress control lives on this view.
    _company_or_404(company_id, seeker)
    profile = company_profile.standardised_profile(company_id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "company not found")
    return profile


@router.get("/{company_id}/pages")
def crawled_pages(
    company_id: str, seeker: Seeker, limit: int = Query(100, ge=1, le=400)
) -> dict:
    """The page inventory of the last crawl, with what each page was read as (FR-221)."""
    _company_or_404(company_id, seeker)
    pages = repo.crawled_pages(company_id, limit=limit)
    return {
        "company_id": company_id,
        "last_crawl_at": repo.last_crawl_at(company_id),
        "count": len(pages),
        "pages": [
            {
                "url": page.get("url"),
                "kind": (page.get("field_path") or "").removeprefix(
                    repo.PAGE_PROVENANCE_PREFIX
                ),
                "http_status": page.get("http_status"),
                "byte_size": page.get("byte_size"),
                "fetched_at": page.get("fetched_at") or page.get("created_at"),
                "raw_document_id": page.get("raw_document_id"),
            }
            for page in pages
        ],
    }


@router.get("/{company_id}/provenance")
def field_provenance(company_id: str, seeker: Seeker) -> dict:
    """Per-field confidence and source, for the flags NFR-402 requires."""
    _company_or_404(company_id, seeker)
    rows = repo.field_provenance(company_id)
    return {
        "company_id": company_id,
        "fields": rows,
        "low_confidence_fields": sorted(
            row["field_path"]
            for row in rows
            if float(row.get("confidence") or 0) < company_profile.LOW_CONFIDENCE
        ),
        "threshold": company_profile.LOW_CONFIDENCE,
    }


@router.post("/{company_id}/refresh")
async def refresh_profile(
    company_id: str, seeker: Seeker, body: RefreshRequest | None = None
) -> dict:
    """Re-crawl and rebuild the profile in the background (FR-221, FR-226).

    Returns immediately with a ``job_run`` id: a 30-page crawl at the configured
    per-domain rate is a minute of work, which is not a request.
    """
    company = _company_or_404(company_id, seeker)
    options = body or RefreshRequest()

    if not options.force and not company_profile.needs_refresh(company):
        return {
            "company_id": company_id,
            "started": False,
            "reused": True,
            "reason": "profile is within the configured staleness age (FR-226)",
            "refreshed_at": company.get("refreshed_at") or company.get("collected_at"),
        }

    job_id = runner.create(
        "profiling",
        campaign_id=options.campaign_id,
        job_seeker_id=seeker.id,
        adapter_key=repo.WEBSITE_ADAPTER_KEY,
        total=options.max_pages,
        estimated_seconds=options.max_pages * 3,
    )

    async def worker(ctx: JobContext) -> None:
        outcome = await company_profile.build_profile(
            company,
            force=options.force,
            max_pages=options.max_pages,
            max_depth=options.max_depth,
            use_llm=options.use_llm,
            campaign_id=options.campaign_id,
            job_seeker_id=seeker.id,
        )
        ctx.progress(outcome.pages_crawled, options.max_pages)
        ctx.save_checkpoint(stage="profile", **outcome.as_dict())
        await ctx.checkpoint_barrier()

        refreshed = repo.get_company(company_id) or company
        if options.include_signals:
            # The pages and the newsroom feed this crawl just found, rather than
            # a second walk of the same site.
            found = await signals.refresh_signals(
                refreshed,
                pages=list(outcome.crawl.pages) if outcome.crawl else None,
                feeds=outcome.feeds or None,
            )
            ctx.save_checkpoint(stage="signals", signals=len(found))
            await ctx.checkpoint_barrier()
        if options.include_competitors:
            suggested = competitors.suggest_competitors(
                refreshed,
                use_llm=options.use_llm,
                campaign_id=options.campaign_id,
                job_seeker_id=seeker.id,
                mentioned=outcome.competitor_mentions,
            )
            ctx.save_checkpoint(stage="competitors", competitors=len(suggested))

    await runner.start(job_id, worker)
    await asyncio.to_thread(
        record_audit,
        "company.refresh_started",
        "company",
        company_id,
        seeker_id=seeker.id,
        detail={"job_id": job_id, "force": options.force, "max_pages": options.max_pages},
    )
    return {"company_id": company_id, "started": True, "reused": False, "job_id": job_id}


# ---------------------------------------------------------------------------
# Competitors and peers (FR-224)
# ---------------------------------------------------------------------------


@router.get("/{company_id}/competitors")
def list_competitors(
    company_id: str, seeker: Seeker, limit: int = Query(12, ge=1, le=50)
) -> dict:
    """Competitor links already on record, aggregated per peer."""
    _company_or_404(company_id, seeker)
    suggestions = competitors.stored_suggestions(company_id, limit=limit)
    return {
        "company_id": company_id,
        "count": len(suggestions),
        "meets_minimum": len(suggestions) >= competitors.MIN_SUGGESTIONS,
        "suggestions": [
            {**s, "on_target_list": _on_target_list(seeker.id, s)} for s in suggestions
        ],
    }


@router.post("/{company_id}/competitors")
def suggest_competitors(
    company_id: str, seeker: Seeker, body: CompetitorRequest | None = None
) -> dict:
    """Run competitor discovery now and store what it finds (FR-224)."""
    company = _company_or_404(company_id, seeker)
    options = body or CompetitorRequest()
    suggestions = competitors.suggest_competitors(
        company,
        limit=options.limit,
        min_suggestions=options.min_suggestions,
        use_llm=options.use_llm,
        job_seeker_id=seeker.id,
    )
    return {
        "company_id": company_id,
        "count": len(suggestions),
        "meets_minimum": len(suggestions) >= competitors.MIN_SUGGESTIONS,
        "suggestions": [
            {**s, "on_target_list": _on_target_list(seeker.id, s)} for s in suggestions
        ],
    }


@router.post("/{company_id}/competitors/{link_id}/adopt", status_code=status.HTTP_201_CREATED)
def adopt_competitor(company_id: str, link_id: str, seeker: Seeker) -> dict:
    """Add a suggested peer to this job seeker's target list (FR-224)."""
    _company_or_404(company_id, seeker)
    link = repo.get_competitor_link(link_id)
    if link is None or link["company_id"] != company_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "competitor link not found")
    try:
        result = competitors.adopt(seeker.id, link_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    record_audit(
        "company.competitor_adopted",
        "company",
        result["company_id"],
        seeker_id=seeker.id,
        detail={"from_company_id": company_id, "link_id": link_id},
    )
    return result


def _on_target_list(seeker_id: str, suggestion: dict[str, Any]) -> bool:
    peer_id = suggestion.get("peer_company_id")
    return bool(peer_id) and repo.is_watchlisted(seeker_id, peer_id)


# ---------------------------------------------------------------------------
# Hiring signals and timing intelligence (FR-225, FR-402)
# ---------------------------------------------------------------------------


@router.get("/{company_id}/signals")
def list_signals(company_id: str, seeker: Seeker) -> dict:
    """Dated, sourced hiring signals plus the recommended application window."""
    _company_or_404(company_id, seeker)
    return {"company_id": company_id, **signals.timing_summary(company_id)}


@router.post("/{company_id}/signals")
async def refresh_signals(
    company_id: str, seeker: Seeker, body: SignalRefreshRequest | None = None
) -> dict:
    """Re-read the newsroom, filings and postings and re-detect signals (FR-225)."""
    company = _company_or_404(company_id, seeker)
    options = body or SignalRefreshRequest()
    await signals.refresh_signals(company, fetch_news=options.fetch_news)
    return {"company_id": company_id, **signals.timing_summary(company_id)}


@router.get("/{company_id}/timing")
def timing(company_id: str, seeker: Seeker) -> dict:
    """The application-window recommendation on its own (FR-402)."""
    _company_or_404(company_id, seeker)
    return signals.recommend_window(company_id).as_dict()
