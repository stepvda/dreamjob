"""Contacts API: discovery, validation, objections, introductions.

Covers FR-301 (the ranked hiring contact per opportunity), FR-303 (how an
address was found, and the domain pattern behind an inferred one), FR-304/305
(the validation verdict and its cache), FR-306/RK-08 (the minimal record),
NFR-302 (the permanent block), NFR-303 (campaign scope and the retention
sweep), and FR-302/FR-461 (introduction routes and the message to the
intermediary).

Two routes here write to *shared* tables and therefore take no job seeker id
into the row (FR-344): contact discovery and objections.  They still require
authentication - the knowledge base is not public - and everything that reads
an opportunity, a network member or an introduction path filters on
``seeker.id`` (FR-101, FR-344).
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from dreamjob.api.deps import (
    CurrentSeeker,
    current_admin,
    current_seeker,
    owned_or_404,
)
from dreamjob.db.connection import from_json, to_json, update_row
from dreamjob.db.repositories import apply as apply_repo
from dreamjob.db.repositories import contacts as repo
from dreamjob.jobs.runner import runner
from dreamjob.pipeline import apply_contacts as scale_pipeline
from dreamjob.pipeline import contacts as pipeline
from dreamjob.pipeline import email_patterns as patterns
from dreamjob.pipeline import email_validate as validation
from dreamjob.pipeline import introductions
from dreamjob.security.audit import record_audit

router = APIRouter()

Seeker = Annotated[CurrentSeeker, Depends(current_seeker)]
Admin = Annotated[CurrentSeeker, Depends(current_admin)]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class DiscoverRequest(BaseModel):
    """FR-301.  ``crawl_site`` and ``allow_smtp`` are the two network switches."""

    crawl_site: bool = True
    allow_smtp: bool = True
    use_lookup_service: bool = False
    max_candidates: int = Field(default=8, ge=1, le=25)
    persist: bool = True


class CampaignDiscoverRequest(DiscoverRequest):
    limit: int = Field(default=25, ge=1, le=200)


class CompanyDiscoverRequest(BaseModel):
    """FR-301 for one named company, with the same network switches.

    ``allow_smtp`` defaults off, like the batch pass: one screen button must not
    open thousands of probes to other people's mail servers (FR-305, CR-402).
    """

    crawl_site: bool = True
    allow_smtp: bool = False
    derive_domains: bool = True
    allow_generic: bool = True


class DiscoverAllRequest(BaseModel):
    """FR-301 for a seeker's whole shortlist, as a resumable job (FR-185).

    ``limit`` counts vacancies, not companies, matching
    :func:`apply_contacts.ensure_apply_contacts`: the pass stops as soon as that
    many vacancies have somebody to write to.
    """

    limit: int = Field(default=500, ge=1, le=5000)
    campaign_id: str | None = None
    crawl_site: bool = True
    allow_smtp: bool = False
    derive_domains: bool = True
    allow_generic: bool = True
    refresh: bool = False
    order: str = Field(default="vacancies", pattern="^(vacancies|recency)$")


class ValidateRequest(BaseModel):
    email: str | None = None
    allow_smtp: bool = True
    force: bool = False


class ObjectionRequest(BaseModel):
    """NFR-302.  Either an address or a LinkedIn URL identifies the person."""

    email: str | None = None
    linkedin_url: str | None = None
    reason: str | None = Field(default=None, max_length=500)
    source: str = Field(default="manual", pattern="^(manual|reply|unsubscribe|bounce)$")


class ManualContactRequest(BaseModel):
    """FR-306: exactly the minimal record, nothing more is accepted."""

    company_id: str
    full_name: str | None = Field(default=None, max_length=200)
    role_title: str | None = Field(default=None, max_length=200)
    department: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    linkedin_url: str | None = Field(default=None, max_length=500)
    source: str = Field(default="manual", max_length=200)
    validate_now: bool = True


class NetworkMemberIn(BaseModel):
    full_name: str = Field(max_length=200)
    headline: str | None = Field(default=None, max_length=300)
    role_title: str | None = Field(default=None, max_length=200)
    company_name: str | None = Field(default=None, max_length=200)
    company_id: str | None = None
    linkedin_url: str | None = Field(default=None, max_length=500)
    degree: int = Field(default=1, ge=1, le=3)
    mutual_name: str | None = Field(default=None, max_length=200)
    mutual_linkedin: str | None = Field(default=None, max_length=500)
    schools: list[str] = Field(default_factory=list)
    employers: list[str] = Field(default_factory=list)
    communities: list[str] = Field(default_factory=list)
    connected_at: str | None = None
    last_interaction_at: str | None = None


class NetworkImportRequest(BaseModel):
    members: list[NetworkMemberIn]
    source: str = Field(default="linkedin_export", max_length=60)
    access_method: str = Field(default="manual", pattern="^(manual|http|browser)$")
    campaign_id: str | None = None


class IntroductionRequest(BaseModel):
    limit: int = Field(default=10, ge=1, le=50)
    write_messages: bool = True
    use_llm: bool = True
    language: str | None = Field(default=None, max_length=8)
    replace: bool = True


class PathStatusRequest(BaseModel):
    status: str = Field(pattern="^(proposed|accepted|requested|declined|sent|rejected)$")


class MessageRequest(BaseModel):
    use_llm: bool = True
    language: str | None = Field(default=None, max_length=8)
    why_company: str = Field(default="", max_length=1000)
    seeker_goal: str = Field(default="", max_length=500)


class LookupServiceSettings(BaseModel):
    """OQ-05: unresolved, so the switch is explicit and administrator-only."""

    enabled: bool = False
    url: str = Field(default="", max_length=500)
    api_key: str = Field(default="", max_length=200)
    name: str = Field(default="", max_length=120)


# ---------------------------------------------------------------------------
# FR-301: the ranked hiring contact for one opportunity
# ---------------------------------------------------------------------------


@router.post("/opportunities/{opportunity_id}/discover")
async def discover_contacts(
    opportunity_id: str, seeker: Seeker, request: DiscoverRequest | None = None
) -> dict[str, Any]:
    """Find and rank the hiring contacts for one opportunity (FR-301, FR-303, FR-304)."""
    body = request or DiscoverRequest()
    try:
        report = await pipeline.discover_for_opportunity(
            seeker.id,
            opportunity_id,
            crawl_site=body.crawl_site,
            allow_smtp=body.allow_smtp,
            use_lookup_service=body.use_lookup_service,
            max_candidates=body.max_candidates,
            persist=body.persist,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return report.as_dict()


@router.post("/campaigns/{campaign_id}/discover")
async def discover_for_campaign(
    campaign_id: str, seeker: Seeker, request: CampaignDiscoverRequest | None = None
) -> dict[str, Any]:
    """FR-301 for every selected opportunity in a campaign."""
    body = request or CampaignDiscoverRequest()
    reports = await pipeline.discover_for_campaign(
        seeker.id,
        campaign_id,
        limit=body.limit,
        crawl_site=body.crawl_site,
        allow_smtp=body.allow_smtp,
        use_lookup_service=body.use_lookup_service,
        max_candidates=body.max_candidates,
        persist=body.persist,
    )
    return {
        "campaign_id": campaign_id,
        "opportunities": len(reports),
        "with_contact": sum(1 for r in reports if r.candidates),
        "reports": [r.as_dict() for r in reports],
    }


# ---------------------------------------------------------------------------
# FR-301 from the Contacts screen: scrape a company, or the whole shortlist
# ---------------------------------------------------------------------------


@router.post("/companies/{company_id}/discover")
async def discover_company_contacts(
    company_id: str, seeker: Seeker, request: CompanyDiscoverRequest | None = None
) -> dict[str, Any]:
    """Walk the FR-301 ladder for one company and return what it found.

    This is the Contacts screen's "Find contacts" control.  It reads the company
    from the database rather than accepting its name and domain from the caller,
    so the ladder cannot be pointed at a company it is not looking at, and it
    honours the same FR-303/FR-304/NFR-302 rules as the batch pass because it
    calls the same code.
    """
    body = request or CompanyDiscoverRequest()
    try:
        outcome = await scale_pipeline.resolve_company_by_id(
            company_id,
            crawl_site=body.crawl_site,
            allow_smtp=body.allow_smtp,
            derive_domains=body.derive_domains,
            allow_generic=body.allow_generic,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {
        "company_id": company_id,
        "outcome": outcome.as_dict(),
        "contacts": repo.contacts_for_company(
            company_id, include_blocked=bool(seeker.is_admin)
        ),
    }


@router.post("/discover", status_code=status.HTTP_202_ACCEPTED)
async def discover_contacts_for_seeker(body: DiscoverAllRequest, seeker: Seeker) -> dict[str, Any]:
    """Start the FR-301 pass for a seeker's shortlist as a resumable job (FR-185).

    The Contacts screen used to list whatever contacts happened to exist and
    offered no way to find more; the Apply Browser's control only covered its
    current page.  This is the missing trigger, and it is a job rather than a
    request because a bounded pass over many companies is not something a screen
    should wait for (NFR-502).  The options travel in the checkpoint, so a run
    interrupted by a restart resumes with the same limits (NFR-401).
    """
    options = body.model_dump()
    options.pop("campaign_id", None)
    job_id = runner.create(
        scale_pipeline.DISCOVERY_JOB_KIND,
        job_seeker_id=seeker.id,
        campaign_id=body.campaign_id,
        total=1,
        # A ladder step budgets about eight seconds per company (FR-182 pacing).
        estimated_seconds=max(60, min(body.limit, 1000) * 8),
    )
    update_row("job_run", job_id, {"checkpoint": to_json({"options": options})})
    await runner.start(job_id)
    return {
        "job_id": job_id,
        "kind": scale_pipeline.DISCOVERY_JOB_KIND,
        "campaign_id": body.campaign_id,
        "limit": body.limit,
    }


@router.get("/discover/{job_id}")
def discovery_status(job_id: str, seeker: Seeker) -> dict[str, Any]:
    """Progress of a contacts pass started above, owned by this seeker (FR-344).

    The checkpoint is decoded here rather than in the screen: the pass stores
    its coverage report there, and a JSON string is not something the interface
    should have to parse.
    """
    row = owned_or_404("job_run", job_id, seeker.id)
    return {**row, "checkpoint": from_json(row.get("checkpoint"), {}) or {}}


@router.get("/coverage")
def contacts_coverage(seeker: Seeker) -> dict[str, Any]:
    """How much of the corpus has somebody to write to, and how it was found.

    Reads the same repository summary the FR-301 report uses, so the screen can
    say what is reachable, what was found and by which FR-303 method, rather
    than showing an unexplained count.
    """
    return {
        "resolutions": apply_repo.resolution_summary(),
        "coverage": apply_repo.coverage_by_method(),
    }


@router.get("/opportunities/{opportunity_id}")
def contacts_for_opportunity(opportunity_id: str, seeker: Seeker) -> dict[str, Any]:
    """The stored, ranked candidates without touching the network (FR-301)."""
    context = repo.opportunity_context(opportunity_id, seeker.id)
    if context is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "opportunity not found")
    company_id = context.get("company_id")
    if not company_id:
        return {"opportunity_id": opportunity_id, "candidates": [], "best": None}

    wanted = pipeline.function_tokens(context.get("function_family"), context.get("title"))
    candidates = pipeline.hiring_managers_from_structure(
        context.get("company_structure") or {}, context.get("company_key_people") or [], wanted
    )
    candidates.extend(
        pipeline.candidates_from_stored(company_id, context.get("campaign_id"))
    )
    ranked = pipeline.rank([c for c in candidates if not c.blocked])
    return {
        "opportunity_id": opportunity_id,
        "company_id": company_id,
        "company_name": context.get("company_name"),
        "best": ranked[0].as_public() if ranked else None,
        "candidates": [c.as_public() for c in ranked],
    }


@router.get("/companies/{company_id}")
def contacts_for_company(
    company_id: str,
    seeker: Seeker,
    campaign_id: str | None = None,
    include_blocked: bool = Query(default=False),
) -> list[dict]:
    """Every stored contact of a company (FR-301, NFR-302, NFR-303).

    Blocked people are only listed to an administrator: the list names the
    individuals who invoked their NFR-302 opt-out, and the usable path is what
    a job seeker is shown.
    """
    if include_blocked:
        if not seeker.is_admin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrator role required")
        return repo.contacts_for_company(company_id, include_blocked=True)
    return repo.usable_contacts_for_company(
        company_id, campaign_id=campaign_id, require_email=False
    )


@router.post("/companies/{company_id}/manual", status_code=status.HTTP_201_CREATED)
def add_contact(company_id: str, body: ManualContactRequest, seeker: Seeker) -> dict[str, Any]:
    """Record a contact the job seeker found themselves (FR-301, FR-306)."""
    if body.company_id != company_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "company_id mismatch")
    if not body.email and not body.linkedin_url:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "an e-mail address or a LinkedIn URL is required"
        )
    if pipeline.is_blocked(body.email, body.linkedin_url):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "this contact has objected and is permanently blocked"
        )

    values = repo.minimise(
        {
            "company_id": company_id,
            "full_name": body.full_name,
            "role_title": body.role_title,
            "department": body.department,
            "email": body.email,
            "email_source_method": patterns.METHOD_MANUAL if body.email else None,
            "is_generic_mailbox": 1
            if body.email and validation.is_role_address(body.email)
            else 0,
            "linkedin_url": body.linkedin_url,
            "source": body.source,
            "access_method": "manual",
            "shareable": 1,
            "confidence": 0.9,
        }
    )
    contact_id, created = repo.upsert_contact(values)
    if body.email and body.validate_now:
        validation.validate(body.email, allow_smtp=False, contact_id=contact_id)
    record_audit(
        "contacts.manual_added",
        entity_type="contact",
        entity_id=contact_id,
        seeker_id=seeker.id,
        detail={"created": created},
    )
    return repo.get_contact(contact_id) or {}


# ---------------------------------------------------------------------------
# FR-303: address discovery and the domain pattern
# ---------------------------------------------------------------------------


@router.get("/patterns/{domain}")
def email_pattern(domain: str, seeker: Seeker) -> dict[str, Any]:
    """The inferred local-part convention for a domain (FR-303)."""
    stored = repo.get_pattern(domain)
    if stored:
        return stored
    inference = patterns.learn_domain_pattern(domain)
    return {
        "domain": inference.domain,
        "pattern": inference.pattern,
        "confidence": inference.confidence,
        "sample_count": inference.sample_count,
        "supporting": inference.supporting,
        "alternatives": inference.alternatives,
    }


@router.get("/patterns/{domain}/candidates")
def pattern_candidates(domain: str, seeker: Seeker, name: str = Query(min_length=2)) -> list[dict]:
    """The addresses the domain's convention would give one person (FR-303)."""
    return [
        {
            "email": found.email,
            "pattern": found.pattern,
            "email_source_method": found.method,
            "confidence": found.confidence,
        }
        for found in patterns.candidates_for_person(name, domain)
    ]


@router.get("/lookup-service")
def lookup_service_status(admin: Admin) -> dict[str, Any]:
    """FR-303 third-party lookup: off until OQ-05 is answered."""
    config = patterns.lookup_service_config()
    return {
        "enabled": config["enabled"],
        "configured": bool(config["url"]),
        "name": config["name"],
        "url": config["url"],
        "open_question": (
            "OQ-05: which third-party e-mail-verification and contact-lookup services "
            "are acceptable in terms of cost and data protection is unresolved, so this "
            "stays off by default."
        ),
    }


@router.put("/lookup-service")
def set_lookup_service(body: LookupServiceSettings, admin: Admin) -> dict[str, Any]:
    """Name a provider and switch it on (FR-303, OQ-05).  Administrator only."""
    from dreamjob.db.repositories import knowledge as kb  # noqa: PLC0415

    if body.enabled and not body.url:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "a provider URL is required before enabling the lookup"
        )
    kb.set_setting(patterns.SETTING_LOOKUP_ENABLED, bool(body.enabled))
    kb.set_setting(patterns.SETTING_LOOKUP_URL, body.url)
    kb.set_setting(patterns.SETTING_LOOKUP_NAME, body.name)
    if body.api_key:
        kb.set_setting(patterns.SETTING_LOOKUP_KEY, body.api_key)
    record_audit(
        "contacts.lookup_service_configured",
        seeker_id=admin.id,
        detail={"enabled": body.enabled, "name": body.name, "url": body.url},
    )
    return lookup_service_status(admin)


# ---------------------------------------------------------------------------
# FR-304 / FR-305: validation
# ---------------------------------------------------------------------------


@router.post("/{contact_id}/validate")
async def validate_contact(
    contact_id: str, seeker: Seeker, request: ValidateRequest | None = None
) -> dict[str, Any]:
    """Re-run the FR-304 checks for one stored contact."""
    body = request or ValidateRequest()
    contact = repo.get_contact(contact_id)
    if contact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "contact not found")
    if not contact.get("email"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "this contact has no e-mail address")

    result = await asyncio.to_thread(
        validation.validate,
        contact["email"],
        allow_smtp=body.allow_smtp,
        force=body.force,
        contact_id=contact_id,
    )
    return {
        "contact_id": contact_id,
        "email": result.email,
        "result": result.result,
        "usable": result.usable,
        "detail": result.detail,
        "checked_at": result.checked_at,
        "from_cache": result.from_cache,
    }


@router.post("/validate")
async def validate_address(body: ValidateRequest, seeker: Seeker) -> dict[str, Any]:
    """Validate a loose address before it is used anywhere (FR-304)."""
    if not body.email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "email is required")
    result = await asyncio.to_thread(
        validation.validate, body.email, allow_smtp=body.allow_smtp, force=body.force
    )
    return {
        "email": result.email,
        "result": result.result,
        "usable": result.usable,
        "detail": result.detail,
        "checked_at": result.checked_at,
        "from_cache": result.from_cache,
    }


# ---------------------------------------------------------------------------
# NFR-302: objections
# ---------------------------------------------------------------------------


@router.post("/objections", status_code=status.HTTP_201_CREATED)
def add_objection(body: ObjectionRequest, seeker: Seeker) -> dict[str, Any]:
    """Block a contact permanently, for every job seeker (NFR-302)."""
    if not body.email and not body.linkedin_url:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "an e-mail address or a LinkedIn URL is required"
        )
    return pipeline.record_objection(
        body.email,
        linkedin_url=body.linkedin_url,
        reason=body.reason,
        source=body.source,
        job_seeker_id=seeker.id,
    )


@router.get("/objections")
def list_objections(seeker: Seeker, limit: int = Query(default=200, ge=1, le=1000)) -> list[dict]:
    """The objection list, to an administrator.

    It is a cross-job-seeker list of addresses, profiles and reasons - the
    shared block list only needs :func:`check_objection`'s yes/no, not the
    identities in it.
    """
    if not seeker.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrator role required")
    return repo.list_objections(limit)


@router.get("/objections/check")
def check_objection(
    seeker: Seeker, email: str | None = None, linkedin_url: str | None = None
) -> dict[str, bool]:
    return {"blocked": pipeline.is_blocked(email, linkedin_url)}


# ---------------------------------------------------------------------------
# NFR-303: retention
# ---------------------------------------------------------------------------


@router.get("/retention/due")
def retention_due(admin: Admin) -> dict[str, Any]:
    """What the next sweep would delete (NFR-303)."""
    due = repo.due_for_retention()
    return {
        "grace_days": pipeline.retention_grace_days(),
        "contacts": len(due["contacts"]),
        "network_members": len(due["network_members"]),
    }


@router.post("/retention/sweep")
def run_retention_sweep(admin: Admin) -> dict[str, Any]:
    """Delete the records whose retention deadline has passed (NFR-303).

    The same function the monitoring scheduler runs; this route is the manual
    trigger for it.
    """
    return pipeline.sweep_retention()


# ---------------------------------------------------------------------------
# FR-302 / FR-461: network and introduction routes
# ---------------------------------------------------------------------------


@router.post("/network/import")
def import_network(body: NetworkImportRequest, seeker: Seeker) -> dict[str, int]:
    """Import the job seeker's LinkedIn network (FR-302, NFR-303)."""
    return introductions.import_members(
        seeker.id,
        [member.model_dump() for member in body.members],
        source=body.source,
        access_method=body.access_method,
        campaign_id=body.campaign_id,
    )


@router.get("/network")
def list_network(seeker: Seeker, limit: int = Query(default=500, ge=1, le=2000)) -> list[dict]:
    return repo.list_network(seeker.id, limit)


@router.delete("/network/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_network_member(member_id: str, seeker: Seeker) -> None:
    if not repo.delete_network_member(member_id, seeker.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "network member not found")


@router.get("/opportunities/{opportunity_id}/introductions")
def list_introductions(opportunity_id: str, seeker: Seeker) -> list[dict]:
    return repo.introduction_paths(seeker.id, opportunity_id=opportunity_id)


@router.post("/opportunities/{opportunity_id}/introductions")
async def build_introductions(
    opportunity_id: str, seeker: Seeker, request: IntroductionRequest | None = None
) -> list[dict]:
    """Rank the routes and draft the message to each intermediary (FR-302, FR-461)."""
    body = request or IntroductionRequest()
    try:
        routes = await asyncio.to_thread(
            introductions.propose_paths,
            seeker.id,
            opportunity_id,
            limit=body.limit,
            write_messages=body.write_messages,
            use_llm=body.use_llm,
            language=body.language,
            replace=body.replace,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return [route.as_dict() for route in routes]


@router.get("/opportunities/{opportunity_id}/outreach")
def outreach_options(opportunity_id: str, seeker: Seeker) -> dict[str, Any]:
    """FR-302: the cold e-mail and the introduction, side by side."""
    try:
        return introductions.outreach_options(seeker.id, opportunity_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/introductions/{path_id}/message")
async def regenerate_message(
    path_id: str, seeker: Seeker, request: MessageRequest | None = None
) -> dict[str, Any]:
    """Rewrite the message to the intermediary (FR-461)."""
    body = request or MessageRequest()
    path = repo.get_introduction_path(path_id, seeker.id)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "introduction path not found")
    context = repo.opportunity_context(path.get("opportunity_id") or "", seeker.id) or {}

    route = introductions.IntroductionRoute(
        member_id=path.get("network_member_id"),
        name=path.get("intermediary_name") or "",
        role=path.get("intermediary_role"),
        company_name=context.get("company_name"),
        linkedin_url=path.get("intermediary_linkedin"),
        relationship=path.get("relationship") or introductions.FIRST_DEGREE,
        degree=int(path.get("degree") or 1),
        strength=float(path.get("strength") or 0.5),
        relevance=float(path.get("relevance") or 0.5),
        rationale=path.get("rationale") or "",
        target_contact_id=path.get("target_contact_id"),
        path_id=path_id,
    )
    target = repo.get_contact(route.target_contact_id) if route.target_contact_id else None
    if target:
        route.target_name = target.get("full_name")
        route.target_role = target.get("role_title")

    language = body.language or context.get("language") or "en"
    if body.use_llm:
        subject, message = await asyncio.to_thread(
            introductions.generate_message,
            seeker.id,
            route,
            company_name=context.get("company_name"),
            opportunity_title=context.get("title"),
            why_company=body.why_company,
            seeker_goal=body.seeker_goal,
            language=language,
            campaign_id=context.get("campaign_id"),
        )
    else:
        subject, message = introductions.fallback_message(
            route,
            seeker_name=repo.display_name(seeker.id),
            company_name=context.get("company_name"),
            opportunity_title=context.get("title"),
            language=language,
        )
    repo.save_path_message(path_id, subject, message)
    return {"path_id": path_id, "message_subject": subject, "message_draft": message}


@router.patch("/introductions/{path_id}")
def set_path_status(path_id: str, body: PathStatusRequest, seeker: Seeker) -> dict:
    updated = repo.set_path_status(path_id, seeker.id, body.status)
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "introduction path not found")
    return updated
