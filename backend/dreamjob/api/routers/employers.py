"""Who actually employs: the verdict, its evidence, and the seeker's answer to it.

Requirements: FR-341 (the knowledge base is shared and campaign-independent),
FR-344 (a shared row carries no link back to a job seeker), FR-185 (long work
runs as a resumable job), FR-282 (explainable), NFR-205 (website text is
untrusted), NFR-402 (per-field provenance), CR-405 (advisory only).

Five routes, and one idea behind all of them: **a verdict about an employer is
only usable if the person it is shown to can see why, and answer it.**

    GET  /api/employers/{id}/kind      the verdict with its full evidence
    POST /api/employers/{id}/resolve   walk the ladder now
    POST /api/employers/{id}/correct   "this is wrong", with a reason
    GET  /api/employers/coverage       how much of the corpus is resolved
    POST /api/employers/resolve-batch  the background pass

The routes take no campaign.  The tag is a shared knowledge-base fact (FR-341):
a company does not stop being a staffing agency between job seekers, and
resolving it once serves everybody.  The one per-seeker artefact - a
correction - is explicitly private to its author until it is promoted, and the
promoted copy drops the seeker id (FR-344).

Three things this router is careful about:

* **``cannot_tell`` is a state, not a 404.**  A company nobody has researched
  comes back with ``kind: "cannot_tell"``, ``reason: "not_researched"`` and the
  cheapest next rung, not with an empty body a caller has to guess about.  A
  404 here means "no such company".
* **Evidence is untrusted content** (NFR-205).  Quotes come from pages the
  product did not write; they are returned as data, marked as such, and a
  verdict whose ``anomalies`` are non-empty - which is where a planted "ignore
  your instructions" line is recorded - carries ``needs_review: true`` all the
  way out.
* **Coverage says how old it is.**  62% resolved reads as "38% are direct
  employers" unless the answer also says when the corpus was last swept and how
  far the sweep got; ``employer_resolve_run`` (migration 113) is that record.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from dreamjob.api.deps import CurrentSeeker, current_seeker
from dreamjob.db.repositories import employer_kind as kind_repo
from dreamjob.db.repositories import employer_product as run_repo
from dreamjob.db.repositories import employer_resolution as trail_repo
from dreamjob.pipeline import employer_product as emp_mod
from dreamjob.pipeline import employer_resolver as resolver
from dreamjob.pipeline.employer_kind import Kind, evidence_in_words
from dreamjob.security.audit import record_audit

log = logging.getLogger(__name__)

router = APIRouter()

Seeker = Annotated[CurrentSeeker, Depends(current_seeker)]

#: A single ladder walk is two requests to one host and about four seconds
#: (Agency_Research_Design.md section 5) - the register is one host at 0.5
#: requests per second, so a pass is serial there whatever the concurrency.
#: Twenty-five names is already a minute and a half, so an inline pass is
#: capped here and everything else is a job (FR-185).
INLINE_BATCH_LIMIT = 25


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ResolveRequest(BaseModel):
    """``force`` re-walks a company whose verdict is still fresh."""

    force: bool = False
    store: bool = True


class CorrectionRequest(BaseModel):
    """FR-285's precedent: a correction with no stated reason is not evidence."""

    kind: str = Field(description="agency | employer")
    note: str = Field(min_length=3, max_length=2000)
    evidence_url: str | None = Field(default=None, max_length=2000)


class BatchRequest(BaseModel):
    limit: int = Field(default=100, ge=1, le=2000)
    country: str | None = Field(default=None, max_length=2)
    refresh: bool = False
    concurrency: int = Field(default=4, ge=1, le=16)
    background: bool = True


def _company_or_404(company_id: str) -> dict:
    company = run_repo.company(company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "company not found")
    return company


def _tag_payload(company: dict, seeker: Seeker) -> dict[str, Any]:
    """The verdict as this reader sees it, with everything needed to argue with it."""
    tag = emp_mod.tag_for_company(
        company["id"], job_seeker_id=seeker.id, company=company
    )
    payload = tag.as_dict(seeker.locale)
    payload["company_name"] = company.get("name")
    payload["evidence_in_words"] = evidence_in_words(tag.verdict)
    payload["vacancy_count"] = run_repo.vacancy_count(company["id"])
    payload["attempts"] = trail_repo.attempts_for(company["id"])
    payload["advisory"] = (
        "This is what the knowledge base has established about who posts these "
        "vacancies. It orders and labels a list; it decides nothing (CR-405). If it "
        "is wrong, say so - the correction applies to your list immediately."
    )
    return payload


# ---------------------------------------------------------------------------
# Coverage (Agency_Research_Design.md section 7.5)
# ---------------------------------------------------------------------------


@router.get("/coverage")
def coverage(seeker: Seeker, queue_sample: int = Query(10, ge=0, le=100)) -> dict:
    """How much of the corpus has a verdict, by role, by rung and by reason.

    Two denominators, because they answer different questions.  *Employers*
    says how much of the knowledge base has been researched; *vacancies* says
    how much of a ranked list is accounted for, and the two diverge sharply -
    eleven employers hold 515 of the corpus's 2 687 postings, so resolving
    eleven names covers a fifth of the rows.

    ``unresearched`` is stated as its own number rather than left to be
    inferred from the difference.  An employer nobody has looked at is not a
    direct employer, and a coverage screen that lets that reading happen is the
    same mistake as a scorer that defaults ``cannot_tell`` to "employer".
    """
    _reconcile_runs()
    summary = kind_repo.coverage()
    employers = int(summary.get("employers_with_vacancies") or 0)
    verdicts = int(summary.get("verdicts") or 0)
    queue = kind_repo.companies_needing_resolution(limit=max(queue_sample, 1))
    covered = sum(int(v) for v in (summary.get("vacancies_by_role") or {}).values())
    total_vacancies = run_repo.total_vacancies()

    return {
        "employers_with_vacancies": employers,
        "verdicts": verdicts,
        "unresearched_employers": max(employers - verdicts, 0),
        "employer_share_resolved": round(verdicts / employers, 4) if employers else None,
        "vacancies_with_employer": total_vacancies,
        "vacancies_covered": covered,
        "vacancy_share_resolved": (
            round(covered / total_vacancies, 4) if total_vacancies else None
        ),
        "by_kind": summary.get("by_kind", {}),
        "by_role": summary.get("by_role", {}),
        "by_rung": summary.get("by_rung", {}),
        "by_tier": summary.get("by_tier", {}),
        "cannot_tell_by_reason": summary.get("cannot_tell_by_reason", {}),
        "vacancies_by_role": summary.get("vacancies_by_role", {}),
        "corrections": summary.get("corrections", {}),
        "flagged_for_review": summary.get("flagged", 0),
        "ladder_by_rung": trail_repo.coverage_by_rung(),
        "queue_depth": len(kind_repo.companies_needing_resolution(limit=1000)),
        "queue": [
            {
                "company_id": row["company_id"],
                "name": row.get("name"),
                "vacancy_count": row.get("vacancy_count"),
                "kind": row.get("kind"),
                "reason": row.get("reason"),
            }
            for row in queue[:queue_sample]
        ],
        "last_pass": run_repo.last_run(),
        "recent_passes": run_repo.recent_runs(limit=5),
        "note": (
            "An employer with no verdict has not been researched. It is not a direct "
            "employer by default, and nothing in the product treats it as one."
        ),
    }


def _reconcile_runs() -> None:
    """Close journal rows whose background job has finished (FR-185).

    The pass itself runs in ``employer_resolver``'s worker, which owns the
    checkpoint that makes it resumable (NFR-401).  Rather than wrap that worker
    to write the journal - which would put the resume logic in two places - the
    journal is closed the next time somebody reads coverage, from the job's own
    checkpoint.  A pass whose process died leaves a ``running`` row with a dead
    job, and it is closed as ``failed`` with that as the reason, which is the
    honest record of what happened.
    """
    for run in run_repo.recent_runs(limit=20):
        if run.get("status") != "running" or not run.get("job_run_id"):
            continue
        job = run_repo.job_state(str(run["job_run_id"]))
        if job is None or job["status"] in ("pending", "running", "paused"):
            continue
        report = (job.get("checkpoint") or {}).get("report") or {}
        status_map = {"done": "done", "failed": "failed", "cancelled": "cancelled"}
        run_repo.finish_run(
            run["id"],
            report,
            status=status_map.get(str(job["status"]), "failed"),
            reason=job.get("last_error"),
        )


# ---------------------------------------------------------------------------
# The background pass
# ---------------------------------------------------------------------------


@router.post("/resolve-batch")
async def resolve_batch(seeker: Seeker, body: BatchRequest | None = None) -> dict:
    """Walk the ladder over the queue, most-mislabelled-rows-first (FR-341, FR-185).

    The order is vacancy count, so a pass that is stopped half-way has still
    covered the postings a wrong answer would have damaged most.  A small batch
    runs inline; anything larger becomes a job, because the register rung is
    0.5 requests per second against one host and 458 Belgian names is half an
    hour.

    ``job_seeker_id`` on the journal row records who *asked*; the verdicts it
    writes carry no person at all (FR-344, RK-08).
    """
    options = body or BatchRequest()
    queue = await asyncio.to_thread(
        kind_repo.companies_needing_resolution,
        limit=options.limit,
        country=options.country,
    )
    if not queue:
        return {
            "started": False,
            "requested": 0,
            "reason": "nothing in the queue: every employer with vacancies has a verdict",
        }

    if options.background:
        job_id = await resolver.launch(
            job_seeker_id=seeker.id,
            limit=options.limit,
            refresh=options.refresh,
            country=options.country,
            concurrency=options.concurrency,
        )
        run_id = run_repo.start_run(
            job_run_id=job_id, job_seeker_id=seeker.id, requested=len(queue)
        )
        await asyncio.to_thread(
            record_audit,
            "employer_kind.batch_started",
            "job_run",
            job_id,
            seeker_id=seeker.id,
            detail={"requested": len(queue), "country": options.country},
        )
        return {
            "started": True,
            "background": True,
            "job_id": job_id,
            "run_id": run_id,
            "requested": len(queue),
            "note": (
                "The register answers about four seconds per name against one host, so "
                "this runs as a resumable job. Coverage reports it when it finishes."
            ),
        }

    if options.limit > INLINE_BATCH_LIMIT:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"an inline pass is capped at {INLINE_BATCH_LIMIT} employers; "
            "leave background on for anything larger",
        )
    run_id = run_repo.start_run(job_seeker_id=seeker.id, requested=len(queue))
    report = await resolver.resolve_many(
        options.limit,
        concurrency=options.concurrency,
        country=options.country,
        refresh=options.refresh,
    )
    payload = report.as_dict()
    run_repo.finish_run(run_id, payload, status=payload.get("status") or "done")
    await asyncio.to_thread(
        record_audit,
        "employer_kind.batch_completed",
        "employer_resolve_run",
        run_id,
        seeker_id=seeker.id,
        detail={k: payload.get(k) for k in ("requested", "resolved", "cannot_tell", "failed")},
    )
    return {"started": True, "background": False, "run_id": run_id, **payload}


# ---------------------------------------------------------------------------
# One employer
# ---------------------------------------------------------------------------


@router.get("/{company_id}/kind")
def employer_kind_view(company_id: str, seeker: Seeker) -> dict:
    """The verdict, with the evidence a job seeker would need to disagree (NFR-402).

    A company nobody has researched is not a 404 and not an employer: it comes
    back as ``cannot_tell(not_researched)`` with the cheapest next rung, which
    is a state the interface can render and act on.
    """
    company = _company_or_404(company_id)
    return _tag_payload(company, seeker)


@router.post("/{company_id}/resolve")
async def resolve_one(
    company_id: str, seeker: Seeker, body: ResolveRequest | None = None
) -> dict:
    """Walk the ladder for this employer now (FR-341).

    Cheapest rung first, stopping at the first that answers with confidence.
    Every rung that ran is in the answer, including the ones that handed down
    and the ones this build does not have: "we could not tell" is only an
    answer when it says what was tried.
    """
    company = _company_or_404(company_id)
    options = body or ResolveRequest()
    try:
        resolution = await resolver.resolve_employer_kind(
            company_id, force=options.force, store=options.store
        )
    except KeyError as exc:  # the company vanished between the two reads
        raise HTTPException(status.HTTP_404_NOT_FOUND, "company not found") from exc
    await asyncio.to_thread(
        record_audit,
        "employer_kind.resolved",
        "company",
        company_id,
        seeker_id=seeker.id,
        detail={"kind": resolution.kind, "force": options.force, "stored": resolution.stored},
    )
    return {
        "resolution": resolution.as_dict(),
        "verdict": _tag_payload(company, seeker) if options.store else None,
    }


@router.post("/{company_id}/correct", status_code=status.HTTP_201_CREATED)
def correct(company_id: str, seeker: Seeker, body: CorrectionRequest) -> dict:
    """"This is wrong" - private to its author, shared only by promotion.

    The benefit of sharing is real: a company is an agency for everybody, and
    corrections cluster on exactly the employers the machine cannot read.  The
    cost of getting it backwards is the failure the whole design exists to
    prevent - one person's mistake, or one person's grudge against a former
    employer, relabelling a good company for every other job seeker with no
    evidence anyone can check.  So a correction applies immediately and only to
    its author, and becomes shared when an operator confirms it or when two
    seekers independently say the same thing.

    A correction never silently overturns a register: that comes back as a
    conflict, with both sentences, for an operator to settle.
    """
    company = _company_or_404(company_id)
    try:
        outcome = kind_repo.record_correction(
            company_id,
            seeker.id,
            body.kind,
            body.note,
            evidence_url=body.evidence_url,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    payload = _tag_payload(company, seeker)
    conflict = bool(outcome.get("conflicts_with_registry"))
    return {
        "correction": outcome,
        "verdict": payload,
        "applies_to": "you" if not outcome.get("promoted") else "everyone",
        "conflicts_with_registry": conflict,
        "note": (
            "The register lists a regulated activity that contradicts this correction. "
            "Neither is applied over the other until an operator looks at it: the "
            "register is occasionally stale and a correction is occasionally wrong, and "
            "neither should win silently."
            if conflict
            else "This applies to your lists, your scoring and your documents straight "
            "away. It carries no weight for anyone else until it is promoted."
        ),
    }


@router.get("/{company_id}/corrections")
def corrections(company_id: str, seeker: Seeker) -> dict:
    """This employer's corrections: the seeker's own, and any shared one.

    Other seekers' private corrections are not readable here (FR-344); the
    operator's review queue is the place where they are, and it is behind the
    admin routes.
    """
    _company_or_404(company_id)
    rows = [
        row
        for row in kind_repo.corrections_for_company(company_id)
        if row.get("job_seeker_id") in (None, seeker.id)
    ]
    return {
        "company_id": company_id,
        "count": len(rows),
        "corrections": [
            {
                "kind": row.get("kind"),
                "note": row.get("note"),
                "scope": row.get("scope"),
                "evidence_url": row.get("evidence_url"),
                "created_at": row.get("created_at"),
                "promoted_by": row.get("promoted_by"),
                "is_mine": row.get("job_seeker_id") == seeker.id,
            }
            for row in rows
        ],
    }


@router.delete("/{company_id}/correct")
def withdraw(company_id: str, seeker: Seeker) -> dict:
    """Withdraw your own correction.  The row goes; the withdrawal is audited."""
    company = _company_or_404(company_id)
    removed = kind_repo.withdraw_correction(company_id, seeker.id, actor=seeker.id)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "you have no correction on this company")
    return {"withdrawn": True, "verdict": _tag_payload(company, seeker)}


@router.get("/review")
def review_queue(seeker: Seeker, limit: int = Query(50, ge=1, le=200)) -> dict:
    """Verdicts an operator should look at: anomalies, and unpromoted corrections.

    NFR-205: a page that addressed the classifier, asked it to change its task
    or claimed to be an instruction is recorded in ``anomalies`` as data about
    that page.  The verdict stands - the measured design survived a planted
    "ignore all previous instructions" line with its answer unchanged - and the
    attempt is surfaced here rather than swallowed.
    """
    if not seeker.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "administrators only")
    flagged = kind_repo.flagged_for_review(limit=limit)
    names = run_repo.company_names([str(r.get("company_id")) for r in flagged])
    return {
        "flagged": [
            {
                "company_id": row.get("company_id"),
                "name": names.get(str(row.get("company_id"))),
                "kind": row.get("kind"),
                "employer_role": row.get("employer_role"),
                "anomalies": row.get("anomalies") or [],
                "summary": row.get("summary"),
                "established_at": row.get("established_at"),
            }
            for row in flagged
        ],
        "pending_corrections": kind_repo.pending_review(limit=limit),
        "note": (
            "An anomaly is what a page tried, not what the verdict says. The verdict "
            "beside it was reached from the quoted evidence and is unchanged (NFR-205)."
        ),
    }


@router.get("")
def list_by_role(
    seeker: Seeker,
    role: str = Query("agency", description="direct | agency | board | unverified"),
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    """The employers behind one role, for the list filter and for the gates."""
    if role not in (
        emp_mod.ROLE_DIRECT, emp_mod.ROLE_AGENCY, emp_mod.ROLE_BOARD, emp_mod.ROLE_UNVERIFIED
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown role {role!r}")
    company_ids = kind_repo.companies_by_role((role,))[:limit]
    badges = kind_repo.badges(company_ids, seeker.id)
    names = run_repo.company_names(company_ids)
    counts = run_repo.vacancy_counts(company_ids)
    rows = [
        {
            "company_id": company_id,
            "name": names.get(company_id),
            "kind": badges.get(company_id, {}).get("kind", str(Kind.CANNOT_TELL)),
            "employer_role": badges.get(company_id, {}).get("employer_role", role),
            "service_model": badges.get(company_id, {}).get("service_model"),
            "confidence": badges.get(company_id, {}).get("confidence"),
            "summary": badges.get(company_id, {}).get("summary"),
            "vacancy_count": counts.get(company_id, 0),
        }
        for company_id in company_ids
    ]
    return {"role": role, "count": len(rows), "employers": rows}
